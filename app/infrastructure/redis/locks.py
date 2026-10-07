"""
模块名：app.infrastructure.redis.locks

作用：
    基于 Redis 的分布式锁原语，供多副本/多 worker 部署下做跨进程互斥。

    - Redis 可用：SET NX PX + 唯一 token，释放用 Lua 比较 token 原子删除
      （防止误删他人的锁——持锁超时后另一持有者写入新 token 的场景）；
    - Redis 不可用（未配置/宕机）：静默退化为 no-op 上下文，调用方依赖
      自身的 DB 级兜底（FOR UPDATE + 唯一键重试）保证正确性。

    注意：锁不保证绝对公平与完全防死锁——持有超时后锁自动过期，
    临界区必须短于 timeout_ms。

主要成员：
    - distributed_lock(name, timeout_ms)：上下文管理器，短临界区互斥锁（降级为无锁）；
    - try_acquire_cycle_lock(key, interval_seconds)：周期任务每周期一次的抢锁函数
      （抢不到跳过本周期，不降级为并发执行）；
    - _KEY_PREFIX：模块级常量，锁键前缀 "lock:"；
    - _RELEASE_LUA：模块级常量，安全释放锁的 Lua 脚本（token 比对 + 删除原子化）。

被谁使用：
    - dao/session.py：会话取号序列 ``with distributed_lock(f"seq:{user_id}")``；
    - memory/long_term.py、memory/profile_service.py、core/purge_scheduler.py：
      后台 flusher/purge 周期任务用 try_acquire_cycle_lock 做多副本互斥。
"""
import logging
import uuid
from contextlib import contextmanager
from typing import Optional

from app.infrastructure.redis.redis_client import get_redis

logger = logging.getLogger(__name__)

# 模块级常量：Redis 锁键前缀，完整键形如 lock:seq:<user_id>、lock:<周期任务键>。
_KEY_PREFIX = "lock:"

# 释放锁 Lua：仅当键内 token 与本持有者一致才删除（原子），
# 防止「自己持锁超时 → 别人拿到新锁 → 自己 finally 误删新锁」
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@contextmanager
def distributed_lock(name: str, timeout_ms: int = 5000):
    """
    Redis 分布式锁上下文管理器。

    用法：
        with distributed_lock(f"seq:{user_id}"):
            ...

    加锁/降级逻辑：SET key token NX PX timeout_ms 抢锁；竞争失败先等 50ms 重试一次，
    仍失败或 Redis 异常则「降级无锁进入」（锁只是优化，正确性由 DB 层
    FOR UPDATE + 唯一键重试兜底）；退出临界区时用 _RELEASE_LUA 比对 token 原子释放。
    Redis 不可用时直接进入临界区（与未加锁等价），永不向调用方抛异常。

    被谁调用：dao/session.py 的会话取号临界区。
    参数：
        name: 锁的逻辑名（不含前缀），来源为调用方拼接的业务标识，如 f"seq:{user_id}"；
        timeout_ms: 持锁超时（毫秒），默认 5000；到期 Redis 自动释放，临界区必须短于该值。
    返回：上下文管理器（无 as 值）；任何分支都正常进入临界区。
    """
    r = None
    try:
        r = get_redis()
    except Exception as e:  # get_redis 正常不抛，防御性兜底
        logger.warning("distributed_lock: get_redis 异常(%s)，按无锁处理", e)

    key = f"{_KEY_PREFIX}{name}"
    token: Optional[str] = None
    if r is not None:
        token = uuid.uuid4().hex
        try:
            acquired = r.set(key, token, nx=True, px=timeout_ms)
            if not acquired:
                # 极端场景（如取号高频）等待一次再试，仍失败则降级放行：
                # DB 层 FOR UPDATE + 唯一键重试保证正确性，锁只是优化
                import time

                time.sleep(0.05)
                acquired = r.set(key, token, nx=True, px=timeout_ms)
            if not acquired:
                logger.warning("distributed_lock: %s 获取锁竞争失败，降级无锁进入", key)
                token = None
        except Exception as e:
            logger.warning("distributed_lock: %s 加锁失败(%s)，降级无锁进入", key, e)
            token = None

    try:
        yield
    finally:
        if token is not None:
            try:
                r.eval(_RELEASE_LUA, 1, key, token)
            except Exception as e:
                # 释放失败只影响下次竞争等待，锁有 PX 自动过期，无死锁
                logger.warning("distributed_lock: %s 释放失败(%s)，等待自动过期", key, e)


def try_acquire_cycle_lock(key: str, interval_seconds: float) -> bool:
    """
    周期任务的跨进程互斥：多 worker/多副本下每个进程都会起同名后台任务
    （如记忆落库 flusher），用本锁保证每周期只有一个进程执行。

    - 抢到锁（或 Redis 不可用）返回 True → 执行本周期任务；
    - 抢不到返回 False → 其他进程正在执行，跳过本周期（不降级为并发执行）；
    - 锁 TTL = 周期 - 5s 余量，自动过期，无需主动释放（临界区 = 单周期任务，
      超时场景下个周期自然重新竞争）。

    与 distributed_lock 的降级差异：周期任务漏跑一个周期无正确性风险（下周期补偿），
    因此 Redis 异常时按「可执行」放行；而抢锁失败严格返回 False，不允许并发跑。

    被谁调用：
        - memory/long_term.py 的短期→长期记忆 flusher 循环；
        - memory/profile_service.py 的用户画像落库循环；
        - core/purge_scheduler.py 的软删除/过期数据清理循环。
    参数：
        key: 周期任务锁的逻辑名（不含前缀），来源为各调用方定义的 *_FLUSH_LOCK_KEY 常量；
        interval_seconds: 任务周期（秒），来源为各后台循环的扫描间隔配置
                          （如 settings.SHORT_TERM_FLUSH_INTERVAL_SECONDS、PURGE_INTERVAL_HOURS）。
    返回：
        bool：True=本进程执行本周期任务（抢到锁或 Redis 不可用/异常）；
        False=其他进程已持锁，跳过本周期。
    """
    try:
        r = get_redis()
    except Exception as e:
        logger.warning("try_acquire_cycle_lock(%s): get_redis 异常(%s)，按可执行处理", key, e)
        return True
    if r is None:
        return True
    try:
        # TTL 取「周期 - 5s 余量」且至少 1s：任务即使超时未走完，下个周期前锁也会自动释放
        ttl_ms = max(int(interval_seconds * 1000) - 5000, 1000)
        # SET NX PX：仅键不存在时写入成功（抢到锁），随机 token 仅用于占坑，本锁无需主动释放
        return bool(r.set(f"{_KEY_PREFIX}{key}", uuid.uuid4().hex, nx=True, px=ttl_ms))
    except Exception as e:
        logger.warning("try_acquire_cycle_lock(%s): 加锁失败(%s)，按可执行处理", key, e)
        return True
