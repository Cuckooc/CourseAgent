"""
模块名：core.deps

作用：
    FastAPI 公共依赖层：JWT 身份认证、角色守卫（管理员/禁管理员）与访问限流
    （Redis 滑动窗口，未配置 Redis 时降级为进程内存滑窗）。

主要成员：
    - get_current_user：JWT 鉴权 + 实时回库校验，解析当前登录用户；
    - require_admin：管理员守卫（实时回库核验角色）；
    - forbid_admin：管理员禁用守卫（admin 不允许对话）；
    - rate_limit(max_times, window_seconds)：IP + 路径维度限流依赖工厂；
    - user_rate_limit(max_times, window_seconds)：user_id + 路径维度限流依赖工厂；
    - _check_window：内部滑动窗口限流检查（Redis ZSET Lua / 进程内存）；
    - reset_rate_limit_store：清空限流状态（仅测试使用）；
    - _rl_lock / _rl_buckets / _RL_LUA：内存限流桶、线程锁与 Redis Lua 脚本。

被谁使用（均通过 FastAPI Depends 注入，由 FastAPI 按请求实例化调用）：
    - get_current_user：chat_control / history_control / file_control / review_control /
      profile_control / knowledge_control / login_control.me，并作为 require_admin、
      forbid_admin、user_rate_limit 的子依赖；
    - require_admin：control/admin_control.py 管理路由；
    - forbid_admin：control/chat_control.py 对话路由；
    - rate_limit：login_control 的 register(5/60s)、login_by_account(10/60s)、
      send_email_code(5/60s)、login_by_email(10/60s)；
    - user_rate_limit：admin_control(30/60s)、chat_control(10/60s)、file_control(5/60s)、
      history_control(60/60s)、knowledge_control(30 或 10/60s)、profile_control(60/60s)、
      login_control.me(30/60s)；
    - reset_rate_limit_store：tests/ 下测试用例（test_boundary/test_adversarial/test_concurrency）。
"""
import threading
import time
from typing import Dict, List, Optional

from fastapi import Depends, Header, Request

from core.responses import BizException
from core.security import decode_token


def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, object]:
    """
    JWT 鉴权依赖：从 Authorization: Bearer <token> 解析当前登录用户。

    解析逻辑：
        1. 校验 Authorization 头格式并取 Bearer token，缺失/格式错抛 401；
        2. core.security.decode_token 验签解出 payload（sub=user_id、ver=token 版本）；
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
    from dao.read import Information_Read  # 局部导入避免 core→dao 循环依赖

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
    from dao.read import Information_Read  # 局部导入避免 core→dao 循环依赖

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


# ---------------- 限流（Redis 滑动窗口，未配置时降级进程内存） ----------------
# 多副本部署必须配置 redis_url，使各副本共享限流状态；单机开发自动走内存实现。
# 模块级全局单例：保护 _rl_buckets 的线程锁（仅内存降级分支使用）。
_rl_lock = threading.Lock()
# 模块级全局单例：进程内限流桶 {维度键: [窗口内请求时间戳...]}，
# 仅 Redis 不可用时启用；进程重启即清空，且不跨副本共享。
_rl_buckets: Dict[str, List[float]] = {}

# ZSET 滑动窗口 Lua：先清窗口外成员，再判数量，放行则记录并续期（整段原子执行，避免检查-写入竞态）
# KEYS[1]=rl:<维度键>；ARGV：当前时间(秒)/窗口(秒)/阈值/唯一成员(ns 时间戳)/键 TTL(毫秒)
_RL_LUA = """
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[2]))
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[3]) then
    return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[5]))
return 1
"""


def reset_rate_limit_store() -> None:
    """清空限流窗口（测试用）：内存桶 + Redis rl:* 前缀键。

    被谁调用：tests/test_boundary.py、tests/test_adversarial.py、tests/test_concurrency.py
              每个限流相关用例执行前调用，保证用例间互不污染；业务代码不调用。
    参数：无。
    返回：None。
    """
    with _rl_lock:
        _rl_buckets.clear()
    from core.redis_client import get_redis

    r = get_redis()
    if r is not None:
        for k in r.scan_iter("rl:*"):
            r.delete(k)


def _check_window(key: str, max_times: int, window_seconds: int) -> None:
    """滑动窗口限流检查：超限抛 429。Redis 可用时为共享滑窗，否则进程内存实现。

    功能：Redis 分支用 _RL_LUA 原子完成「清理窗口外成员→计数→放行则记录」；
    Redis 不可用时在 _rl_buckets 内按时间戳过滤后计数（加锁）。
    被谁调用：本模块 rate_limit / user_rate_limit 工厂返回的 _limiter（每个被限请求一次）。
    参数：
        key: 限流维度键（IP:路径 或 user:<id>:路径），由上层工厂拼好；
        max_times: 窗口内最大放行次数，来源为路由装饰器 Depends(rate_limit(...)) 的配置；
        window_seconds: 滑动窗口长度（秒），来源同上。
    返回：None（放行即正常返回）。
    异常：
        BizException(http_status=429)：窗口内计数已达 max_times。
    """
    now = time.time()

    from core.redis_client import get_redis

    r = get_redis()
    if r is not None:
        # Redis 共享滑窗：Lua 原子执行，多副本计数一致；返回 0 表示本次被限流
        allowed = r.eval(
            _RL_LUA,
            1,
            f"rl:{key}",
            f"{now}",
            str(window_seconds),
            str(max_times),
            f"{time.time_ns()}",
            str(window_seconds * 1000),
        )
        if not allowed:
            raise BizException("请求过于频繁，请稍后再试", http_status=429)
        return

    # Redis 降级：进程内存滑窗，加锁后先丢弃窗口外时间戳再计数
    with _rl_lock:
        timestamps = [t for t in _rl_buckets.get(key, []) if now - t < window_seconds]
        if len(timestamps) >= max_times:
            raise BizException("请求过于频繁，请稍后再试", http_status=429)
        timestamps.append(now)
        _rl_buckets[key] = timestamps


def rate_limit(max_times: int, window_seconds: int):
    """
    限流依赖工厂：同一 IP + 路径在 window_seconds 秒内最多访问 max_times 次。
    适用于公开端点（login 等，用户尚未登录）。

    被哪些路由 Depends 使用：control/login_control.py 的
        register(5,60)、login_by_account(10,60)、send_email_code(5,60)、login_by_email(10,60)。

    参数：
        max_times: 窗口内最大请求次数，来源为路由装饰器的静态配置；
        window_seconds: 窗口长度（秒），来源同上。
    返回：
        FastAPI 依赖函数 _limiter（由 FastAPI 按请求注入 Request 调用；无返回值，超限抛 429）。
    """

    def _limiter(request: Request) -> None:
        # 维度：客户端 IP（取自 ASGI 连接）+ 请求路径模板的实际 path
        client_host = request.client.host if request.client else "unknown"
        _check_window(f"{client_host}:{request.url.path}", max_times, window_seconds)

    return _limiter


def user_rate_limit(max_times: int, window_seconds: int):
    """
    登录用户维度限流依赖工厂：同一 user_id + 路径在窗口内最多 max_times 次。
    适用于业务端点（须与 get_current_user 同用；按用户而非 IP 限流，
    避免 NAT/办公网下多用户共享出口 IP 相互误伤）。
    同请求内 get_current_user 依赖缓存生效，JWT 只解析一次。

    被哪些路由 Depends 使用：admin_control(30,60)、chat_control(10,60)、
        file_control(5,60)、history_control(60,60)、knowledge_control(30/10,60)、
        profile_control(60,60)、login_control.me(30,60)。

    参数：
        max_times: 窗口内最大请求次数，来源为路由装饰器静态配置；
        window_seconds: 窗口长度（秒），来源同上。
    返回：
        FastAPI 依赖函数 _limiter（FastAPI 按请求注入 Request 与 current_user；
        无返回值，未登录抛 401、超限抛 429）。
    """

    def _limiter(request: Request, current_user: dict = Depends(get_current_user)) -> None:
        # 维度：登录用户 id（来自 JWT 回库解析）+ 请求路径
        _check_window(f"user:{current_user['user_id']}:{request.url.path}", max_times, window_seconds)

    return _limiter
