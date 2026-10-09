"""
模块：review_control.py
作用：文档审核 HTTP 接口。用户查看/审核本人的 OCR 或多模态文本提取结果；
      teacher/admin 审核员可查看全量队列并代审任意用户文档（通过后知识写入
      文档上传者的私有库，归属修正逻辑在 app/application/review/review_service.py）。
主要成员：
- review_router：文档审核路由对象（prefix=/review）；
- _MODERATOR_ROLES：可代审角色常量元组 ("teacher", "admin")；
- _can_moderate：记录查看/审核权限判定（所有者本人或审核员）；
- _get_service：内部工厂函数，返回 ReviewService 实例；
- list_reviews：分页查询当前用户自己的审核记录；
- list_all_reviews：审核员视图，分页查询所有用户记录（仅 teacher/admin）；
- get_review：获取单条审核详情（含原文与清洗文本）；
- approve_review：审核通过并将文本入库知识库（可选编辑覆盖）；
- reject_review：驳回审核记录（notes 必填）。
被谁使用：由 control/app.py 通过 `from app.api.v1.review import review_router` 导入并
          app.include_router 注册；各端点经 get_current_user 做 JWT 鉴权，权限校验统一走
          _can_moderate（普通用户访问他人记录 403 的水平越权防护不放松）；
          路由由 HTTP 客户端（web/frontend 审核页：我的审核/全部审核两个视图）调用，非内部调用。

端点：
- GET  /review/list          分页查询当前用户审核记录（可选 status 过滤）
- GET  /review/all           审核员视图：查询所有用户审核记录（仅 teacher/admin，可按 user_id 过滤）
- GET  /review/{review_id}   获取单条审核详情（所有者本人或 teacher/admin）
- POST /review/{review_id}/approve  审核通过（可选 edited_text 覆盖；所有者本人或 teacher/admin）
- POST /review/{review_id}/reject   驳回（notes 必填；所有者本人或 teacher/admin）
"""
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query

from app.auth.guards import get_current_user
from core.responses import BizException, success
from app.application.review.review_service import ReviewService

# 本模块日志器：预留给审核流程异常记录
logger = logging.getLogger(__name__)
# 文档审核路由：prefix=/review，由 control/app.py 的 app.include_router(review_router) 注册；
# 路由未设置路由级限流，鉴权统一由各端点的 get_current_user 依赖完成
review_router = APIRouter(prefix="/review", tags=["document review"])


def _get_service() -> ReviewService:
    """构造并返回一个 ReviewService 实例（简单工厂，便于各路由获取服务层对象）。

    被谁调用：模块内部函数，由本文件全部五个路由函数调用。
    返回：新的 ReviewService 实例，交给调用方执行审核业务逻辑。
    """
    return ReviewService()


# 可代审角色：teacher 承担课程内容审核员职责，admin 为超管，均可查看/审核他人记录
_MODERATOR_ROLES = ("teacher", "admin")


def _can_moderate(review: dict, current_user: dict) -> bool:
    """判定当前用户是否有权查看/审核目标记录（所有者本人或 teacher/admin 审核员）。

    被谁调用：get_review / approve_review / reject_review 三个端点做归属与角色校验。
    参数：
    - review：审核记录 dict（须含 user_id，文档上传者），来源 ReviewService.get_review。
    - current_user：JWT 注入的登录用户 dict（user_id/role）。
    返回：bool。True 表示记录所有者本人，或角色属于 teacher/admin；其余普通用户
          访问他人记录返回 False（端点转 403，水平越权防护不放松）。
    """
    return (
        review["user_id"] == current_user["user_id"]
        or current_user.get("role") in _MODERATOR_ROLES
    )


@review_router.get("/list")
def list_reviews(
    status: Optional[str] = Query(None, description="过滤状态: pending/approved/rejected"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """分页查询当前用户的审核记录。

    HTTP 方法+路径：GET /review/list。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由未声明限流依赖。
    被谁调用：由 HTTP 客户端（web/frontend 我的审核列表）调用，非内部调用。
    参数：
    - status：查询参数，可选过滤状态 pending/approved/rejected，来源前端；不传表示全部；
    - page：查询参数，页码，来源前端；默认 1，必须 >=1；
    - page_size：查询参数，每页条数，来源前端；默认 20，范围 1~100；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：统一 success 结构的 HTTP JSON 响应，data 为记录列表（已剔除体积过大的 raw_text），
          另含 total/page/page_size。
    异常：status 取值非法时抛 BizException(400)。
    """
    if status and status not in ("pending", "approved", "rejected"):
        raise BizException("无效的 status 参数", http_status=400)

    svc = _get_service()
    records, total = svc.list_reviews(
        user_id=current_user["user_id"],
        status=status,
        page=page,
        page_size=page_size,
    )
    for r in records:
        r.pop("raw_text", None)
    return success(data=records, total=total, page=page, page_size=page_size)


@review_router.get("/all")
def list_all_reviews(
    status: Optional[str] = Query(None, description="过滤状态: pending/approved/rejected"),
    user_id: Optional[int] = Query(None, description="按上传用户ID过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """审核员视图：分页查询所有用户的审核记录。

    HTTP 方法+路径：GET /review/all。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；函数内再校验角色，仅 teacher/admin 放行
                （teacher 承担“课程内容审核员”职责，admin 为超管）；路由未声明限流依赖。
    被谁调用：由 HTTP 客户端（web/frontend 审核队列页）调用，非内部调用。
    参数：
    - status：查询参数，可选过滤状态 pending/approved/rejected，来源前端；不传表示全部；
    - user_id：查询参数，可选按上传用户 ID 过滤，来源前端；不传表示全部用户；
    - page / page_size：查询参数，分页，默认 1 / 20，page_size 范围 1~100；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：统一 success 结构的 HTTP JSON 响应，data 为全量记录列表（含文件路径与上传者
          user_id，但剔除 raw_text），另含 total/page/page_size。
    异常：非 teacher/admin 角色抛 BizException(403)；status 非法抛 BizException(400)。
    """
    role = current_user.get("role")
    if role not in ("teacher", "admin"):
        raise BizException("没有权限查看全量审核队列", http_status=403)

    if status and status not in ("pending", "approved", "rejected"):
        raise BizException("无效的 status 参数", http_status=400)

    svc = _get_service()
    records, total = svc.list_all_reviews(
        status=status,
        user_id=user_id,
        page=page,
        page_size=page_size,
    )
    # 审核员视图需展示文件路径与上传者 user_id，但 raw_text 体积过大不返回
    for r in records:
        r.pop("raw_text", None)
    return success(data=records, total=total, page=page, page_size=page_size)


@review_router.get("/{review_id}")
def get_review(
    review_id: int,
    current_user: dict = Depends(get_current_user),
):
    """获取单条审核记录详情（含原始文本与清洗后文本）。

    HTTP 方法+路径：GET /review/{review_id}。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；函数内做权限校验——记录所有者本人或
                teacher/admin 审核员（_can_moderate）可查看，其余普通用户拒绝；路由未声明限流依赖。
    被谁调用：由 HTTP 客户端（web/frontend 审核详情页）调用，非内部调用。
    参数：
    - review_id：路径参数，审核记录 ID，来源前端请求路径；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：统一 success 结构的 HTTP JSON 响应，data 为完整审核记录（含 raw_text）。
    异常：记录不存在抛 BizException(404)；非所有者且非 teacher/admin 抛 BizException(403)。
    """
    svc = _get_service()
    review = svc.get_review(review_id)
    if not review:
        raise BizException("审核记录不存在", http_status=404)
    if not _can_moderate(review, current_user):
        raise BizException("没有权限查看此记录", http_status=403)
    return success(data=review)


@review_router.post("/{review_id}/approve")
def approve_review(
    review_id: int,
    edited_text: Optional[str] = Body(None, description="编辑后的文本，覆盖原 cleaned_text"),
    notes: Optional[str] = Body(None, description="审核备注"),
    current_user: dict = Depends(get_current_user),
):
    """审核通过并将文本入库到知识库。

    HTTP 方法+路径：POST /review/{review_id}/approve（请求体为 JSON Body 字段）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；函数内做权限校验——记录所有者本人或
                teacher/admin 审核员（_can_moderate）可操作；路由未声明限流依赖。
    被谁调用：由 HTTP 客户端（web/frontend 审核详情页通过按钮）调用，非内部调用。
    参数：
    - review_id：路径参数，审核记录 ID，来源前端请求路径；
    - edited_text：请求体字段（可选），审核员编辑后的文本，提供时覆盖原 cleaned_text；来源前端；
    - notes：请求体字段（可选），审核备注，来源前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息（审核操作者）。
    返回：统一 success 结构的 HTTP JSON 响应，含 review_id、parent_chunks、child_chunks
          （实际入库的父/子分块数量）。
    异常：记录不存在抛 BizException(404)；非所有者且非 teacher/admin 抛 BizException(403)；
          service 层返回失败时抛 BizException(400)。
    """
    svc = _get_service()
    review = svc.get_review(review_id)
    if not review:
        raise BizException("审核记录不存在", http_status=404)
    if not _can_moderate(review, current_user):
        raise BizException("没有权限操作此记录", http_status=403)

    result = svc.approve(
        review_id,
        user_id=current_user["user_id"],
        edited_text=edited_text,
        notes=notes,
    )
    if not result.get("success"):
        raise BizException(result.get("error", "审核通过失败"), http_status=400)
    return success(
        review_id=review_id,
        parent_chunks=result.get("parent_chunks", 0),
        child_chunks=result.get("child_chunks", 0),
    )


@review_router.post("/{review_id}/reject")
def reject_review(
    review_id: int,
    notes: str = Body(..., embed=True, description="驳回原因"),
    current_user: dict = Depends(get_current_user),
):
    """驳回审核记录。

    HTTP 方法+路径：POST /review/{review_id}/reject（请求体为 JSON 对象 {"notes": "原因"}，
    embed=True 与前端 reviewApi.rejectReview 及 approve 的对象体风格保持一致）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；函数内做权限校验——记录所有者本人或
                teacher/admin 审核员（_can_moderate）可操作；路由未声明限流依赖。
    被谁调用：由 HTTP 客户端（web/frontend 审核详情页驳回按钮）调用，非内部调用。
    参数：
    - review_id：路径参数，审核记录 ID，来源前端请求路径；
    - notes：请求体字段（必填），驳回原因，来源前端输入；空白字符串在函数内判为非法；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息（审核操作者）。
    返回：统一 success 结构的 HTTP JSON 响应，含 review_id。
    异常：notes 为空/纯空白抛 BizException(400)；记录不存在抛 BizException(404)；
          非所有者且非 teacher/admin 抛 BizException(403)；记录状态不允许驳回时抛 BizException(400)。
    """
    if not notes or not notes.strip():
        raise BizException("驳回原因不能为空", http_status=400)

    svc = _get_service()
    review = svc.get_review(review_id)
    if not review:
        raise BizException("审核记录不存在", http_status=404)
    if not _can_moderate(review, current_user):
        raise BizException("没有权限操作此记录", http_status=403)

    ok = svc.reject(review_id, user_id=current_user["user_id"], notes=notes.strip())
    if not ok:
        raise BizException("驳回失败，记录不存在或状态不允许", http_status=400)
    return success(review_id=review_id)
