"""
模块名：app.auth.authentication（安全模块：密码哈希与 JWT 令牌）。

作用：
    - 密码安全：使用 bcrypt 对用户口令做单向哈希与校验；
    - 令牌安全：签发与解析 JWT access token，承载用户身份、角色与单点互踢版本号。

密钥/配置来源（env/config.env 或同名环境变量，经 core.config.settings 读取）：
    - jwt_secret -> settings.JWT_SECRET：JWT 签名/验签密钥，必须显式配置，
      缺失时配置加载直接失败，禁止携带默认值上线；
    - jwt_algorithm -> settings.JWT_ALGORITHM：签名算法，默认 HS256；
    - access_token_expire_minutes -> settings.ACCESS_TOKEN_EXPIRE_MINUTES：
      token 有效期，默认 720 分钟。

token 去向与校验链路：
    登录成功（util/user.py 的 login_by_username / login_by_email_code）调用
    create_access_token 签发 -> token 放入登录响应体 access_token 返回前端
    -> 前端存储后在后续请求的 Authorization: Bearer <token> 头携带
    -> core/deps.py 的 get_current_user 依赖取出 Bearer token 并调用
    decode_token 验签/验过期 -> 再回库校验用户存在性与 token_version(ver)
    单点互踢、以库内角色为准 -> 解析出的 user_id 供各业务端点使用。

主要成员：
    hash_password / verify_password / create_access_token / decode_token。

被谁使用（Grep）：
    - util/user.py：注册哈希、登录校验与自动升级哈希、两种登录方式签发 token；
    - core/deps.py：get_current_user 调用 decode_token；
    - tests/conftest.py、test_api_rbac.py、test_concurrency.py 构造测试 token。
"""
import time
from typing import Any, Dict

import bcrypt
import jwt

from core.config import settings
from core.responses import BizException

# bcrypt 仅支持最长 72 字节密码，超出部分截断（bcrypt 标准行为）
_BCRYPT_MAX_BYTES = 72

# 缓解 PyJWT<2.10 对未消费 payload 段的无界 Base64 解码 DoS（GHSA-w7vc-732c-9m39，
# 修复版要求 py>=3.9 而无法升级）：超长 token 直接拒绝。
# 正常 token 仅含 sub/user_name/role/iat/exp，实际长度 <1KB，8KB 阈值足够宽裕。
_MAX_TOKEN_CHARS = 8192


def hash_password(plain_password: str) -> str:
    """生成 bcrypt 密码哈希。

    功能：对 UTF-8 编码后的明文口令截断至 bcrypt 上限 72 字节，
    随机加盐后返回可入库存储的哈希字符串。
    被谁调用：util/user.py 的 register_user（注册入库）、
        login_by_username（历史明文密码登录成功后自动升级哈希）；
        tests/conftest.py、test_concurrency.py 构造测试用户。
    参数：
        plain_password: 用户明文口令，来源为注册/修改密码 HTTP 请求体。
    返回：str，bcrypt 哈希（$2b$...），去向为 user_information.user_pwd 字段。
    """
    pw = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。

    被谁调用：util/user.py 的 login_by_username（账号密码登录）。
    参数：
        plain_password: 登录请求中的明文口令（HTTP 请求体）；
        hashed_password: 库内 user_pwd（MySQL，$2 开头的 bcrypt 哈希）。
    返回：bool；匹配返回 True；哈希为空、格式非法或不匹配均返回 False
        （不抛异常，便于登录流程统一走“用户名或密码错误”分支）。
    """
    if not hashed_password:
        return False
    try:
        pw = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pw, hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        # 库内哈希损坏/格式非法时按不匹配处理，避免异常向上泄露
        return False


def create_access_token(user_id: int, user_name: str, role: str = "user", expire_minutes: int = None, ver: int = 0) -> str:
    """签发 JWT access token（HS256，密钥来自 settings.JWT_SECRET）。

    功能：组装 sub/user_name/role/ver/iat/exp 声明并用 jwt_secret 签名。
    role 供前端导航过滤与服务端管理端点鉴权；ver 为单点互踢版本号，
    与库内 token_version 比对，不等则 token 立即失效。旧 token 无 ver
    字段时 deps 默认 0，与库内 DEFAULT 0 相等，平滑过渡不被一次性踢出。

    被谁调用：util/user.py 的 login_by_username、login_by_email_code
        （两种登录成功后签发）；tests 下构造认证请求。
    参数：
        user_id: 用户 id，来源为 user_information 主键；写入 sub 声明；
        user_name: 用户名，来源为库内记录，写入 user_name 声明；
        role: 角色（user/teacher/admin），来源为库内角色，默认 "user"；
        expire_minutes: 有效期分钟数；None 时取
            settings.ACCESS_TOKEN_EXPIRE_MINUTES（access_token_expire_minutes）；
        ver: 单点互踢版本号，来源为登录时自增后的库内 token_version。
    返回：str，签名后的 JWT；去向为登录响应体 access_token 字段
        （token_type="bearer"），前端存储后以 Authorization: Bearer 头回传。
    """
    now = int(time.time())
    # 显式有效期优先，否则取配置的默认有效期
    ttl = expire_minutes if expire_minutes is not None else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "user_name": user_name,
        "role": role,
        "ver": ver,
        "iat": now,
        "exp": now + ttl * 60,
    }
    # 签名密钥来自 env/config.env 的 jwt_secret（settings.JWT_SECRET）
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> Dict[str, Any]:
    """解析并校验 JWT（验签 + 验过期，密钥来自 settings.JWT_SECRET）。

    功能：先拦截空 token 与超长 token（防 DoS），再用 jwt_secret 验签解码。
    被谁调用：core/deps.py 的 get_current_user —— 即 Authorization 头
        Bearer token 的统一校验入口（几乎所有需登录的 FastAPI 端点都依赖它）。
    参数：
        token: 前端请求 Authorization: Bearer 头中携带的 JWT 字符串（HTTP 请求头）。
    返回：Dict[str, Any]，解码后的 payload（含 sub/user_name/role/ver/iat/exp），
        去向为 get_current_user 的用户身份与单点互踢校验。
    异常：
        token 为空或超过 8192 字符、签名无效 -> BizException(401, "无效的登录凭证")；
        token 过期 -> BizException(401, "登录已过期，请重新登录")；
        两类异常最终由 control/app.py 全局处理器转为统一失败响应。
    """
    if not token or len(token) > _MAX_TOKEN_CHARS:
        raise BizException("无效的登录凭证", http_status=401)
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise BizException("登录已过期，请重新登录", http_status=401)
    except jwt.PyJWTError:
        # 签名错误、格式非法等统一按无效凭证处理，不向客户端区分具体原因
        raise BizException("无效的登录凭证", http_status=401)
