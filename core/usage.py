"""
模块名：core.usage（LLM token 用量计量）。

作用：
    每次 LLM 调用后累加请求次数与 token 用量，供管理看板与单用户预算熔断使用。
两路（实际为三路）聚合：
    - 按模型累计（Redis key=llm:usage:<model>）：兼容历史看板，
      管理员可查看各模型总量；
    - 按用户×月份累计（key=llm:user_usage:<user_id>:<YYYY-MM>）：
      管理员可查看每个用户每月的 token 消耗；
    - 按用户×日累计（key=llm:user_daily:<user_id>:<YYYY-MM-DD>）：
      预算熔断口径，键带 48h 过期兜底清理。

存储策略：
    Redis 优先（多副本一致，pipeline + HINCRBY 原子累加）；未配置或写入
    失败时降级进程内存（三张 defaultdict）。快照读取时合并 Redis 与内存
    两个来源，保证降级期间的用量不丢失。

用户 id 透传：
    user_id 通过线程局部变量从请求入口透传：app/application/chat/chat_service 在调用
    LLM 前 set_current_user_id，结束后 reset_current_user_id；
    model_llm/gateway 在 record_usage 时经 get_current_user_id 读取后
    写入用户维度。选用 threading.local 而非 contextvars：Starlette 对同步
    生成器每次 next() 都会复制事件循环上下文，导致生成器内 set 的 contextvar
    在下一次 next() 时丢失；而 LLM 调用均为同步阻塞，线程局部在同一执行
    线程内稳定可见。未设置时按 0 归到“未知用户”，不影响模型维度统计。

说明：
    流式响应中服务端可能不返回 token 用量（取决于上游兼容端点），
    此时仅累计 requests 次数，token 字段保持诚实为 0，不做估算。

主要成员：
    set_current_user_id / get_current_user_id / reset_current_user_id（线程局部）、
    record_usage（写入三路计量）、usage_snapshot / user_usage_snapshot（管理看板）、
    get_user_token_usage / check_user_budget（预算熔断）、reset_usage_for_test（测试）。

被谁使用（Grep）：
    - model_llm/gateway.py：get_current_user_id、record_usage（LLM 调用收尾）；
    - app/application/chat/chat_service.py：set/reset_current_user_id（对话入口）；
    - app/api/v1/chat.py：check_user_budget（对话前 429 熔断）；
    - app/api/v1/admin.py：usage_snapshot、user_usage_snapshot（管理看板）；
    - tests/phase/test_phase8_agent_eval.py：check_user_budget 限额验证。
"""
import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional, Tuple

from core.config import settings
from app.infrastructure.redis.redis_client import get_redis

logger = logging.getLogger(__name__)

# 三类 Redis 键前缀与 Hash 字段名
_MODEL_KEY_PREFIX = "llm:usage:"
_USER_KEY_PREFIX = "llm:user_usage:"
_DAILY_KEY_PREFIX = "llm:user_daily:"
_FIELDS = ("requests", "prompt_tokens", "completion_tokens")
# 保护三张内存降级表的进程内互斥锁（Redis 路径无状态，不需此锁）
_lock = threading.Lock()

# model -> {"requests", "prompt_tokens", "completion_tokens"}
_memory_model_usage: Dict[str, Dict[str, int]] = defaultdict(
    lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
)
# (user_id, month) -> {"requests", "prompt_tokens", "completion_tokens"}
_memory_user_usage: Dict[tuple, Dict[str, int]] = defaultdict(
    lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
)
# (user_id, date) -> {"requests", "prompt_tokens", "completion_tokens"}
_memory_user_daily: Dict[tuple, Dict[str, int]] = defaultdict(
    lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
)

# 线程局部：当前执行线程的用户 id（LLM 调用同步阻塞，同线程内稳定）
_tls = threading.local()


def set_current_user_id(user_id: Optional[int]) -> None:
    """设置当前线程的用户 id。

    功能：把请求用户 id 绑定到工作线程，供随后同线程内 LLM 网关
        record_usage 计量用户维度（模型/月/日）用量。
    被谁调用：app/application/chat/chat_service.py 各对话入口（调用 LLM 前设置，
        finally 中 reset_current_user_id 清理）。
    参数：
        user_id: 当前登录用户 id，来源为 get_current_user 鉴权结果；
        None 用于显式清除/匿名场景。
    返回：None。
    """
    _tls.user_id = user_id


def get_current_user_id() -> Optional[int]:
    """读取当前线程绑定的用户 id。

    被谁调用：model_llm/gateway.py 的 record_usage 调用点
        （_resolve_user_id 解析用户维度时）。
    返回：Optional[int]；set_current_user_id 设置过则返回该 id，
        未设置（如后台/非对话链路）返回 None（计量时按未知用户处理）。
    """
    return getattr(_tls, "user_id", None)


def reset_current_user_id() -> None:
    """清除当前线程的用户 id。

    功能：对话请求结束（或异常）后解绑线程局部，避免线程池线程复用时
        把上一用户的 id 串到下一请求的计量里。
    被谁调用：app/application/chat/chat_service.py 各对话入口的 finally 块。
    返回：None；属性本就不存在时静默忽略。
    """
    try:
        del _tls.user_id
    except AttributeError:
        pass


def _current_month() -> str:
    """返回当前月份字符串 YYYY-MM（用户×月维度的分桶键）。"""
    return datetime.now().strftime("%Y-%m")


def _current_date() -> str:
    """返回当前日期字符串 YYYY-MM-DD（用户×日维度/预算熔断的分桶键）。"""
    return datetime.now().strftime("%Y-%m-%d")


def record_usage(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    user_id: Optional[int] = None,
) -> None:
    """累加一次成功 LLM 调用的用量（token 可能为 0：流式且上游未返回用量时）。

    功能：同一次调用以原子 pipeline 写入「模型维度」「用户×月份维度」
        「用户×日维度」三路 Hash；Redis 不可用时整体降级写三张内存表；
        最后尽力同步递增 Prometheus 进程计数器。
    被谁调用：model_llm/gateway.py（非流式与流式 LLM 调用完成后）。
    参数：
        model: 模型名称，来源为本次调用实际使用的模型标识；
        prompt_tokens / completion_tokens: 上游返回的输入/输出 token 数，
            来源为 LLM 响应 usage；上游不返回时保持 0，不估算；
        user_id: 显式指定的用户 id；None 时回退读取线程局部
            （由 chat_service 在请求入口设置）。
    返回：None。
    """
    # 用户 id：显式参数优先，否则读线程局部（可能为 None -> 未知用户桶）
    if user_id is None:
        user_id = get_current_user_id()
    month = _current_month()
    date = _current_date()
    stored_in_redis = False
    try:
        r = get_redis()
        if r is not None:
            # 用 pipeline 一次发送全部 HINCRBY，保证三路计数原子且减少往返
            pipe = r.pipeline()
            # 模型维度：历史看板按模型汇总
            model_key = f"{_MODEL_KEY_PREFIX}{model}"
            pipe.hincrby(model_key, "requests", 1)
            if prompt_tokens:
                pipe.hincrby(model_key, "prompt_tokens", prompt_tokens)
            if completion_tokens:
                pipe.hincrby(model_key, "completion_tokens", completion_tokens)
            # 用户×月份维度：管理员查看每人每月消耗
            user_key = f"{_USER_KEY_PREFIX}{user_id}:{month}"
            pipe.hincrby(user_key, "requests", 1)
            if prompt_tokens:
                pipe.hincrby(user_key, "prompt_tokens", prompt_tokens)
            if completion_tokens:
                pipe.hincrby(user_key, "completion_tokens", completion_tokens)
            # 用户×日维度（预算熔断口径）：跨日自动滚动，键带过期兜底清理
            daily_key = f"{_DAILY_KEY_PREFIX}{user_id}:{date}"
            pipe.hincrby(daily_key, "requests", 1)
            if prompt_tokens:
                pipe.hincrby(daily_key, "prompt_tokens", prompt_tokens)
            if completion_tokens:
                pipe.hincrby(daily_key, "completion_tokens", completion_tokens)
            pipe.expire(daily_key, 172800)  # 48h，覆盖跨日边界
            pipe.execute()
            stored_in_redis = True
    except Exception as e:
        # Redis 任一环节失败：不丢计量，降级到进程内存表
        logger.warning("用量写入 Redis 失败，降级内存: %s", e)

    if not stored_in_redis:
        # 内存降级：三张表在同一把锁内完成累加，保证快照读取一致性
        with _lock:
            m = _memory_model_usage[model]
            m["requests"] += 1
            m["prompt_tokens"] += int(prompt_tokens or 0)
            m["completion_tokens"] += int(completion_tokens or 0)

            u = _memory_user_usage[(user_id, month)]
            u["requests"] += 1
            u["prompt_tokens"] += int(prompt_tokens or 0)
            u["completion_tokens"] += int(completion_tokens or 0)

            d = _memory_user_daily[(user_id, date)]
            d["requests"] += 1
            d["prompt_tokens"] += int(prompt_tokens or 0)
            d["completion_tokens"] += int(completion_tokens or 0)

    # 同步递增 Prometheus 进程计数器（延迟 import 避免硬耦合；钩子内部不抛异常）
    try:
        from core.metrics import record_llm_usage

        record_llm_usage(model, prompt_tokens, completion_tokens)
    except Exception:
        # 指标系统故障不得影响主调用链，直接忽略
        pass


def usage_snapshot() -> Dict[str, Dict[str, int]]:
    """按模型聚合的用量快照（Redis + 内存合并）。

    被谁调用：app/api/v1/admin.py 的模型用量看板端点。
    返回：Dict[str, Dict[str, int]]，形如
        {model: {"requests","prompt_tokens","completion_tokens"}}；
        同一模型在 Redis 与内存降级表中的计数相加，去向为管理接口响应
        （经 success(usage=...) 平铺返回）。
    """
    merged: Dict[str, Dict[str, int]] = {}
    try:
        r = get_redis()
        if r is not None:
            # 扫描 llm:usage:* 全部模型键并剥离前缀得到模型名
            for key in r.scan_iter(f"{_MODEL_KEY_PREFIX}*"):
                model = key[len(_MODEL_KEY_PREFIX):]
                data = r.hgetall(key) or {}
                merged[model] = {f: int(data.get(f, 0)) for f in _FIELDS}
    except Exception as e:
        # Redis 读失败不致命：仅返回内存口径
        logger.warning("用量快照读取 Redis 失败: %s", e)
    with _lock:
        # 合并降级期间落在内存里的计数（同名模型字段逐个相加）
        for model, bucket in _memory_model_usage.items():
            target = merged.setdefault(model, {f: 0 for f in _FIELDS})
            for f in _FIELDS:
                target[f] += bucket[f]
    return merged


def user_usage_snapshot(month: Optional[str] = None) -> Dict[int, Dict[str, int]]:
    """按用户聚合的月度用量快照。

    被谁调用：app/api/v1/admin.py 的用户用量看板端点
        （可通过查询参数指定月份）。
    参数：
        month: 月份字符串 YYYY-MM，来源为管理接口的可选查询参数；
            None 时取当前月。
    返回：Dict[int, Dict[str, int]]，
        {user_id: {requests, prompt_tokens, completion_tokens}}；
        Redis 与内存同用户同月份数据相加；无法解析 user_id 的键跳过。
    """
    month = month or _current_month()
    merged: Dict[int, Dict[str, int]] = {}
    try:
        r = get_redis()
        if r is not None:
            pattern = f"{_USER_KEY_PREFIX}*:{month}"
            for key in r.scan_iter(pattern):
                # key = llm:user_usage:<user_id>:<month>
                tail = key[len(_USER_KEY_PREFIX):]  # <user_id>:<month>
                # 月份本身不含冒号，最后一个冒号左侧即 user_id
                user_id_str = tail.rsplit(":", 1)[0]
                try:
                    user_id = int(user_id_str)
                except ValueError:
                    # 异常键（不符合数字 uid 约定）直接跳过，不污染结果
                    continue
                data = r.hgetall(key) or {}
                merged[user_id] = {f: int(data.get(f, 0)) for f in _FIELDS}
    except Exception as e:
        logger.warning("用户用量快照读取 Redis 失败: %s", e)
    with _lock:
        # 仅合并目标月份的内存桶
        for (uid, m), bucket in _memory_user_usage.items():
            if m != month:
                continue
            target = merged.setdefault(uid, {f: 0 for f in _FIELDS})
            for f in _FIELDS:
                target[f] += bucket[f]
    return merged


def get_user_token_usage(user_id: Optional[int]) -> Dict[str, int]:
    """读取当前用户今日/本月的 token 总消耗（prompt+completion 合计）。

    功能：分别读 Redis 的用户×日键与用户×月键，再叠加内存降级表口径。
    被谁调用：check_user_budget（core.usage）。
    参数：
        user_id: 待判定用户 id，来源为对话入口的鉴权身份；
            可能为 None（未知用户桶）。
    返回：Dict[str, int]，{"daily": 今日 token 合计,
        "monthly": 本月 token 合计}；只统计 token（不含 requests）。
    """
    month = _current_month()
    date = _current_date()
    daily = monthly = 0
    try:
        r = get_redis()
        if r is not None:
            d = r.hgetall(f"{_DAILY_KEY_PREFIX}{user_id}:{date}") or {}
            daily = int(d.get("prompt_tokens", 0)) + int(d.get("completion_tokens", 0))
            m = r.hgetall(f"{_USER_KEY_PREFIX}{user_id}:{month}") or {}
            monthly = int(m.get("prompt_tokens", 0)) + int(m.get("completion_tokens", 0))
    except Exception as e:  # noqa: BLE001 - 读取失败时仅用内存口径
        logger.warning("用户用量读取失败(仅内存口径): %s", e)
    with _lock:
        # Redis 不可用或部分降级时，用内存桶补齐同口径计数
        db = _memory_user_daily.get((user_id, date))
        if db:
            daily += db["prompt_tokens"] + db["completion_tokens"]
        mb = _memory_user_usage.get((user_id, month))
        if mb:
            monthly += mb["prompt_tokens"] + mb["completion_tokens"]
    return {"daily": daily, "monthly": monthly}


def check_user_budget(user_id: Optional[int]) -> Tuple[bool, Dict[str, int]]:
    """单用户 LLM 预算熔断判定（成本治理）。

    功能：读取用户今日/本月 token 消耗，与配置的日/月限额比较，
        任一达到限额即建议拒绝。限额为 0 表示不限制。
    被谁调用：app/api/v1/chat.py 对话入口（allowed=False 时
        端点向客户端返回 HTTP 429）；
        tests/phase/test_phase8_agent_eval.py 验证熔断行为。
    参数：
        user_id: 当前用户 id（鉴权身份透传）。
    返回：(allowed, info)，Tuple[bool, Dict[str, int]]；
        allowed=False 表示已达限额，对话入口应拒绝（HTTP 429）；
        info 在用量基础上附带 daily_limit/monthly_limit，
        去向为 429 响应提示或通过时的上下文信息。
    判定口径：用量与限额比较用 >=，即本次调用前已消耗的 token 总额达到
        限额即拒绝（超限前最后一段额度内的已发请求正常完成，不做按 token
        的预估截断）。
    """
    usage = get_user_token_usage(user_id)
    # 限额来自配置 llm_daily_token_limit / llm_monthly_token_limit，0 为不限
    daily_limit = settings.LLM_DAILY_TOKEN_LIMIT
    monthly_limit = settings.LLM_MONTHLY_TOKEN_LIMIT
    info = {
        **usage,
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
    }
    # 限额>0 且已消耗达到限额即熔断；日/月任一命中即拒绝
    blocked = (daily_limit > 0 and usage["daily"] >= daily_limit) or (
        monthly_limit > 0 and usage["monthly"] >= monthly_limit
    )
    return (not blocked), info


def reset_usage_for_test() -> None:
    """清空全部用量数据（仅测试用）。

    功能：清空三张内存表，并删除 Redis 上 llm:usage:*、llm:user_usage:*、
        llm:user_daily:* 全部用量键，供测试用例之间隔离。
    被谁调用：全仓 Grep 未见生产调用方，仅供测试夹具按需导入。
    返回：None。
    """
    with _lock:
        _memory_model_usage.clear()
        _memory_user_usage.clear()
        _memory_user_daily.clear()
    r = get_redis()
    if r is not None:
        for key in r.scan_iter(f"{_MODEL_KEY_PREFIX}*"):
            r.delete(key)
        for key in r.scan_iter(f"{_USER_KEY_PREFIX}*"):
            r.delete(key)
        for key in r.scan_iter(f"{_DAILY_KEY_PREFIX}*"):
            r.delete(key)
