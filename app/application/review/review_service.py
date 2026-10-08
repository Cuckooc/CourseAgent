"""
模块名：app.application.review.review_service
作用：文档审核服务。对 OCR/多模态提取结果提供人工审核工作流：创建 pending
      审核记录、分页查询（本人/审核员视图）、审核通过（可附带人工修正文本，
      重新脱敏后父子块入库并 flush 索引）或驳回。协调审核表 DAO、脱敏服务
      与持久化向量库网关。

      代审归属规则（2026-09 教师全量队列接入时修正）：approve/reject 的
      user_id 形参是「审核操作者」（JWT，仅记审计日志）；记录定位（DAO
      id+user_id 双键）与向量入库归属一律取 review["user_id"]（文档上传者），
      保证 teacher/admin 代审时状态更新得到行、知识写入上传者私有库而非审核者库。

流程：
1. 文件提取完成后（OCR/多模态），调用 submit_for_review() 创建 pending 记录
2. 用户在审核页查看/编辑 → approve 或 reject
3. approve 时取 cleaned_text（或用户编辑后的文本）执行父子块入库

主要成员：
- ReviewService：文档审核服务类。
- submit_for_review()：创建 pending 审核记录。
- list_reviews()/list_all_reviews()：本人/审核员分页查询审核记录。
- get_review()：审核记录详情。
- approve()：审核通过 → 重新脱敏 → 写持久化向量库 → flush 索引。
- reject()：驳回审核。

被谁使用：
- app/api/v1/review.py：_get_service() 中每次请求 `ReviewService()`
  新建轻量实例，/review 列表、/review/all、/review/{id} 详情、
  /review/{id}/approve、/review/{id}/reject 五个端点分别调用上述方法。
- submit_for_review 当前在仓库内无控制层调用点（为 OCR/多模态提取后
  自动送审预留，DAO 文档注释描述了该流程）。
"""
import logging
from typing import Dict, List, Optional, Tuple

from app.infrastructure.persistence.repositories.document_review import DocumentReviewDAO
from app.application.files.mask_service import mask_text
from app.application.ports.vector import (
    add_parent_child,
    flush_persistent_index,
    get_persistent_db,
    persistent_lock,
)

# 模块级日志器：审核提交/通过/驳回与入库失败日志走该 logger
logger = logging.getLogger(__name__)


class ReviewService:
    """文档审核业务逻辑（审核记录状态机 + 通过即入库）。

    类作用：封装审核记录的增查与状态流转；approve 时把审核文本经脱敏后
            写入文档上传者的持久化知识库（支持 teacher/admin 代审，入库归属
            取记录 owner 而非审核操作者），并立即 flush HNSW 索引保证可检索。
            状态约束：仅 pending 记录可审核，防重复提交。

    实例化位置：app/api/v1/review.py 的 _get_service() 中每个请求
            `ReviewService()` 新建（类本身无跨请求可变状态，DAO 无状态）。

    关键 self 属性：
    - _dao：dao/document_review.py 的 DocumentReviewDAO 实例，负责审核表
      document_review（MySQL）的增删改查；全部审核数据读写都经它完成。
    """

    def __init__(self):
        # __init__ 无形参：DAO 无状态，每次新建 ReviewService 时随之实例化
        self._dao = DocumentReviewDAO()

    def submit_for_review(
        self,
        user_id: int,
        file_name: str,
        file_path: str,
        doc_type: str,
        raw_text: str,
        cleaned_text: str,
    ) -> int:
        """提交审核，返回审核记录 ID。

        功能：把一次文件提取结果（原文 + 清洗文本）落为 pending 审核记录，
              等待人工审核，并记录提交日志。
        被谁调用：当前仓库内无控制层调用点（为 OCR/多模态提取完成后自动
                  送审预留，DAO 文档注释描述了该流程）。
        参数：
        - user_id (int)：文件所属用户 ID（来源：JWT）。
        - file_name (str)：原始文件名（审核页展示用）。
        - file_path (str)：物理文件绝对路径（approve 入库时作为向量块 source）。
        - doc_type (str)：提取类型 pure_text/scanned/two_column/image_rich。
        - raw_text (str)：提取的原始文本（OCR/多模态直出，仅供对照查看）。
        - cleaned_text (str)：清洗后文本（默认入库文本，用户可再编辑）。
        返回：int——新建审核记录的自增 ID；去向：调用方可据此跳转审核详情。
        数据去向：dao/document_review.py 的 DocumentReviewDAO.create → MySQL。
        """
        review_id = self._dao.create(
            user_id=user_id,
            file_name=file_name,
            file_path=file_path,
            doc_type=doc_type,
            raw_text=raw_text,
            cleaned_text=cleaned_text,
        )
        logger.info(
            "Review submitted: id=%d, file=%s, doc_type=%s, user=%d",
            review_id, file_name, doc_type, user_id,
        )
        return review_id

    def list_reviews(
        self,
        user_id: int,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[dict], int]:
        """分页查询当前用户自己的审核记录。

        功能：按状态可选过滤，分页返回该用户提交的审核记录与总条数。
        被谁调用：app/api/v1/review.py 的 /review 列表端点（list_reviews）。
        参数：
        - user_id (int)：JWT 注入的当前用户 ID，仅查本人记录。
        - status (str|None)：pending/approved/rejected 过滤（control 层校验合法性）。
        - page (int) / page_size (int)：页码与每页条数，来源：查询参数。
        返回：Tuple[List[dict], int]——(记录列表, 总条数)；数据来源：
              DocumentReviewDAO.list_by_user 查 MySQL；去向：列表接口 JSON。
        """
        return self._dao.list_by_user(user_id, status=status, page=page, page_size=page_size)

    def list_all_reviews(
        self,
        status: Optional[str] = None,
        user_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[dict], int]:
        """审核员视图：分页查询所有用户的审核记录（/review/all 端点）。

        功能：跨用户分页查询审核记录，可按 status / user_id 过滤。
        被谁调用：app/api/v1/review.py 的 /review/all 端点
                  （list_all_reviews，端点侧已限 teacher/admin 角色）。
        参数：
        - status (str|None)：pending/approved/rejected 过滤，来源：查询参数。
        - user_id (int|None)：可选按提交用户过滤，来源：查询参数。
        - page (int) / page_size (int)：页码与每页条数。
        返回：Tuple[List[dict], int]——(记录列表, 总条数)；数据来源：
              DocumentReviewDAO.list_all 查 MySQL；去向：审核员视图 JSON。
        """
        return self._dao.list_all(status=status, user_id=user_id, page=page, page_size=page_size)

    def get_review(self, review_id: int) -> Optional[dict]:
        """获取单条审核记录详情（含上传者账号存活校验）。

        功能：按主键取审核记录全文（含 raw_text/cleaned_text，供审核页
              对照与编辑）；若记录归属的上传者账号已注销（软删），视为不可
              操作的孤儿记录，统一返回 None（详情端点转 404）。
        被谁调用：app/api/v1/review.py 的 /review/{review_id} 详情端点；
                  本类 approve / reject 内部也改走本方法，保证三条链路孤儿
                  拦截口径一致。
        参数：review_id (int)——审核记录 ID，来源：路径参数。
        返回：Optional[dict]——记录字典；记录不存在或上传者已注销返回 None。
        """
        review = self._dao.get_by_id(review_id)
        if review is None:
            return None
        if not self._dao.is_owner_active(review["user_id"]):
            logger.warning(
                "Review %d belongs to deactivated user %d; denied access",
                review_id, review["user_id"],
            )
            return None
        return review

    def approve(
        self,
        review_id: int,
        user_id: int,
        edited_text: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict:
        """审核通过并触发知识库入库。

        功能：①校验记录存在且处于 pending（防重复审核）；②edited_text
        非空时覆盖 cleaned_text（审核员手动修正 OCR 错误），空文本拒绝入库；
        ③入库前重新执行 mask_text 脱敏，防止编辑引入敏感信息；④DAO 更新
        状态为 approved（WHERE 双键传记录归属者 review["user_id"]，支持
        teacher/admin 代审）；⑤以审核文件为 source、scope=private 调
        add_parent_child 写入文档上传者的持久化向量库（而非审核者库），并
        flush_persistent_index 立即落盘 HNSW 索引。
        被谁调用：app/api/v1/review.py 的 /review/{review_id}/approve 端点
                  （所有者本人或 teacher/admin 审核员）。
        参数：
        - review_id (int)：审核记录 ID，来源：路径参数。
        - user_id (int)：审核操作者用户 ID（JWT；仅用于审计日志，记录是谁审核的）。
          记录归属与入库归属一律以 review["user_id"]（文档上传者）为准。
        - edited_text (str|None)：审核页人工修正后的文本，来源：请求体。
        - notes (str|None)：审核备注，来源：请求体。
        返回：Dict——成功 {"success": True, "review_id", "parent_chunks",
              "child_chunks"}；各类失败 {"success": False, "error": 原因}。
              去向：approve 端点 JSON 返回前端；向量数据去向：
              app/infrastructure/vector_store/persistent 的 chromadb_data 持久库。
        异常：向量入库抛错时状态已更新为 approved，方法捕获后返回
              success=False 并记录异常日志（不向 control 层抛出）。
        """
        review = self.get_review(review_id)
        if not review:
            return {"success": False, "error": "审核记录不存在或上传者已注销"}
        if review["status"] != "pending":
            return {"success": False, "error": f"记录状态为 {review['status']}，无法重复审核"}

        text_to_ingest = edited_text if edited_text is not None else review["cleaned_text"]
        if not text_to_ingest.strip():
            return {"success": False, "error": "入库文本为空，无法入库"}

        text_to_ingest = mask_text(text_to_ingest)

        # DAO 以 id+user_id 双键定位记录，user_id 必须是文档归属者（上传者）而非审核
        # 操作者：teacher/admin 代审他人文档时二者不同，传操作者 id 会导致更新不到行。
        owner_id = review["user_id"]
        ok = self._dao.update_status(
            review_id, owner_id, "approved",
            notes=notes,
            edited_text=edited_text,
        )
        if not ok:
            return {"success": False, "error": "更新审核状态失败"}

        try:
            # 入库归属同样是文档上传者：教师/管理员代审通过后，知识写入上传者的
            # 私有库（scope=private + owner_id），而非审核者库
            parents, children = add_parent_child(
                get_persistent_db(),
                persistent_lock(),
                source=review["file_path"],
                scope="private",
                text=text_to_ingest,
                user_id=owner_id,
                original_name=review["file_name"],
            )
            logger.info(
                "Review %d approved by reviewer %s (owner %s): %d parents, %d children ingested",
                review_id, user_id, owner_id, parents, children,
            )
            flush_persistent_index()
            return {
                "success": True,
                "review_id": review_id,
                "parent_chunks": parents,
                "child_chunks": children,
            }
        except Exception as e:
            logger.exception("Failed to ingest review %d: %s", review_id, e)
            return {"success": False, "error": f"知识库入库失败: {str(e)}"}

    def reject(
        self,
        review_id: int,
        user_id: int,
        notes: Optional[str] = None,
    ) -> bool:
        """驳回审核。

        功能：校验记录存在且处于 pending 后，把状态更新为 rejected
              （WHERE 双键传记录归属者 review["user_id"]，支持 teacher/admin 代审；
              驳回不触发向量入库）。
        被谁调用：app/api/v1/review.py 的 /review/{review_id}/reject 端点
                  （所有者本人或 teacher/admin 审核员）。
        参数：
        - review_id (int)：审核记录 ID，来源：路径参数。
        - user_id (int)：审核操作者用户 ID（JWT；仅记审计日志），归属以记录 owner 为准。
        - notes (str|None)：驳回原因备注（端点侧要求非空）。
        返回：bool——True 状态更新成功；False 表示记录不存在、非 pending
              或更新失败。去向：reject 端点据真假返回成功/失败响应。
        数据去向：DocumentReviewDAO.update_status 更新 MySQL 审核表。
        """
        review = self.get_review(review_id)
        if not review:
            return False
        # 状态防重：仅 pending 可驳回，approved/rejected 重复请求一律失败
        if review["status"] != "pending":
            return False
        # 同 approve：DAO 双键定位须传文档归属者 id，教师/管理员代审时 user_id 是操作者
        ok = self._dao.update_status(review_id, review["user_id"], "rejected", notes=notes)
        if ok:
            logger.info(
                "Review %d rejected by reviewer %s (owner %s)",
                review_id, user_id, review["user_id"],
            )
        return ok
