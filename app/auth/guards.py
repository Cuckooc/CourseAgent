"""
模块名：app.auth.guards

作用：
    FastAPI 身份认证与角色守卫依赖：JWT 身份解析、管理员守卫、管理员禁用守卫。
    （访问限流已拆至 app.auth.rate_limit。）

主要成员：
    - get_current_user：JWT 鉴权 + 实时回库校验，解析当前登录用户；
    - require_admin：管理员守卫（实时回库核验角色）；
    - forbid_admin：管理员禁用守卫（admin 不允许对话）。

被谁使用（均通过 FastAPI Depends 注入，由 FastAPI 按请求实例化调用）：
    - get_current_user：chat_control / history_control / file_control / review_control /
      profile_control / knowledge_control / login_control.me，并作为 require_admin、
      forbid_admin、user_rate_limit（app.auth.rate_limit）的子依赖；
    - require_admin：control/admin_control.py 管理路由；
    - forbid_admin：control/chat_control.py 对话路由。
"""
from typing import Dict, Optional

from fastapi import Depends, Header

from app.auth.authentication import decode_token
from core.responses import BizException


def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, object]:
    """
    JWT 鉴权依赖：从 Authorization: Bearer <token> 解析当前登录用户。

    解析逻辑：
        1. 校验 Authorization 头格式并取 Bearer token，缺失/格式错抛 401；
        2. app.auth.authentication.decode_token 验签解出 payload（sub=user_id、ver=token 版本）；
        3. 实时回库（dao.read.Information_Read.get_by_id）确认用户仍存在（fail-closed）；
        4. 校验 payload.ver 与库内 token_version 一致（单点互踢，旧 token 立即失效）；
        5. 角色以数据库为准（管理员调整即时生效）。
    被哪些路由 Depends 使用：chat_control / history_control / file_control / review_control /
        profile_control / knowledge_control 等所有需登录端点，以及 login_control.me；
        同时是 require_admin / forbid_admin / user_rate_limit 的上游子依赖（FastAPI 同请求缓存，只解析一次）。

    参数：
        authorization: HTTP 请求头 Authorization，格式 "Bearer <jwt>"，由 FastAPI 自动注入。
    返回：
        Dict：{"user_id": int, "user_name": str, "role": str}，去向为各业务端点与下游 service；
        业务接口一律以这里的 user_id 为准，禁止信任请求体中的 user_id。
    异常：
        BizException(http_status=401)：无头/格式错、token 无效、用户不存在或被删除、
        token 版本过期（被单点互踢）。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise BizException("未登录或登录已过期", http_status=401)
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    try:
        user_id = int(payload.get("sub", 0))
    except (TypeError, ValueError):
        raise BizException("无效的登录凭证", http_status=401)
    if not user_id:
        raise BizException("无效的登录凭证", http_status=401)
    # 实时回库校验用户存在性：用户被删除后，其未过期的历史 token 立即失效。
    # get_by_id 查询异常或用户不存在均返回 None → 统一按登录失效处理（fail-closed）。
    from app.infrastructure.persistence.repositories.read import Information_Read  # 局部导入避免 core→dao 循环依赖

    row = Information_Read().get_by_id(user_id)
    if not row:
        raise BizException("登录已失效，请重新登录", http_status=401)
    # 单点互踢：JWT 的 ver 声明必须等于库内 token_version，否则视为旧 token 立即失效。
    # 旧 token 无 ver 字段时 payload.get("ver", 0) 默认 0，与库内 DEFAULT 0 相等，平滑过渡。
    if int(payload.get("ver", 0)) != int(row.get("token_version") or 0):
        raise BizException("登录已失效，请重新登录", http_status=401)
    # 角色以数据库为准（管理员调整角色即时生效，旧 token 的 role 声明不再被信任）；
    # 库内无 role 时兼容历史数据按普通用户处理
    return {
        "user_id": user_id,
        "user_name": row.get("user_name") or payload.get("user_name", ""),
        "role": row.get("role") or "user",
    }


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    管理员守卫：角色非 admin 返回 403（挂在 /admin/* 管理端点）。

    鉴权逻辑：先经 get_current_user 完成登录校验（FastAPI 注入当前用户），
    再实时回库（Information_Read.get_by_ids）核验数据库中的当前角色：
    管理员被降权后，未过期的旧 token 不得继续行使管理权限。
    数据库异常时 get_by_ids 返回空 → 校验拒绝（fail-closed）。
    被哪些路由 Depends 使用：control/admin_control.py 管理员路由组。

    参数：
        current_user: 上游 get_current_user 依赖的返回值（含 user_id/role），由 FastAPI 注入。
    返回：
        dict：校验通过时原样返回 current_user（去向：管理端点继续使用其 user_id）。
    异常：
        BizException(http_status=403)：当前角色不是 admin 或库内角色已非 admin；
        get_current_user 失败时抛 401。
    """
    if current_user.get("role") != "admin":
        raise BizException("没有权限执行此操作", http_status=403)
    from app.infrastructure.persistence.repositories.read import Information_Read  # 局部导入避免 core→dao 循环依赖

    uid = int(current_user["user_id"])
    row = Information_Read().get_by_ids([uid]).get(uid)
    if not row or row.get("role") != "admin":
        raise BizException("没有权限执行此操作", http_status=403)
    return current_user


def forbid_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    管理员禁用守卫：admin 为纯管理角色（无对话/私有知识库/画像）。

    鉴权逻辑：经 get_current_user 解析当前用户后，role == "admin" 即拒绝；
    teacher/user 不受影响。
    被哪些路由 Depends 使用：control/chat_control.py 对话路由组（与 user_rate_limit 并列）。

    参数：
        current_user: 上游 get_current_user 依赖的返回值，由 FastAPI 注入。
    返回：
        dict：非 admin 用户原样返回 current_user。
    异常：
        BizException(http_status=403)：管理员账号访问对话功能；
        get_current_user 失败时抛 401。
    """
    if current_user.get("role") == "admin":
        raise BizException("管理员账号不支持对话功能", http_status=403)
    return current_user
