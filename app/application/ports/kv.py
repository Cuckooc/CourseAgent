"""
模块名：app.application.ports.kv

作用：
    Redis 键值能力的运行时装配点（服务定位器）。application/domain/auth 层
    需要共享缓存、限流、验证码、分布式锁等状态时统一经本模块获取客户端与
    周期锁原语，不直接 import app.infrastructure.redis.*（分层守卫 RULES
    禁止业务层依赖基础设施）；具体实现由组合根 app/api/deps.py 在应用启动
    时注册（Port↔Adapter 装配）。

主要成员：
    - register_redis_provider(provider)：组合根注册 Redis 客户端提供者
      （启动时一次）；测试可注入假实现；
    - get_redis()：获取共享 Redis 客户端，语义与
      infrastructure.redis.redis_client.get_redis 完全一致——未配置或
      连接失败时返回 None，调用方据此降级为进程内存实现；
    - register_cycle_lock(lock)：组合根注册周期任务抢锁函数；
    - try_acquire_cycle_lock(key, interval_seconds)：周期任务跨进程互斥，
      语义与 infrastructure.redis.locks.try_acquire_cycle_lock 完全一致
      （抢到锁或 Redis 不可用返回 True，抢不到返回 False 跳过本周期）。

被谁使用：
    - 调用方：app/auth/{account_guard,delete_guard,rate_limit,verification}.py、
      app/application/{admin/admin_user_service,files/file_service}.py、
      app/domain/memory/{context_memory,profile_service,session_keyword_service,
      session_rollover,short_term,long_term}.py；
    - 装配方：app/api/deps.py（import 时执行 register_redis_provider /
      register_cycle_lock）。
"""
from typing import Any, Callable, Optional

__all__ = [
    "get_redis",
    "register_redis_provider",
    "register_cycle_lock",
    "try_acquire_cycle_lock",
]


# 已注册的 Redis 客户端提供者（组合根装配前为 None）；
# 签名为 () -> Optional[redis.Redis]，与 infrastructure 实现一致
_redis_provider: Optional[Callable[[], Any]] = None

# 已注册的周期锁函数（组合根装配前为 None）；
# 签名为 (key: str, interval_seconds: float) -> bool
_cycle_lock: Optional[Callable[[str, float], bool]] = None


def register_redis_provider(provider: Callable[[], Any]) -> None:
    """注册 Redis 客户端提供者（组合根在启动时调用一次；测试可注入假实现）。

    参数：provider —— 与 infrastructure.redis.redis_client.get_redis 同签名的
          无参函数（-> Optional[redis.Redis]，不可用返回 None）。
    """
    global _redis_provider
    _redis_provider = provider


def get_redis() -> Any:
    """获取共享 Redis 客户端（语义与行为同 infrastructure 实现）。

    返回：连通时返回 redis.Redis 实例（decode_responses=True）；未配置
          redis_url 或连接失败时返回 None，调用方据此走进程内存降级。
    异常：RuntimeError —— 组合根尚未装配（属启动期配置错误，不应在运行期出现）。
    """
    if _redis_provider is None:
        raise RuntimeError(
            "Redis 能力未装配：组合根 app/api/deps.py 未注册实现"
            "（register_redis_provider）"
        )
    return _redis_provider()


def register_cycle_lock(lock: Callable[[str, float], bool]) -> None:
    """注册周期任务抢锁函数（组合根在启动时调用一次；测试可注入假实现）。

    参数：lock —— 与 infrastructure.redis.locks.try_acquire_cycle_lock 同
          签名的函数（key, interval_seconds -> bool）。
    """
    global _cycle_lock
    _cycle_lock = lock


def try_acquire_cycle_lock(key: str, interval_seconds: float) -> bool:
    """周期任务跨进程互斥抢锁（语义与行为同 infrastructure 实现）。

    参数：key —— 周期任务锁逻辑名（不含前缀）；
          interval_seconds —— 任务周期（秒），锁 TTL 取其减 5s 余量。
    返回：True=本进程执行本周期（抢到锁或 Redis 不可用/异常）；
          False=其他进程已持锁，跳过本周期。
    异常：RuntimeError —— 组合根尚未装配（属启动期配置错误，不应在运行期出现）。
    """
    if _cycle_lock is None:
        raise RuntimeError(
            "周期锁能力未装配：组合根 app/api/deps.py 未注册实现"
            "（register_cycle_lock）"
        )
    return _cycle_lock(key, interval_seconds)
