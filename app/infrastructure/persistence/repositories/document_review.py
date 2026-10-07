"""
模块名：app.infrastructure.persistence.repositories.document_review

作用：
    文档人工审核队列的数据库操作，对应 MySQL 表 document_review
    （ORM 模型 db.models.DocumentReview；记录上传文件的原始文本 raw_text、
    清洗文本 cleaned_text、审核状态 status：pending/approved/rejected、
    审核备注 reviewer_notes、创建/审核时间等）。
    注意：审核通过后的向量库父子块入库不在本 DAO，而在 app/application/review/review_service.py。

主要成员：
    - DocumentReviewDAO：审核记录 CRUD，方法包括
      create() / get_by_id() / list_by_user() / list_all() /
      update_status() / get_pending_text() / _to_dict()。

被谁使用：
    - app/application/review/review_service.py 的 ReviewService.__init__ 中实例化
      （self._dao = DocumentReviewDAO()），由 submit_for_review / list_reviews /
      list_all_reviews / get_review / approve / reject 等服务方法调用；
    - tests/test_concurrency.py、tests/test_adversarial.py 中直接实例化做并发/对抗测试。

审核流程（服务层编排，DAO 仅提供对应数据操作）：
1. OCR/多模态提取完成后 → ReviewService.submit_for_review() → DAO create() 建 pending 记录；
2. 用户/审核员在审核页查看 → list_by_user() / list_all() / get_by_id()；
3. 审核通过或驳回 → update_status()（approve 后由服务层触发父子块入库）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import func as sa_func

from app.infrastructure.persistence.models import DocumentReview, UserInformation
from app.infrastructure.persistence.session import session_scope

logger = logging.getLogger(__name__)


class DocumentReviewDAO:
    """文档审核数据访问层，对应 MySQL 表 document_review。

    承担审核记录的增（create）、查（get_by_id / list_by_user / list_all /
    get_pending_text）、改（update_status）；不提供物理删除。
    实例化位置：app/application/review/review_service.py 的 ReviewService.__init__
    （self._dao = DocumentReviewDAO()），以及 tests 中的并发/对抗测试。
    无 __init__ 形参、不持有连接；每个方法内部以 session_scope() 获取
    SQLAlchemy 会话，ORM 操作随 with 块正常退出自动提交、异常自动回滚。
    """

    def create(
        self,
        user_id: int,
        file_name: str,
        file_path: str,
        doc_type: str,
        raw_text: str,
        cleaned_text: str,
    ) -> int:
        """创建一条待审核记录（INSERT，初始 status 固定为 "pending"）。

        被谁调用：app/application/review/review_service.py 的 ReviewService.submit_for_review()
        （文件.函数：review_service.ReviewService.submit_for_review），
        入参来自文件提取（OCR/多模态）完成后的上传处理链路。
        参数：
            user_id: 上传者用户 ID（登录态），同时作为记录归属与后续审核权限依据。
            file_name: 用户上传时的原始文件名，用于审核页展示与入库后的 original_name。
            file_path: 文件落盘后的物理存储路径（审核通过后向量入库的 source）。
            doc_type: 文档类型标识（如 pdf/txt/md 或提取通道类型）。
            raw_text: 提取得到的原始文本（未经清洗脱敏）。
            cleaned_text: 清洗/脱敏后的文本，审核通过后以此入库。
        返回：int，新记录主键 id（先 flush 取 id，随事务提交生效）；
              交给 ReviewService 作为审核记录 ID 返回给前端。
        """
        with session_scope() as session:
            review = DocumentReview(
                user_id=user_id,
                file_name=file_name,
                file_path=file_path,
                doc_type=doc_type,
                raw_text=raw_text,
                cleaned_text=cleaned_text,
                status="pending",
            )
            session.add(review)
            session.flush()
            review_id = review.id
            logger.info("Created review record %d for file %s", review_id, file_name)
            return review_id

    def get_by_id(self, review_id: int) -> Optional[dict]:
        """按主键 ID 获取单条审核记录（SELECT by id）。

        被谁调用：app/application/review/review_service.py 的 ReviewService.get_review / approve /
        reject（文件.函数：review_service.ReviewService.get_review、approve、reject），
        审核操作前先取记录并校验 status 是否仍为 pending。
        参数：
            review_id: 审核记录 ID，来源前端请求（经 app/api/v1/review.py 透传）。
        返回：Optional[dict]。命中返回 _to_dict() 序列化的记录字段
              （含文本、状态、时间等，供服务层判定与前端回显）；不存在返回 None。
        """
        with session_scope() as session:
            review = session.query(DocumentReview).filter_by(id=review_id).first()
            if not review:
                return None
            return self._to_dict(review)

    def is_owner_active(self, user_id: int) -> bool:
        """判定审核记录的上传者账号是否仍存活（未注销）。

        作用：document_review 表无软删字段，账号注销后其审核记录成为孤儿行；
        审核详情/通过/驳回经此判定拒绝孤儿记录，避免审核员操作已注销用户文档
        （approve 尤其会把向量写入已不存在的用户私有库）。
        被谁调用：app/application/review/review_service.py 的 ReviewService.get_review（详情、
                  approve、reject 三条链路共用该前置校验）。
        参数：user_id (int)——记录中的上传者 ID（review["user_id"]）。
        返回：bool。user_information 中存在且 deleted_at IS NULL 返回 True；
              账号不存在或已软删返回 False。
        """
        with session_scope() as session:
            return (
                session.query(UserInformation.id)
                .filter(UserInformation.id == user_id)
                .filter(UserInformation.deleted_at.is_(None))
                .first()
                is not None
            )

    def list_by_user(
        self,
        user_id: int,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[dict], int]:
        """分页查询指定用户自己的审核记录（SELECT + 可选 status 过滤）。

        被谁调用：app/application/review/review_service.py 的 ReviewService.list_reviews()
        （文件.函数：review_service.ReviewService.list_reviews），对应用户审核列表页。
        参数：
            user_id: 记录归属用户 ID，强制取登录态当前用户，只能看自己的记录。
            status: 可选状态过滤（pending/approved/rejected），来源前端查询参数；
                    为空/None 时不加状态条件。
            page: 页码，从 1 开始，来源前端分页参数。
            page_size: 每页行数，来源前端分页参数，默认 20。
        返回：Tuple[List[dict], int]，即 (records, total_count)：
              records 为当前页 _to_dict() 字典列表（按 created_at 倒序），
              total_count 为过滤条件下的总数，供服务层/前端计算分页。
        SQL 安全：分页通过 ORM offset((page-1)*page_size).limit(page_size)
              实现，行数被 page_size 钳制，避免一次性拉全表。
        """
        with session_scope() as session:
            query = session.query(DocumentReview).filter_by(user_id=user_id)
            if status:
                query = query.filter_by(status=status)

            total = query.count()
            records = (
                query.order_by(DocumentReview.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            return [self._to_dict(r) for r in records], total

    def list_all(
        self,
        status: Optional[str] = None,
        user_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[dict], int]:
        """审核员视图：分页查询所有用户的审核记录（SELECT，可选 status/user_id 过滤）。

        供 teacher/admin 角色使用（/review/all 端点）。可选按 status / user_id 过滤。
        与 list_by_user 的差异：不强制 user_id == current_user.id；角色鉴权在
        control/service 层完成，DAO 只负责按给定条件查询。

        孤儿记录防护：固定 INNER JOIN user_information 并过滤 deleted_at IS NULL——
        上传者账号已注销（软删）的审核记录不得进入审核队列，也避免审核员对孤儿
        记录执行 approve 后把向量写入已注销用户的私有库。document_review 表本身
        无软删字段，账号注销后的残留记录只能靠此 join 在读侧排除（写侧清理由
        注销硬删计划负责）。

        被谁调用：app/application/review/review_service.py 的 ReviewService.list_all_reviews()
        （文件.函数：review_service.ReviewService.list_all_reviews）。
        参数：
        status: 可选状态过滤（pending/approved/rejected），来源前端查询参数。
        user_id: 可选按上传者过滤；None 表示不限制上传者。
        page: 页码，从 1 开始。
        page_size: 每页行数，默认 20（分页上限，防止全表返回）。
        返回：Tuple[List[dict], int]，即 (records, total_count)，
        结构与 list_by_user 相同，records 按 created_at 倒序。
        """
        with session_scope() as session:
            query = (
                session.query(DocumentReview)
                .join(UserInformation, UserInformation.id == DocumentReview.user_id)
                .filter(UserInformation.deleted_at.is_(None))
            )
            if status:
                query = query.filter(DocumentReview.status == status)
            if user_id is not None:
                query = query.filter(DocumentReview.user_id == user_id)

            total = query.count()
            records = (
                query.order_by(DocumentReview.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            return [self._to_dict(r) for r in records], total

    def update_status(
        self,
        review_id: int,
        user_id: int,
        status: str,
        notes: Optional[str] = None,
        edited_text: Optional[str] = None,
    ) -> bool:
        """更新审核状态（UPDATE：status/reviewed_at，可选 notes 与 cleaned_text）。

        被谁调用：app/application/review/review_service.py 的 ReviewService.approve() 与 reject()
        （文件.函数：review_service.ReviewService.approve、reject）。
        权限防护：查询条件固定带 id + user_id 双键，只有记录归属本人才能改到行；
        teacher/admin 代审他人文档时，服务层保证传入记录归属 user_id（上传者），
        审核操作者身份在服务层日志中记录（见 review_service.approve/reject）。
        防重复审核由服务层先查 status == "pending" 保证。
        参数：
            review_id: 审核记录 ID，来源前端审核操作请求。
            user_id: 记录归属用户 ID（文档上传者，作为 WHERE 归属键）；本人审核时
                    即登录用户，teacher/admin 代审时由服务层传记录 owner 而非审核操作者。
            status: 目标状态，"approved"（通过）或 "rejected"（驳回）。
            notes: 审核备注/驳回原因；为空则不覆盖 reviewer_notes。
            edited_text: 用户在审核页编辑修正后的文本，非 None 时覆盖
                         cleaned_text（审核通过后以覆盖后的文本入库）。
        返回：bool。True 表示命中并更新了记录；记录不存在或归属不符返回 False，
              服务层据此向用户返回“更新审核状态失败”。
        """
        with session_scope() as session:
            review = (
                session.query(DocumentReview)
                .filter_by(id=review_id, user_id=user_id)
                .first()
            )
            if not review:
                return False

            review.status = status
            review.reviewed_at = datetime.now()
            if notes:
                review.reviewer_notes = notes
            if edited_text is not None:
                review.cleaned_text = edited_text

            logger.info("Review %d status → %s by user %d", review_id, status, user_id)
            return True

    def get_pending_text(self, review_id: int) -> Optional[str]:
        """获取 pending 状态记录的清洗后文本（SELECT cleaned_text）。

        用途：供“审核通过后取文本入库”的链路使用；当前服务层 approve() 直接经
        get_by_id() 取 cleaned_text，本方法为预留的窄查询（只取文本列），
        代码库中暂无生产调用方。
        参数：
            review_id: 审核记录 ID，来源审核操作请求。
        返回：Optional[str]。仅当记录存在且 status == "pending" 时返回
              cleaned_text；否则返回 None（隐式防止对非待审记录取文本入库）。
        """
        with session_scope() as session:
            review = (
                session.query(DocumentReview)
                .filter_by(id=review_id, status="pending")
                .first()
            )
            if not review:
                return None
            return review.cleaned_text

    def _to_dict(self, review: DocumentReview) -> dict:
        """ORM 实例 → 纯字典的内部序列化方法（不访问数据库）。

        被谁调用：本类 get_by_id / list_by_user / list_all 在会话关闭前调用，
        将 ORM 对象转为可跨会话使用、可直接 JSON 响应的 dict；
        时间字段统一转 ISO 字符串，None 安全处理。
        参数：
            review: 已查出的 DocumentReview ORM 实例（须在 session 生命周期内访问属性）。
        返回：dict，键含 id、user_id、file_name、file_path、doc_type、raw_text、
              cleaned_text、status、reviewer_notes、created_at、reviewed_at，
              交给 service 层再返回 control 层响应前端。
        """
        return {
            "id": review.id,
            "user_id": review.user_id,
            "file_name": review.file_name,
            "file_path": review.file_path,
            "doc_type": review.doc_type,
            "raw_text": review.raw_text,
            "cleaned_text": review.cleaned_text,
            "status": review.status,
            "reviewer_notes": review.reviewer_notes,
            "created_at": review.created_at.isoformat() if review.created_at else None,
            "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        }
