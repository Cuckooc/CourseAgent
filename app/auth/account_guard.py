"""
模块名：app.auth.account_guard

作用：
    登录失败锁定。窗口期内同一账号标识（用户名）失败达阈值后临时锁定该账号的登录尝试。

    - Redis 计数（INCR + 固定窗口 EXPIRE）；Redis 不可用时全部放行——与项目
      「Redis 故障自动降级」策略一致，此时仍有全局 IP/用户级限流兜底；
    - 锁定按「尝试的用户名」计数：攻击者对随机用户名的爆破不会影响真实用户，
      真实用户被锁定后等待窗口过期即可（无需管理员介入）；
    - 登录成功立即清零，避免正常用户偶发输错被累积锁定。

主要成员：
    - is_locked(identifier)：查询账号是否已被锁定；
    - record_failure(identifier)：登录失败计数 +1，返回累计失败次数；
    - reset(identifier)：登录成功后清零失败计数；
    - _key(identifier)：内部函数，拼接 Redis 键；
    - _FAIL_KEY_PREFIX：模块级常量，失败计数键前缀。

被谁使用：
    - control/login_control.py：以 ``from app.auth import account_guard`` 导入，
      在账号密码登录端点 login_by_account 中依次调用 is_locked / record_failure / reset。
"""
import logging
from typing import Union

from core.config import settings
from app.infrastructure.redis.redis_client import get_redis

logger = logging.getLogger(__name__)

# 模块级常量：Redis 中登录失败计数键的前缀，完整键形如 login:fail:<用户名>。
# 进程导入时确定，不含运行期状态；计数数据本身保存在 Redis（见 core.redis_client）。
_FAIL_KEY_PREFIX = "login:fail:"


def _key(identifier: str) -> str:
    """拼接失败计数的 Redis 键。

    功能：对账号标识做 strip + 小写归一化后拼上前缀，避免同一用户名因大小写/空格差异绕过锁定。
    被谁调用：仅本模块内的 is_locked / record_failure / reset 调用。

    参数：
        identifier: 尝试登录的用户名，来源为 HTTP 请求体（login_control.LoginByUsernameRequest.username）。
    返回：
        str：形如 "login:fail:zhangsan" 的 Redis 键名。
    """
    return f"{_FAIL_KEY_PREFIX}{identifier.strip().lower()}"


def is_locked(identifier: str) -> bool:
    """查询该账号标识是否已被锁定。

    功能：读取 Redis 失败计数，达到 settings.LOGIN_MAX_FAILURES 即判定锁定。
    被谁调用：control/login_control.py 的 login_by_account（账号密码登录端点，密码校验之前的前置拦截）。

    参数：
        identifier: 尝试登录的用户名，来源为 HTTP 请求体 LoginByUsernameRequest.username。
    返回：
        bool：True 表示已锁定（登录端点直接拒绝并返回提示）；False 放行。
        Redis 不可用、identifier 为空或读取异常时一律返回 False（降级放行，由 app.auth.rate_limit 的 IP 限流兜底）。
    """
    if not identifier:
        return False
    try:
        # Redis 降级：get_redis 返回 None（未配置 redis_url/连接失败）时不阻断登录，直接放行；
        # 此时安全性由 app.auth.rate_limit 的 IP 级滑动窗口限流兜底
        r = get_redis()
        if r is None:
            return False
        fails = int(r.get(_key(identifier)) or 0)
        return fails >= settings.LOGIN_MAX_FAILURES
    except Exception as e:  # noqa: BLE001 - 降级放行
        logger.warning("登录锁定检查失败(降级放行): %s", e)
        return False


def record_failure(identifier: Union[str, None]) -> int:
    """登录失败计数 +1（首写设定窗口 TTL，固定窗口到期自动解锁）。

    功能：对该用户名的失败计数执行 INCR，并刷新固定窗口 TTL（settings.LOGIN_LOCK_WINDOW_SECONDS）。
    被谁调用：control/login_control.py 的 login_by_account，密码校验失败后调用；
              调用方用返回值与 settings.LOGIN_MAX_FAILURES 比较，决定本次是否直接返回锁定响应。

    返回递增后的失败次数：并发登录场景下「先 is_locked 检查、后 record」存在
    检查-计数竞态，同一批次的请求可能在任何计数落库前都通过检查。调用方依据
    本次 INCR 的返回值判定是否达阈值，可保证同一批次内第 N 个请求立即收到
    锁定响应。Redis 不可用/异常时返回 0（降级放行，由 IP 限流兜底）。

    参数：
        identifier: 尝试登录的用户名，来源为 HTTP 请求体 LoginByUsernameRequest.username；可为空。
    返回：
        int：INCR 之后的累计失败次数（去向：login_control 与阈值比较）；
        identifier 为空、Redis 不可用或异常时返回 0。
    """
    if not identifier:
        return 0
    try:
        # Redis 降级：未配置/连接异常时返回 0，登录端点据此放行（IP 限流仍生效）
        r = get_redis()
        if r is None:
            return 0
        # pipeline 保证 INCR 与 EXPIRE 连续下发：首写即设定窗口 TTL，
        # 窗口到期键自动删除即视为自动解锁，无需后台清理任务
        pipe = r.pipeline()
        pipe.incr(_key(identifier), 1)
        pipe.expire(_key(identifier), settings.LOGIN_LOCK_WINDOW_SECONDS)
        count, _ = pipe.execute()
        return int(count)
    except Exception as e:  # noqa: BLE001
        logger.warning("登录失败计数写入失败: %s", e)
        return 0


def reset(identifier: Union[str, None]) -> None:
    """登录成功后清零失败计数。

    功能：删除该用户名的失败计数键，避免正常用户偶发输错被累积锁定。
    被谁调用：control/login_control.py 的 login_by_account，密码校验通过、签发 token 之前调用。

    参数：
        identifier: 登录成功的用户名，来源为 HTTP 请求体 LoginByUsernameRequest.username；可为空。
    返回：
        None。Redis 不可用/异常时仅告警不抛出（计数会随窗口 TTL 自然过期，无副作用）。
    """
    if not identifier:
        return
    try:
        r = get_redis()
        if r is not None:
            r.delete(_key(identifier))
    except Exception as e:  # noqa: BLE001
        logger.warning("登录失败计数清理失败: %s", e)
