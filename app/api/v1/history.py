"""
模块：history_control.py
作用：会话历史管理 HTTP 接口，提供会话列表、详情、创建、改名、删除（预览/确认）、撤销删除与最近消息查询。
主要成员：
- history_router：会话历史路由对象（prefix=/history，路由级登录用户限流）；
- HISTORY_PAGE_SIZE：会话列表默认每页条数常量；
- SessionDetailRequest / CreateSessionRequest / UpdateTitleRequest / DeleteSessionRequest /
  DeleteConfirmRequest / RecentMessagesRequest：六个请求体模型；
- get_session_list：分页获取当前用户会话列表；
- get_session_detail：获取会话完整聊天记录（MySQL 长期记忆 + Redis 短期记忆拼接）；
- create_session / update_session_title：创建会话 / 更新标题；
- delete_session_preview / delete_session_confirm：删除会话的预览令牌签发与确认执行；
- undo_delete：撤销最近一次软删除；get_recent_messages：获取指定会话最近消息。
被谁使用：由 control/app.py 通过 `from app.api.v1.history import history_router` 导入并
          app.include_router 注册；全部端点需 JWT 鉴权，user_id 强制取自登录态，
          防止越权访问他人会话；路由由 HTTP 客户端（web/frontend 历史侧边栏）调用，非内部调用。
"""
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.infrastructure.persistence.repositories.session import SessionDAO
from app.infrastructure.persistence.repositories.read import Information_Read
from app.auth.guards import get_current_user
from app.auth.rate_limit import user_rate_limit
from app.auth.delete_guard import PendingDeleteStore
from core.responses import BizException
from app.domain.memory.context_memory import get_context_memory_service
from app.domain.memory.short_term import get_short_term_store

# 本模块日志器：记录短期记忆读取、临时知识库清理等非阻断失败
logger = logging.getLogger(__name__)

# 历史信息页滑动窗口默认每页会话数：get_session_list 的 page_size 查询参数缺省值
HISTORY_PAGE_SIZE = 10

# 会话历史路由：prefix=/history，由 control/app.py 的 app.include_router(history_router) 注册。
# 会话管理为轻 DB 操作：user_rate_limit(60, 60) 按登录用户限流 60 次/分钟（防脚本滥用）。
history_router = APIRouter(
    prefix="/history", tags=["history control"],
    dependencies=[Depends(user_rate_limit(60, 60))],
)


class SessionDetailRequest(BaseModel):
    """会话详情请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 get_session_detail 路由的 req 参数。
    字段：
    - session_id：待查询会话 ID，来源前端当前/选中的会话；必填。
    """

    session_id: int


class CreateSessionRequest(BaseModel):
    """创建会话请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 create_session 路由的 req 参数。
    字段：
    - title：新会话标题，来源前端；默认“新会话”，最长 100 字符。
    """

    title: str = Field(default="新会话", max_length=100)


class UpdateTitleRequest(BaseModel):
    """更新会话标题请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 update_session_title 路由的 req 参数。
    字段：
    - session_id：待改名会话 ID，来源前端；必填；
    - title：新标题，来源前端输入；校验规则 1~100 字符（不允许纯空）。
    """

    session_id: int
    title: str = Field(min_length=1, max_length=100)


class DeleteSessionRequest(BaseModel):
    """删除会话预览请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 delete_session_preview 路由的 req 参数。
    字段：
    - session_id：待删除会话 ID，来源前端；必填。
    """

    session_id: int


class DeleteConfirmRequest(BaseModel):
    """删除会话确认请求体模型。

    实例化位置：由前端确认对话框提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 delete_session_confirm 路由的 req 参数。
    字段：
    - session_id：待删除会话 ID，来源前端；必填，需与令牌内记录的会话一致；
    - confirm_token：二次确认令牌，来源 delete/preview 接口下发、前端原样回传。
    """

    session_id: int
    confirm_token: str


class RecentMessagesRequest(BaseModel):
    """最近消息请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 get_recent_messages 路由的 req 参数。
    字段：
    - session_id：会话 ID，来源前端；必填。
    """

    session_id: int


@history_router.post("/list")
def get_session_list(
    range: str = Query(default="all", pattern="^(day|week|all)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=HISTORY_PAGE_SIZE, ge=1, le=50),
    current_user: dict = Depends(get_current_user),
):
    """
    获取当前登录用户的会话列表（长期记忆）。

    HTTP 方法+路径：POST /history/list（分页/范围参数走查询字符串）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 历史侧边栏）调用，非内部调用。
    参数：
    - range：查询参数，时间范围 day/week/all（正则限定），来源前端；默认 all；
    - page：查询参数，页码，来源前端；默认 1，必须 >=1；
    - page_size：查询参数，每页条数，来源前端；默认 10，范围 1~50；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：HTTP JSON 响应，data/sessions 均为当前页会话列表（按最后消息时间倒序），
          另含 total（该范围总数）、page、page_size、has_more；记录不足一页时全部返回。
    """
    session_dao = SessionDAO()
    offset = (page - 1) * page_size
    result = session_dao.get_session_list_paged(
        current_user["user_id"], range_=range, limit=page_size, offset=offset
    )
    return {
        "status": "success",
        "data": result["data"],
        "sessions": result["data"],
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(result["data"]) < result["total"],
    }


@history_router.post("/detail")
def get_session_detail(req: SessionDetailRequest, current_user: dict = Depends(get_current_user)):
    """
    获取会话详情（聊天记录）。

    HTTP 方法+路径：POST /history/detail。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 打开会话）调用，非内部调用。
    参数：
    - req：详情请求体，SessionDetailRequest 模型，session_id 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：HTTP JSON 响应 {status:"success", data: messages}；messages 为完整聊天记录。
    异常说明：短期记忆（Redis）读取失败时仅记录错误日志，退化为仅返回 MySQL 已落库部分。

    长期记忆由短期记忆转变而来：MySQL 保存已落库部分，会话进行中未落库的
    消息在短期记忆（Redis）里，此处拼接两段返回完整记录。
    """
    user_id = current_user["user_id"]
    session_dao = SessionDAO()
    messages = session_dao.get_session_detail(user_id, req.session_id)
    try:
        messages = messages + get_short_term_store().pending_messages(user_id, req.session_id)
    except Exception as e:
        # 短期记忆不可用时退化为仅返回已落库部分
        logger.error("load pending short-term messages failed: %s", e)
    return {"status": "success", "data": messages}


@history_router.post("/create")
def create_session(req: CreateSessionRequest, current_user: dict = Depends(get_current_user)):
    """创建新会话。

    HTTP 方法+路径：POST /history/create。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 新建会话按钮）调用，非内部调用。
    参数：
    - req：创建请求体，CreateSessionRequest 模型，title 来自前端（默认“新会话”）；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：成功返回 {status:"success", session_id, title}；失败返回 {status:"fail", message}。
    """
    session_dao = SessionDAO()
    new_session_id = session_dao.create_session(current_user["user_id"], req.title)
    if new_session_id > 0:
        return {"status": "success", "session_id": new_session_id, "title": req.title}
    return {"status": "fail", "message": "创建会话失败"}


@history_router.post("/update_title")
def update_session_title(req: UpdateTitleRequest, current_user: dict = Depends(get_current_user)):
    """更新会话标题。

    HTTP 方法+路径：POST /history/update_title。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 重命名会话）调用，非内部调用。
    参数：
    - req：改名请求体，UpdateTitleRequest 模型，session_id/title 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：成功返回 {status:"success", message:"更新成功"}；
          会话不存在或不属于当前用户时返回 {status:"fail", message:"更新失败"}。
    """
    session_dao = SessionDAO()
    success = session_dao.update_session_title(current_user["user_id"], req.session_id, req.title)
    if success:
        return {"status": "success", "message": "更新成功"}
    return {"status": "fail", "message": "更新失败"}


@history_router.post("/delete/preview")
def delete_session_preview(req: DeleteSessionRequest, current_user: dict = Depends(get_current_user)):
    """删除会话预览：返回确认令牌和会话信息，前端需展示确认对话框。

    HTTP 方法+路径：POST /history/delete/preview；本接口仅签发令牌，不执行删除。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 删除会话入口）调用，非内部调用。
    参数：
    - req：删除预览请求体，DeleteSessionRequest 模型，session_id 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：{status:"success", confirm_token, session_id}，令牌供 confirm 接口回传。
    异常：会话不存在或不属于当前用户时抛 BizException(404)。
    """
    user_id = current_user["user_id"]
    session_dao = SessionDAO()
    if not session_dao.is_session_owner(user_id, req.session_id):
        raise BizException("会话不存在或不属于当前用户", http_status=404)
    token = PendingDeleteStore.create_token(
        user_id=user_id,
        action="delete_session",
        target_info={"session_id": req.session_id},
    )
    return {"status": "success", "confirm_token": token, "session_id": req.session_id}


@history_router.post("/delete/confirm")
def delete_session_confirm(req: DeleteConfirmRequest, current_user: dict = Depends(get_current_user)):
    """确认删除会话：验证令牌后执行删除，并联动清理临时知识库与记忆缓存。

    HTTP 方法+路径：POST /history/delete/confirm。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 删除确认对话框）调用，非内部调用。
    参数：
    - req：删除确认请求体，DeleteConfirmRequest 模型，session_id/confirm_token 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：删除成功返回 {status:"success", message:"删除成功"}；
          DAO 层删除失败（会话不存在等）返回 {status:"fail", message}。
          删除后尽力清理会话临时知识库、短期记忆与上下文记忆，清理失败仅记日志不阻断。
    异常：确认令牌无效/过期抛 BizException(400)；令牌与会话 ID 不匹配抛 BizException(400)。
    """
    user_id = current_user["user_id"]
    target = PendingDeleteStore.verify_token(req.confirm_token, user_id, "delete_session")
    if target is None:
        raise BizException("确认令牌无效或已过期，请重新操作", http_status=400)
    if target.get("session_id") != req.session_id:
        raise BizException("确认令牌与会话 ID 不匹配", http_status=400)

    session_dao = SessionDAO()
    ok = session_dao.delete_session(user_id, req.session_id)
    if not ok:
        return {"status": "fail", "message": "会话不存在或删除失败"}
    try:
        from app.application.ports.vector import get_temp_store
        get_temp_store().drop(user_id, req.session_id)
    except Exception as e:
        logger.error("clean temp knowledge failed: %s", e)
    try:
        get_short_term_store().clear(user_id, req.session_id)
        get_context_memory_service().clear(user_id, req.session_id)
    except Exception as e:
        logger.error("clean memory cache failed: %s", e)
    return {"status": "success", "message": "删除成功"}


@history_router.post("/undo_delete")
def undo_delete(current_user: dict = Depends(get_current_user)):
    """撤销最近一次软删除（每用户一次性机会）。

    HTTP 方法+路径：POST /history/undo_delete（无请求体）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 撤销删除提示）调用，非内部调用。
    参数：
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：{status:"success", message:"已恢复", type: recovered}，type 为恢复对象类型。
    异常：没有可撤销记录时抛 BizException(404)；底层恢复失败时抛 BizException(500)。
    """
    user_id = current_user["user_id"]
    from core.undo_store import UndoStore
    record = UndoStore.consume(user_id)
    if record is None:
        raise BizException("没有可撤销的删除记录", http_status=404)

    from app.infrastructure.persistence.repositories.soft_delete import recover_last_deleted
    recovered = recover_last_deleted(user_id)
    if recovered is None:
        raise BizException("恢复失败，记录可能已被彻底清理", http_status=500)
    return {"status": "success", "message": "已恢复", "type": recovered}


@history_router.post("/send")
def get_recent_messages(req: RecentMessagesRequest, current_user: dict = Depends(get_current_user)):
    """
    获取最近会话消息（供调试/上下文使用）。

    HTTP 方法+路径：POST /history/send。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 调试/上下文逻辑）调用，非内部调用。
    参数：
    - req：最近消息请求体，RecentMessagesRequest 模型，session_id 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：HTTP JSON 响应 {status:"success", data: messages}，messages 为该会话消息记录。
    说明：旧实现为 GET 携带 body（FastAPI 不会解析），已修正为 POST + Pydantic 模型。
    """
    read_dao = Information_Read()
    messages = read_dao.read_information(
        {"user_id": current_user["user_id"], "session_id": req.session_id}
    )
    return {"status": "success", "data": messages}
