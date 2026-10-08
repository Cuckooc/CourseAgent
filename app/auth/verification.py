"""
模块名：app.auth.verification（邮箱验证码存储与校验）。

作用：
    为“邮箱 + 验证码”免密登录生成、暂存并校验 6 位数字验证码。
    Redis 优先（多副本共享），未配置或不可用时降级进程内存。

语义（两种实现保持一致）：
    - 验证码 5 分钟有效（_CODE_TTL_SECONDS）；
    - 同一邮箱两次发送间隔至少 60 秒（冷却期内 generate_code 返回空串）；
    - 仅校验成功时立即作废（一次性使用）；校验失败保留记录，
      暴力猜测由 /login/email 接口的限流兜底（见 app.auth.rate_limit.rate_limit）。

存储键设计（Redis）：
    - vc:{email}：验证码本体，SETEX 写入，TTL 5 分钟；
    - vc:cd:{email}：发送冷却标记，SET NX EX 60 原子占位，冷却期内不可再发。
    内存降级时由 _store 字典记录 code/expire_at/last_sent 三个时间点，
    并在每次操作前调用 _purge_expired 清理过期邮箱。

注意：
    当前验证码在未配置 SMTP 时通过服务日志输出（仅开发环境），
    生产环境应接入邮件服务发送，禁止写日志。

主要成员：
    generate_code（生成/冷却判定）、verify_code（一次性校验）、
    reset_verification_store（测试清理）、_purge_expired（内存过期清理）。

被谁使用（Grep）：
    app/application/auth/user.py —— send_email_code 调用 generate_code 并发送，
    SMTP 失败时调用 verify_code 作废验证码；
    login_by_email_code 调用 verify_code 完成登录前校验。
"""
import logging
import random
import threading
import time
from typing import Dict

from app.application.ports.kv import get_redis

logger = logging.getLogger(__name__)

_CODE_TTL_SECONDS = 5 * 60        # 验证码 5 分钟有效
_RESEND_COOLDOWN_SECONDS = 60     # 两次发送间隔 60 秒

# Redis 键：vc:{email} 验证码（TTL 5min）；vc:cd:{email} 发送冷却标记（TTL 60s）
_KEY_PREFIX = "vc:"
_COOLDOWN_PREFIX = "vc:cd:"

# 内存降级路径的互斥锁与存储表
_lock = threading.Lock()
# email -> {"code": str, "expire_at": float, "last_sent": float}
_store: Dict[str, Dict[str, float]] = {}


def _purge_expired(now: float) -> None:
    """内存模式：清掉所有已过有效期的邮箱记录（调用方须持 _lock）。

    被谁调用：generate_code、verify_code 的内存分支（持锁后、读写前）。
    参数：
        now: 当前时间戳（time.time()），由调用方统一取一次，避免竞态差。
    返回：None。
    """
    expired = [k for k, v in _store.items() if v["expire_at"] < now]
    for k in expired:
        _store.pop(k, None)


def generate_code(email: str) -> str:
    """生成并存储验证码；处于 60 秒发送冷却期时返回空串。

    功能：生成 6 位数字码；Redis 路径用 SET NX 原子抢冷却位实现防刷，
        成功后 SETEX 写入验证码；内存路径检查 last_sent 间隔后写入记录。
    被谁调用：app/application/auth/user.py 的 send_email_code（发送验证码接口），
        返回空串时上层提示“验证码发送过于频繁，请 60 秒后再试”。
    参数：
        email: 目标邮箱，来源为发送验证码 HTTP 请求体（经上层格式校验）。
    返回：str；正常返回 6 位数字验证码（去向为邮件/开发日志），
        冷却期内返回 ""（上层据此拒绝本次发送）。
    """
    # 固定 6 位、前导补零（0 ~ 999999）
    code = f"{random.randint(0, 999999):06d}"

    r = get_redis()
    if r is not None:
        # SET NX 原子占冷却位：键已存在（冷却中）返回 None，避免并发下重复发送
        if not r.set(f"{_COOLDOWN_PREFIX}{email}", "1", nx=True, ex=_RESEND_COOLDOWN_SECONDS):
            return ""
        # 占位成功后写验证码本体（5 分钟 TTL，到期自动失效）
        r.setex(f"{_KEY_PREFIX}{email}", _CODE_TTL_SECONDS, code)
        return code

    now = time.time()
    with _lock:
        # 先清理过期记录，防止过期邮箱占用判断
        _purge_expired(now)
        record = _store.get(email)
        # 冷却判定：距上次发送不足 60 秒则拒绝（本次不刷新任何时间）
        if record and now - record["last_sent"] < _RESEND_COOLDOWN_SECONDS:
            return ""
        # 覆盖旧记录：新验证码生效，last_sent 用于下一轮冷却判定
        _store[email] = {"code": code, "expire_at": now + _CODE_TTL_SECONDS, "last_sent": now}
        return code


def verify_code(email: str, code: str) -> bool:
    """
    校验验证码：仅校验成功时立即作废（一次性使用）；
    校验失败保留记录（继续受 TTL 与发送冷却约束，暴力猜测由接口限流兜底）。

    被谁调用：app/application/auth/user.py 的 login_by_email_code（登录校验，失败返回
        “验证码错误或已过期”）；send_email_code 在 SMTP 发送失败时也会
        用本函数作废旧码，保证用户冷却期内可立即重新获取。
    参数：
        email: 邮箱，来源为邮箱登录 HTTP 请求体；
        code: 用户提交的验证码，来源为同一请求体，校验前 strip 去空白。
    返回：bool；匹配且未过期返回 True（记录随即删除），否则返回 False。
    """
    # 入参兜底：空邮箱/空验证码直接判失败
    if not email or not code:
        return False

    r = get_redis()
    if r is not None:
        key = f"{_KEY_PREFIX}{email}"
        stored = r.get(key)
        if stored is not None and stored == code.strip():
            # 一次性：成功立即删除，防止同一验证码重复登录
            r.delete(key)
            return True
        # 失败不删：仍受 5min TTL 约束，猜测风险由接口限流控制
        return False

    now = time.time()
    with _lock:
        _purge_expired(now)
        record = _store.get(email)
        # 无记录或已过期：顺手移除并判失败
        if not record or record["expire_at"] < now:
            _store.pop(email, None)
            return False
        if record["code"] == code.strip():
            # 一次性消费：校验成功立即弹出记录
            _store.pop(email, None)
            return True
        # 校验失败保留记录（不刷新过期时间），允许在 TTL 内继续尝试
        return False


def reset_verification_store() -> None:
    """清空验证码存储（仅测试用）。

    功能：清空内存 _store，并删除 Redis 上 vc:* 与 vc:cd:* 前缀全部键，
        供测试用例之间隔离，避免冷却/验证码残留影响断言。
    被谁调用：全仓 Grep 未见生产调用方，仅供测试夹具按需导入。
    返回：None。
    """
    with _lock:
        _store.clear()
    r = get_redis()
    if r is not None:
        for k in r.scan_iter(f"{_KEY_PREFIX}*"):
            r.delete(k)
        for k in r.scan_iter(f"{_COOLDOWN_PREFIX}*"):
            r.delete(k)
