"""
模块：login_control.py
作用：认证相关 HTTP 接口，提供注册、账号密码登录、邮箱验证码登录及当前登录身份查询。
主要成员：
- login_router：认证路由对象（prefix=/login）；
- RegisterRequest / LoginByUsernameRequest / SendCodeRequest / LoginByEmailRequest：四个请求体模型；
- register：用户注册（密码复杂度校验）；
- login_by_account：账号密码登录（JWT + 失败锁定）；
- send_email_code：发送邮箱一次性验证码；
- login_by_email：邮箱验证码登录（JWT）；
- me：查询当前登录身份（实时回库）。
被谁使用：由 control/app.py 通过 `from app.api.v1.auth import login_router` 导入并
          app.include_router 注册；路由由 HTTP 客户端（web/frontend）调用，非内部调用。
说明：登录成功签发 JWT，后续接口通过 Authorization: Bearer <token> 鉴权；
      认证接口启用限流防暴力破解与验证码轰炸；注册/登录失败/锁定事件写入独立审计日志。
"""
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.application.auth.user import UserInformation
from app.application.ports.persistence import get_session_dao, get_read_dao
from app.auth import account_guard
from core.audit import audit
from core.config import settings
from app.auth.guards import get_current_user
from app.auth.rate_limit import rate_limit, user_rate_limit
from core.responses import success

# 认证路由：prefix=/login，由 control/app.py 的 app.include_router(login_router) 注册
login_router = APIRouter(prefix="/login", tags=["login control"])

# 密码复杂度正则常量：注册时由 RegisterRequest.password_policy 校验使用
# 密码复杂度：至少 8 位且同时包含字母和数字
_PASSWORD_RE_LETTER = re.compile(r"[A-Za-z]")
_PASSWORD_RE_DIGIT = re.compile(r"\d")


class RegisterRequest(BaseModel):
    """注册请求体模型。

    实例化位置：不由业务代码显式实例化，由前端注册表单提交的 JSON 请求体经
    FastAPI/Pydantic 自动校验后实例化，作为 register 路由的 req 参数传入。
    字段：
    - user_name：用户名，来源前端注册表单；校验规则 1~20 个字符；
    - user_pwd：登录密码明文（仅 HTTPS 传输，落库前由业务层哈希），来源前端；
      校验规则 8~64 个字符，且经 password_policy 校验必须同时含字母和数字；
    - email：邮箱地址，来源前端注册表单；校验规则 3~255 个字符。
    """

    user_name: str = Field(min_length=1, max_length=20)
    user_pwd: str = Field(min_length=8, max_length=64)
    email: str = Field(min_length=3, max_length=255)

    @field_validator("user_pwd")
    @classmethod
    def password_policy(cls, v: str) -> str:
        """密码复杂度校验器：密码必须同时包含字母与数字，否则抛出 ValueError（经全局处理器返回 422）。

        参数：v 为待校验的密码明文，来源请求体 user_pwd 字段。
        返回：校验通过时原样返回密码字符串。
        异常：缺少字母或数字时抛出 ValueError。
        """
        if not _PASSWORD_RE_LETTER.search(v) or not _PASSWORD_RE_DIGIT.search(v):
            raise ValueError("密码需至少8位且同时包含字母和数字")
        return v


class LoginByUsernameRequest(BaseModel):
    """账号密码登录请求体模型。

    实例化位置：由前端登录表单的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 login_by_account 路由的 req 参数。
    字段：
    - username：用户名，来源前端登录表单；校验规则 1~20 个字符；
    - password：密码明文，来源前端登录表单；校验规则 1~64 个字符。
    """

    username: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=64)


class SendCodeRequest(BaseModel):
    """发送邮箱验证码请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 send_email_code 路由的 req 参数。
    字段：
    - email：接收验证码的邮箱地址，来源前端；校验规则 3~255 个字符。
    """

    email: str = Field(min_length=3, max_length=255)


class LoginByEmailRequest(BaseModel):
    """邮箱验证码登录请求体模型。

    实例化位置：由前端的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 login_by_email 路由的 req 参数。
    字段：
    - email：登录邮箱，来源前端；校验规则 3~255 个字符；
    - code：一次性邮箱验证码，来源前端输入；校验规则 4~8 个字符。
    """

    email: str = Field(min_length=3, max_length=255)
    code: str = Field(min_length=4, max_length=8)


@login_router.post("/register")
def register(req: RegisterRequest, _=Depends(rate_limit(5, 60))):
    """用户注册：校验用户名/邮箱唯一性后创建账号，并记录注册审计事件。

    HTTP 方法+路径：POST /login/register。
    鉴权与限流：无需登录；rate_limit(5, 60) 依赖做全局限流（5 次/60 秒，防恶意注册）。
    被谁调用：由 HTTP 客户端（web/frontend 注册页）调用，非内部调用。
    参数：
    - req：注册请求体，RegisterRequest 模型，由前端 JSON 经 FastAPI 自动校验实例化；
    - _：rate_limit 依赖注入的限流占位返回值，仅起限流作用，函数内不使用。
    返回：dict，直接作为 HTTP JSON 响应给前端；成功时为业务层 register_user 的成功结果，
          用户名或邮箱已存在时返回 {status:"fail", message}。
    """
    user_service = UserInformation()
    if user_service.check_user(req.model_dump()):
        return {"status": "fail", "message": "用户名或邮箱已被注册"}
    result = user_service.register_user(req.model_dump())
    if result.get("status") == "success":
        audit("user_registered", actor={"user_name": req.user_name}, target=req.user_name)
    return result


@login_router.post("/account")
def login_by_account(req: LoginByUsernameRequest, _=Depends(rate_limit(10, 60))):
    """
    通过账号密码登录（JWT）。

    HTTP 方法+路径：POST /login/account。
    鉴权与限流：无需登录；rate_limit(10, 60) 全局限流（10 次/60 秒）。
    被谁调用：由 HTTP 客户端（web/frontend 登录页）调用，非内部调用。
    参数：
    - req：账号密码登录请求体，LoginByUsernameRequest 模型，由前端 JSON 自动校验实例化；
    - _：rate_limit 限流依赖的占位返回值，函数内不使用。
    返回：dict，直接作为 HTTP JSON 响应给前端；成功时含 JWT 及该用户的会话列表 sessions，
          失败或锁定时返回 {status:"fail", message}。
    失败锁定：失败计数达阈值（LOGIN_MAX_FAILURES / LOGIN_LOCK_WINDOW_SECONDS，
          Redis 降级时放行）后临时锁定该用户名；用本次 INCR 返回值即时判定锁定，
          消除并发批次「检查先于计数」的竞态。
    """
    if account_guard.is_locked(req.username):
        audit(
            "login_locked",
            actor={"user_name": req.username},
            target=req.username,
            result="blocked",
        )
        return {"status": "fail", "message": "失败次数过多，账号已临时锁定，请稍后再试"}

    user_service = UserInformation()
    result = user_service.login_by_username(req.username, req.password)
    if result["status"] == "success":
        account_guard.reset(req.username)
        session_dao = get_session_dao()
        result["sessions"] = session_dao.get_session_list(result["user_id"])
    else:
        # 用本次 INCR 返回值即时判定锁定，消除并发批次「检查先于计数」的竞态：
        # 同一批并发错误登录中，计数达到阈值的那个请求立即收到锁定响应。
        fail_count = account_guard.record_failure(req.username)
        audit("login_failed", actor={"user_name": req.username}, target=req.username, result="fail")
        if fail_count and fail_count >= settings.LOGIN_MAX_FAILURES:
            return {"status": "fail", "message": "失败次数过多，账号已临时锁定，请稍后再试"}
    return result


@login_router.post("/email/code")
def send_email_code(req: SendCodeRequest, _=Depends(rate_limit(5, 60))):
    """
    发送邮箱验证码（开发环境验证码输出在服务日志中）。

    HTTP 方法+路径：POST /login/email/code。
    鉴权与限流：无需登录；rate_limit(5, 60) 全局限流（5 次/60 秒，防验证码轰炸）。
    被谁调用：由 HTTP 客户端（web/frontend 登录页）调用，非内部调用。
    参数：
    - req：发送验证码请求体，SendCodeRequest 模型，邮箱地址来自前端 JSON；
    - _：rate_limit 限流依赖的占位返回值，函数内不使用。
    返回：dict，直接作为 HTTP JSON 响应给前端，表示验证码发送结果。
    """
    return UserInformation().send_email_code(req.email)


@login_router.get("/me")
def me(current_user: dict = Depends(get_current_user), _=Depends(user_rate_limit(30, 60))):
    """当前登录身份（实时回库）：前端启动/聚焦时同步，管理员调整角色后无需重登即生效。

    HTTP 方法+路径：GET /login/me。
    鉴权与限流：get_current_user 依赖解析 Authorization Bearer JWT 完成登录鉴权并注入用户信息；
                user_rate_limit(30, 60) 按登录用户限流（30 次/60 秒）。
    被谁调用：由 HTTP 客户端（web/frontend）调用，非内部调用。
    参数：
    - current_user：Depends 注入的当前登录用户信息字典（来自 JWT，含 user_id 等）；
    - _：user_rate_limit 限流依赖的占位返回值，函数内不使用。
    返回：统一 success 结构的 HTTP JSON 响应，含 user_id/user_name/email/role，
          其中 user_name/email/role 实时查询数据库，库内缺失时回退 JWT 字段或默认值。
    """
    info = get_read_dao().get_by_id(current_user["user_id"]) or {}
    return success(
        user_id=current_user["user_id"],
        user_name=info.get("user_name") or current_user.get("user_name", ""),
        email=info.get("email") or "",
        role=info.get("role") or "user",
    )


@login_router.post("/email")
def login_by_email(req: LoginByEmailRequest, _=Depends(rate_limit(10, 60))):
    """
    通过邮箱 + 一次性验证码登录（JWT），替代旧的邮箱免密登录。

    HTTP 方法+路径：POST /login/email。
    鉴权与限流：无需登录；rate_limit(10, 60) 全局限流（10 次/60 秒）。
    被谁调用：由 HTTP 客户端（web/frontend 登录页）调用，非内部调用。
    参数：
    - req：邮箱登录请求体，LoginByEmailRequest 模型，邮箱与验证码来自前端 JSON；
    - _：rate_limit 限流依赖的占位返回值，函数内不使用。
    返回：dict，直接作为 HTTP JSON 响应给前端；成功时含 JWT 及该用户会话列表 sessions，
          验证码错误/过期时返回业务层给出的失败结构。
    """
    user_service = UserInformation()
    result = user_service.login_by_email_code(req.email, req.code)
    if result["status"] == "success":
        session_dao = get_session_dao()
        result["sessions"] = session_dao.get_session_list(result["user_id"])
    return result
