"""
模块名：app.infrastructure.redis.redis_client（Redis 连接管理，可选依赖）。

作用：
    为限流、邮箱验证码、撤销记录、LLM 用量、分布式锁等“多副本需共享状态”
    的功能提供统一的 Redis 客户端获取入口 get_redis()。

连接来源：
    settings.REDIS_URL，即环境变量/env/config.env 中的 redis_url；
    使用 redis.Redis.from_url 创建客户端，连接/读写超时均为 2 秒，
    decode_responses=True（返回 str 而非 bytes）。

降级策略：
    - 未配置 REDIS_URL：返回 None，调用方改用进程内存实现（单机开发模式）；
    - 已配置但连接失败（ping 异常）：返回 None 并告警降级，
      服务不因 Redis 故障而不可用；
    - 生产多副本部署必须配置 REDIS_URL，否则各副本限流/验证码/锁状态不互通。
    连接状态采用进程级惰性单例（首次调用探测一次），Redis 中途恢复需重启
    进程；如需运行时自动重连，可在此处增加周期性重探。

主要成员：
    - get_redis()：获取共享客户端（不可用时为 None）；
    - reset_redis_for_test()：测试辅助，重置探测状态强制重探。

被哪些模块依赖（Grep get_redis / from core.redis_client）：
    core.locks、app.auth.rate_limit（限流）、app.auth.account_guard、app.auth.delete_guard、
    core.undo_store、app.auth.verification、core.usage、app.domain.memory.context_memory、
    app.domain.memory.profile_service、app.domain.memory.session_keyword_service、app.domain.memory.session_rollover、
    app.domain.memory.short_term、service.file_service、service.admin_user_service；
    tests 下多个测试模块也直接导入。
"""
import logging
import threading
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)

# 进程级惰性单例：_client 为已连通的客户端（None 表示不可用/未配置），
# _checked 标记是否已完成过首次探测；_lock 保护首次探测的并发安全
_client = None
_checked = False
_lock = threading.Lock()


def get_redis() -> Optional[object]:
    """获取 Redis 客户端单例；未配置或不可用时返回 None（调用方走内存降级）。

    功能：首次调用时按 settings.REDIS_URL 探测一次连接并缓存结果，
    之后所有调用直接返回缓存（惰性单例 + 双重检查锁）。
    被谁调用：core.locks、app.auth.rate_limit、app.auth.account_guard、app.auth.delete_guard、
        core.undo_store、app.auth.verification、core.usage 及 app/domain/memory/service 下
        多个需要共享存储的模块（完整清单见模块 docstring）。
    返回：Optional[object]；连通时返回 redis.Redis 实例（decode_responses=True），
        未配置 redis_url 或 ping 失败时返回 None，调用方据此降级为进程内存实现。
    """
    global _client, _checked
    # 快路径：已探测过直接返回，避免每次调用都加锁
    if _checked:
        return _client
    with _lock:
        if _checked:
            return _client
        _checked = True
        if not settings.REDIS_URL:
            logger.info("未配置 redis_url，限流/验证码使用进程内存实现（单机模式）")
            return None
        try:
            import redis  # 延迟导入：未安装 redis 包且未配置时零影响

            client = redis.Redis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            # 主动 ping 探活：连接不通时立即走降级，而非把故障延迟到首次业务命令
            client.ping()
            _client = client
            logger.info("Redis 已连接，限流/验证码为多副本共享模式")
        except Exception as e:  # 连接失败不阻断启动，降级内存
            logger.warning("Redis 连接失败(%s)，限流/验证码降级为进程内存实现", e)
    return _client


def reset_redis_for_test() -> None:
    """测试辅助：清空缓存的客户端与探测标记。

    功能：强制下一次 get_redis() 重新读取配置并探测连接，
    通常配合 monkeypatch 切换 settings.REDIS_URL 或模拟 Redis 故障使用。
    被谁调用：仅测试代码（tests），生产代码不调用。
    返回：None。
    """
    global _client, _checked
    with _lock:
        _client = None
        _checked = False
