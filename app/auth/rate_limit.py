"""
模块名：app.auth.rate_limit

作用：
    访问限流（Redis 滑动窗口，未配置 Redis 时降级为进程内存滑窗）。

主要成员：
    - rate_limit(max_times, window_seconds)：IP + 路径维度限流依赖工厂；
    - user_rate_limit(max_times, window_seconds)：user_id + 路径维度限流依赖工厂；
    - _check_window：内部滑动窗口限流检查（Redis ZSET Lua / 进程内存）；
    - reset_rate_limit_store：清空限流状态（仅测试使用）；
    - _rl_lock / _rl_buckets / _RL_LUA：内存限流桶、线程锁与 Redis Lua 脚本。

被谁使用（均通过 FastAPI Depends 注入，由 FastAPI 按请求实例化调用）：
    - rate_limit：login_control 的 register(5/60s)、login_by_account(10/60s)、
      send_email_code(5/60s)、login_by_email(10/60s)；
    - user_rate_limit：admin_control(30/60s)、chat_control(10/60s)、file_control(5/60s)、
      history_control(60/60s)、knowledge_control(30 或 10/60s)、profile_control(60/60s)、
      login_control.me(30/60s)；
    - reset_rate_limit_store：tests/ 下测试用例（test_boundary/test_adversarial/test_concurrency）。
"""
import threading
import time
from typing import Dict, List

from fastapi import Depends, Request

from app.auth.guards import get_current_user
from core.responses import BizException

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
    from app.infrastructure.redis.redis_client import get_redis

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

    from app.infrastructure.redis.redis_client import get_redis

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

    被哪些路由 Depends 使用：app/api/v1/auth.py 的
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
