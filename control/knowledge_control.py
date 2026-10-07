"""
模块：knowledge_control.py
作用：知识库文件管理 HTTP 接口，提供可见文件列表与删除文件（预览/确认两步式）。
主要成员：
- knowledge_router：知识库路由对象（prefix=/knowledge，路由级登录用户限流）；
- KnowledgeDeleteRequest / KnowledgeDeleteConfirmRequest：删除预览、删除确认两个请求体模型；
- list_documents：列出当前用户可见的知识库文件；
- delete_document_preview / delete_document_confirm：删除文件的预览令牌签发与确认执行。
被谁使用：由 control/app.py 通过 `from control.knowledge_control import knowledge_router`
          导入并 app.include_router 注册；JWT 鉴权 + per-user 限流；
          路由由 HTTP 客户端（web/frontend 知识库管理页）调用，非内部调用。

知识库分三类：
- public：公共知识库，所有用户可检索，teacher/admin 可上传，所有者或 admin 可删除；
- private：用户私有知识库，仅所有者可检索/管理；
- temp：会话临时知识库，仅当前会话可检索，会话结束后清理。

- GET  /knowledge/list   列出当前用户可见的上传文件（公共 + 自己的私有 + 自己会话的临时）
- POST /knowledge/delete 删除指定文件（仅所有者或 admin）
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.guards import get_current_user
from app.auth.rate_limit import user_rate_limit
from app.auth.delete_guard import PendingDeleteStore
from core.responses import BizException, success
from service.knowledge_service import get_knowledge_service

# 知识库路由：prefix=/knowledge，由 control/app.py 的 app.include_router(knowledge_router) 注册。
# 路由级依赖 user_rate_limit(30, 60)：按登录用户限流 30 次/分钟（读操作量级）。
knowledge_router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge control"],
    dependencies=[Depends(user_rate_limit(30, 60))],
)


class KnowledgeDeleteRequest(BaseModel):
    """知识库文件删除预览请求体模型。

    实例化位置：由前端知识库管理页提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 delete_document_preview 路由的 req 参数。
    字段：
    - stored_name：文件入库时的存储文件名（非原始文件名），来源前端列表项；
      校验规则 1~80 字符。
    """

    stored_name: str = Field(min_length=1, max_length=80)


class KnowledgeDeleteConfirmRequest(BaseModel):
    """知识库文件删除确认请求体模型。

    实例化位置：由前端确认对话框提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 delete_document_confirm 路由的 req 参数。
    字段：
    - stored_name：待删除文件的存储文件名，来源前端；校验规则 1~80 字符，需与令牌记录一致；
    - confirm_token：二次确认令牌，来源 delete/preview 接口下发、前端原样回传。
    """

    stored_name: str = Field(min_length=1, max_length=80)
    confirm_token: str


@knowledge_router.get("/list")
def list_documents(current_user: dict = Depends(get_current_user)):
    """列出当前用户可见的知识库文件。

    HTTP 方法+路径：GET /knowledge/list。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 30 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 知识库管理页）调用，非内部调用。
    参数：
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息（取 user_id 与 role）。
    返回：统一 success 结构的 HTTP JSON 响应，data 为可见文件列表，total 为列表长度。
    可见范围：公共(public) + 当前用户私有(private) + 当前用户所有会话的临时(temp)。
    前端只展示文件名与范围，不展示分块细节。
    """
    user_id = current_user.get("user_id")
    is_admin = current_user.get("role") == "admin"
    data = get_knowledge_service().list_documents(user_id=user_id, is_admin=is_admin)
    return success(data=data, total=len(data))


@knowledge_router.post(
    "/delete/preview",
    dependencies=[Depends(user_rate_limit(10, 60))],
)
def delete_document_preview(req: KnowledgeDeleteRequest, current_user: dict = Depends(get_current_user)):
    """删除知识库文件预览：返回确认令牌，前端需展示确认对话框。

    HTTP 方法+路径：POST /knowledge/delete/preview；本接口仅校验权限并签发令牌，不执行删除。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 30 次/分钟限流叠加本端点
                user_rate_limit(10, 60)（删除预览 10 次/分钟）。
    被谁调用：由 HTTP 客户端（web/frontend 删除文件入口）调用，非内部调用。
    参数：
    - req：删除预览请求体，KnowledgeDeleteRequest 模型，stored_name 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：统一 success 结构，含 confirm_token、stored_name、file_name。
    异常：文件不存在或无权限查看时抛 BizException(404)。
    """
    user_id = current_user.get("user_id")
    is_admin = current_user.get("role") == "admin"
    doc = get_knowledge_service().get_document_info(req.stored_name, user_id=user_id, is_admin=is_admin)
    if doc is None:
        raise BizException("文件不存在或无权限查看", http_status=404)
    token = PendingDeleteStore.create_token(
        user_id=user_id,
        action="delete_document",
        target_info={"stored_name": req.stored_name},
    )
    return success(confirm_token=token, stored_name=req.stored_name, file_name=doc.get("file_name", ""))


@knowledge_router.post(
    "/delete/confirm",
    dependencies=[Depends(user_rate_limit(10, 60))],
)
def delete_document_confirm(req: KnowledgeDeleteConfirmRequest, current_user: dict = Depends(get_current_user)):
    """确认删除知识库文件：验证令牌后执行删除。

    HTTP 方法+路径：POST /knowledge/delete/confirm。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权；路由级 30 次/分钟限流叠加本端点
                user_rate_limit(10, 60)（删除确认 10 次/分钟）。
    被谁调用：由 HTTP 客户端（web/frontend 删除确认对话框）调用，非内部调用。
    参数：
    - req：删除确认请求体，KnowledgeDeleteConfirmRequest 模型，
      stored_name/confirm_token 来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：统一 success 结构，message 为“删除成功”，并展开 service 返回的删除结果字段。
    异常：
    - BizException(400)：确认令牌无效/过期，或令牌与文件名不匹配；
    - BizException(404)：文件不存在或当前用户无权限删除。
    """
    user_id = current_user.get("user_id")
    is_admin = current_user.get("role") == "admin"
    target = PendingDeleteStore.verify_token(req.confirm_token, user_id, "delete_document")
    if target is None:
        raise BizException("确认令牌无效或已过期，请重新操作", http_status=400)
    if target.get("stored_name") != req.stored_name:
        raise BizException("确认令牌与文件名不匹配", http_status=400)
    result = get_knowledge_service().delete_document(req.stored_name, user_id=user_id, is_admin=is_admin)
    if result is None:
        raise BizException("文件不存在或无权限删除", http_status=404)
    return success(message="删除成功", **result)
