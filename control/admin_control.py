"""
模块：admin_control.py
作用：管理员接口，提供 LLM 用量观测、用户列表、角色调整与用户注销（预览/确认两步式）。
主要成员：
- admin_router：管理路由对象（prefix=/admin，路由级登录用户限流）；
- UserRoleRequest / DeactivateConfirmRequest：角色调整、注销确认两个请求体模型；
- llm_usage：按模型聚合的 token 用量快照；
- llm_user_usage：按用户聚合的月度 token 用量快照；
- list_users：全部用户基础信息列表；
- update_user_role：管理员调整用户角色（user ⇄ teacher）；
- deactivate_user_preview / deactivate_user_confirm：注销用户的预览令牌签发与确认执行。
被谁使用：由 control/app.py 通过 `from control.admin_control import admin_router` 导入并
          app.include_router 注册；所有端点再经 require_admin 依赖做 JWT 鉴权 + admin 角色校验；
          路由由 HTTP 客户端（web/frontend 管理后台）调用，非内部调用。
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.audit import audit
from core.deps import require_admin, user_rate_limit
from core.delete_guard import PendingDeleteStore
from core.responses import BizException, success
from core.usage import usage_snapshot, user_usage_snapshot
from dao.read import Information_Read
from service import admin_user_service

# 管理路由：prefix=/admin；由 control/app.py 的 app.include_router(admin_router) 注册。
# 路由级依赖 user_rate_limit(30, 60)：按登录用户限流 30 次/60 秒；
# 各端点再以 require_admin 依赖做 JWT 鉴权 + admin 角色校验。
admin_router = APIRouter(
    prefix="/admin", tags=["Admin Control"],
    dependencies=[Depends(user_rate_limit(30, 60))],
)


@admin_router.get("/llm/usage")
def llm_usage(current_user: dict = Depends(require_admin)):
    """按模型聚合的 LLM token 用量快照（requests/prompt_tokens/completion_tokens）。

    HTTP 方法+路径：GET /admin/llm/usage。
    鉴权与限流：require_admin 依赖完成 JWT 鉴权并要求 admin 角色；路由级 30 次/60 秒限流。
    被谁调用：由 HTTP 客户端（web/frontend 管理后台）调用，非内部调用。
    参数：
    - current_user：Depends(require_admin) 注入的当前登录管理员信息（含 user_id/user_name/role）。
    返回：统一 success 结构的 HTTP JSON 响应，usage 字段为按模型聚合的用量数据，仅 admin 可见。
    """
    return success(usage=usage_snapshot())


@admin_router.get("/llm/usage/users")
def llm_user_usage(
    month: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}$"),
    current_user: dict = Depends(require_admin),
):
    """按用户聚合的月度 token 用量快照。

    HTTP 方法+路径：GET /admin/llm/usage/users。
    鉴权与限流：require_admin 依赖完成 JWT 鉴权并要求 admin 角色；路由级 30 次/60 秒限流。
    被谁调用：由 HTTP 客户端（web/frontend 管理后台）调用，非内部调用。
    参数：
    - month：查询字符串参数，格式 YYYY-MM（正则 ^\\d{4}-\\d{2}$），来源前端；不传默认当前月；
    - current_user：Depends(require_admin) 注入的当前登录管理员信息。
    返回：统一 success 结构的 HTTP JSON 响应，含 month 与 rows；
          rows 元素为 {user_id, user_name, email, role, requests,
          prompt_tokens, completion_tokens}，按总 token 降序；已注销用户用户名显示“已注销用户”。
          仅 admin 角色可见。
    """
    target_month = month or datetime.now().strftime("%Y-%m")
    usage = user_usage_snapshot(target_month)
    user_map = Information_Read().get_by_ids(list(usage.keys())) if usage else {}
    rows = []
    for uid, stat in usage.items():
        info = user_map.get(int(uid), {})
        rows.append({
            "user_id": uid,
            "user_name": info.get("user_name") or "已注销用户",
            "email": info.get("email") or "",
            "role": info.get("role") or "user",
            "requests": stat.get("requests", 0),
            "prompt_tokens": stat.get("prompt_tokens", 0),
            "completion_tokens": stat.get("completion_tokens", 0),
        })
    # 按总 token 降序
    rows.sort(key=lambda r: r["prompt_tokens"] + r["completion_tokens"], reverse=True)
    return success(month=target_month, rows=rows)


@admin_router.get("/users")
def list_users(current_user: dict = Depends(require_admin)):
    """全部用户基础信息列表（id/用户名/邮箱/角色，不含密码）。

    HTTP 方法+路径：GET /admin/users。
    鉴权与限流：require_admin 依赖完成 JWT 鉴权并要求 admin 角色；路由级 30 次/60 秒限流。
    被谁调用：由 HTTP 客户端（web/frontend 管理后台）调用，非内部调用。
    参数：
    - current_user：Depends(require_admin) 注入的当前登录管理员信息。
    返回：统一 success 结构的 HTTP JSON 响应，users 字段为全部用户的基础信息列表。仅 admin 可见。
    """
    return success(users=admin_user_service.list_all_users())


class UserRoleRequest(BaseModel):
    """角色调整请求体模型。

    实例化位置：由前端管理后台提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 update_user_role 路由的 req 参数。
    字段：
    - role：目标角色，来源前端；无 Field 长度约束，路由内二次校验仅允许 user/teacher
      （admin 由运维侧直接管理 DB，不接受本接口授予或回收）。
    """

    role: str


class DeactivateConfirmRequest(BaseModel):
    """注销用户确认请求体模型。

    实例化位置：由前端确认对话框提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 deactivate_user_confirm 路由的 req 参数。
    字段：
    - confirm_token：二次确认令牌，来源 deactivate_user_preview 接口下发、前端原样回传；
      由 PendingDeleteStore 校验签发者、动作类型与有效期。
    """

    confirm_token: str


@admin_router.put("/users/{user_id}/role")
def update_user_role(
    user_id: int,
    req: UserRoleRequest,
    current_user: dict = Depends(require_admin),
):
    """管理员调整用户角色（user ⇄ teacher），即时生效（鉴权以库内 role 为准）。

    HTTP 方法+路径：PUT /admin/users/{user_id}/role。
    鉴权与限流：require_admin 依赖完成 JWT 鉴权并要求 admin 角色；路由级 30 次/60 秒限流。
    被谁调用：由 HTTP 客户端（web/frontend 管理后台）调用，非内部调用。
    参数：
    - user_id：路径参数，目标用户 ID，来源前端请求路径；
    - req：角色调整请求体，UserRoleRequest 模型，role 来自前端 JSON；
    - current_user：Depends(require_admin) 注入的当前登录管理员信息。
    返回：统一 success 结构的 HTTP JSON 响应，message 描述调整结果；并写 user_role_changed 审计事件。
    异常：
    - BizException(400)：目标角色非 user/teacher，或试图修改自己的角色；
    - BizException(404)：目标用户不存在或已注销；
    - BizException(403)：目标账号为 admin（不允许经本接口修改）。

    - 目标角色仅限 user/teacher：teacher 可上传公共知识库，属特权角色，
      由管理员授予；admin 角色的授予/回收不走本接口（与注销 admin 同规则）；
    - 不能修改自己（防止误操作失去管理权限）；
    - 不能修改其他 admin 账号的角色。
    """
    role = (req.role or "").strip().lower()
    if role not in ("user", "teacher"):
        raise BizException("角色仅支持 user / teacher", http_status=400)
    if user_id == current_user.get("user_id"):
        raise BizException("不能修改当前登录账号的角色", http_status=400)
    target = Information_Read().get_by_id(user_id)
    if not target:
        raise BizException("用户不存在或已注销", http_status=404)
    if target.get("role") == "admin":
        raise BizException("不能修改管理员账号的角色", http_status=403)
    admin_user_service.update_role(user_id, role)
    audit(
        "user_role_changed",
        actor={"user_id": current_user.get("user_id"), "user_name": current_user.get("user_name", ""), "role": "admin"},
        target=user_id,
        changed_role=role,
        target_user_name=target.get("user_name", ""),
    )
    return success(message=f"已将用户 {target.get('user_name', user_id)} 的角色调整为 {'教师' if role == 'teacher' else '普通用户'}")


@admin_router.post("/users/{user_id}/deactivate/preview")
def deactivate_user_preview(
    user_id: int,
    current_user: dict = Depends(require_admin),
):
    """注销用户预览：返回确认令牌和用户信息，前端需展示确认对话框。

    HTTP 方法+路径：POST /admin/users/{user_id}/deactivate/preview。
    鉴权与限流：require_admin 依赖完成 JWT 鉴权并要求 admin 角色；路由级 30 次/60 秒限流。
    被谁调用：由 HTTP 客户端（web/frontend 管理后台）调用，非内部调用；本接口仅签发令牌、不执行注销。
    参数：
    - user_id：路径参数，待注销目标用户 ID，来源前端请求路径；
    - current_user：Depends(require_admin) 注入的当前登录管理员信息。
    返回：统一 success 结构的 HTTP JSON 响应，含 confirm_token（供 confirm 接口回传）、
          target_user_id、target_user_name。
    异常：
    - BizException(400)：试图注销当前登录的管理员自己；
    - BizException(404)：目标用户不存在或已注销；
    - BizException(403)：目标账号为 admin。
    """
    if user_id == current_user.get("user_id"):
        raise BizException("不能注销当前登录的管理员账号", http_status=400)
    target = Information_Read().get_by_id(user_id)
    if not target:
        raise BizException("用户不存在或已注销", http_status=404)
    if target.get("role") == "admin":
        raise BizException("不能注销管理员账号", http_status=403)
    token = PendingDeleteStore.create_token(
        user_id=current_user.get("user_id"),
        action="deactivate_user",
        target_info={"target_user_id": user_id, "target_user_name": target.get("user_name", "")},
    )
    return success(
        confirm_token=token,
        target_user_id=user_id,
        target_user_name=target.get("user_name", ""),
    )


@admin_router.post("/users/{user_id}/deactivate/confirm")
def deactivate_user_confirm(
    user_id: int,
    req: DeactivateConfirmRequest,
    current_user: dict = Depends(require_admin),
):
    """确认注销用户：验证令牌后执行注销。

    HTTP 方法+路径：POST /admin/users/{user_id}/deactivate/confirm。
    鉴权与限流：require_admin 依赖完成 JWT 鉴权并要求 admin 角色；路由级 30 次/60 秒限流。
    被谁调用：由 HTTP 客户端（web/frontend 管理后台确认对话框）调用，非内部调用。
    参数：
    - user_id：路径参数，待注销目标用户 ID，来源前端请求路径；
    - req：确认请求体，DeactivateConfirmRequest 模型，confirm_token 来自 preview 接口下发；
    - current_user：Depends(require_admin) 注入的当前登录管理员信息。
    返回：统一 success 结构的 HTTP JSON 响应，message 告知注销结果；并写 user_deactivated 审计事件。
    异常：
    - BizException(400)：注销自己、确认令牌无效/过期、或令牌与目标用户不匹配；
    - BizException(404)：目标用户不存在或已注销；
    - BizException(403)：目标账号为 admin。
    """
    if user_id == current_user.get("user_id"):
        raise BizException("不能注销当前登录的管理员账号", http_status=400)
    target_info = PendingDeleteStore.verify_token(req.confirm_token, current_user.get("user_id"), "deactivate_user")
    if target_info is None:
        raise BizException("确认令牌无效或已过期，请重新操作", http_status=400)
    if target_info.get("target_user_id") != user_id:
        raise BizException("确认令牌与目标用户不匹配", http_status=400)
    target = Information_Read().get_by_id(user_id)
    if not target:
        raise BizException("用户不存在或已注销", http_status=404)
    if target.get("role") == "admin":
        raise BizException("不能注销管理员账号", http_status=403)
    admin_user_service.deactivate_user(user_id)
    audit(
        "user_deactivated",
        actor={"user_id": current_user.get("user_id"), "user_name": current_user.get("user_name", ""), "role": "admin"},
        target=user_id,
        target_user_name=target.get("user_name", ""),
    )
    return success(message=f"已注销用户 {target.get('user_name', user_id)}")

