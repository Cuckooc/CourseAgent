"""
模块：app.domain.memory.session_rollover —— 会话自动滚换（长对话漂移治理）。

背景：实测单会话超过约 12 轮后，模型对长历史的利用开始劣化（机械复读摘要、
事实泛化、回答膨胀），即使早期摘要中的事实并未丢失。达到设定轮数后把会话
滚换到新会话：重置历史长度，同时把上下文整体搬迁，用户无感续聊。

阈值配置（core/config.py）：
- settings.SESSION_AUTO_ROLLOVER_ENABLED（默认 true）：滚换总开关；
- settings.SESSION_AUTO_ROLLOVER_TURNS（默认 15 轮）：旧会话累计轮数达到
  该值即触发（1 轮 = user+assistant 两条；轮数由 chat_service 从短期
  记忆消息数 //2 得到，零 SQL）。

迁移内容（零额外 LLM 调用）：
1. 最近 N 轮原文：mem:short 旧 list 尾部 2N 条搬到新 list（meta total=2N、
   flushed=0，后台落库任务会把它们持久化进新会话，/history/detail 立即可见）；
2. 早期摘要：mem:ctx 的 summary 原样复制到新会话，compressed_rounds 置 0
   （摘要语义为"新会话本地窗口之前"的信息，新本地轮次尚未压缩；新会话首轮
   total=N=keep_n，boundary=0，不会重复触发压缩）；
3. 会话关键词：mem:kw SET 复制（同 TTL）；
4. 会话临时知识库：物理文件与 Chroma 向量（含原 embedding）复制到
   uploads/temp/{uid}_{new}（Windows 下 sqlite 句柄无法即时释放，不能
   move/rename；旧库保留，随旧会话删除时清理，见 TempKnowledgeStore.relocate）；
5. 用户画像为 user 级存储，天然跨会话，无需迁移。

失败策略：任何一步失败仅记日志并返回 None，绝不影响本轮已完成的回答；
已创建的空接续会话 best-effort 软删；旧会话保持可用，下一轮自动重试。

主要成员：
- maybe_rollover()：滚换判定+执行入口（模块级函数，含 Redis SET NX 分布式锁）；
- _do_rollover()：实际搬迁实现（读旧状态 → 建新会话 → pipeline 写三类
  Redis key → 复制临时知识库）；
- _new_title()：接续会话标题生成；_soft_delete_empty_session()：失败回滚。

被谁使用（全仓 import 位置）：
- app/application/chat/chat_service.py：ChatService._maybe_rollover 在 handle/
  handle_stream 本轮落库后调用（唯一生产调用方）；返回的新会话 ID 写入
  响应/done 帧的 session_id（附 rolled_over/previous_session_id 交前端切换）；
  app/domain/agents/ 不直接 import 本模块，滚换后由 chat_service 用新 sid 取
  上下文/画像/关键词再注入。
"""
import logging
import uuid
from typing import Optional

from sqlalchemy import text

from core.config import settings
from app.application.ports.kv import get_redis
from core.sql_guard import safe_execute
from app.application.ports.persistence import get_session_dao, session_scope

logger = logging.getLogger(__name__)

# 模块级常量：滚换后带入新会话的最近轮数（取稀疏窗口上限 5，保证首轮不超载；
# 更早的信息由复制过去的早期摘要承载）
_CARRY_ROUNDS = settings.CONTEXT_ROUNDS_SPARSE
# 模块级常量：滚换分布式锁 TTL（秒）。锁仅用于合并同会话的并发滚换请求，
# 30 秒覆盖搬迁临界区即可；超时自动释放（极端情况下允许下一轮重试）
_LOCK_TTL_SECONDS = 30
# 模块级常量：滚换锁键前缀，完整键 mem:rollover:{user_id}:{old_session_id}
_LOCK_KEY_PREFIX = "mem:rollover:"
# 以下三个常量与三个记忆模块的 Redis key 前缀保持一致（搬迁时直接按键操作，
# 不经对应服务类）：短期记忆 list / 上下文摘要 hash / 会话关键词 SET
_SHORT_KEY_PREFIX = "mem:short:"
_CTX_KEY_PREFIX = "mem:ctx:"
_KW_KEY_PREFIX = "mem:kw:"


def _new_title(old_title: str) -> str:
    """生成接续会话标题：空/默认标题 → “会话接续”；非默认标题追加“（接续）”。

    被谁调用：_do_rollover（建会话与写新 meta 各一次）。
    参数：old_title (str)——旧会话标题（来源 chat_service 本轮预取生成）。
    返回：str——新标题；原标题超 40 字先截断再追加后缀。
    """
    base = (old_title or "").strip()
    if not base or base in ("新会话", "未命名会话"):
        return "会话接续"
    if len(base) > 40:
        base = base[:40]
    return f"{base}（接续）"


def _soft_delete_empty_session(user_id: int, session_id: int) -> None:
    """迁移失败时 best-effort 软删刚创建、尚无消息的空接续会话。

    被谁调用：maybe_rollover 的异常回滚分支。
    参数：user_id (int，JWT)、session_id (int)——新建但废弃的接续会话 ID。
    返回：None。经 core.sql_guard.safe_execute 参数绑定执行 UPDATE
    history_information SET is_deleted=1；任何异常仅记 error 日志
    （孤儿会话最坏只留下一个空条目，不影响旧会话继续可用）。
    """
    try:
        with session_scope() as session:
            safe_execute(
                session,
                text(
                    "UPDATE history_information SET is_deleted = 1 "
                    "WHERE user_id = :uid AND session_id = :sid"
                ),
                {"uid": user_id, "sid": session_id},
            )
    except Exception as e:
        logger.error("rollover cleanup orphan session failed uid=%s sid=%s: %s",
                     user_id, session_id, e)


def maybe_rollover(
    user_id: int,
    old_session_id: int,
    current_rounds: int,
    old_title: str = "",
) -> Optional[int]:
    """
    达到轮数阈值时把旧会话滚换为新会话。

    功能：开关/入参/轮数三重前置判定 → Redis SET NX 分布式锁合并并发请求
    → 委托 _do_rollover 完成上下文整体搬迁；任何失败返回 None，调用方按
    旧会话继续。本函数不额外调用 LLM，迁移纯 Redis/MySQL/文件复制。
    被谁调用：app/application/chat/chat_service.py 的 ChatService._maybe_rollover
    （handle/handle_stream 本轮写入短期记忆后，唯一生产调用方）。
    :param user_id: 用户 ID，来源 JWT；
    :param old_session_id: 旧会话 ID，来源对话请求体；
    :param current_rounds: 本轮落库后旧会话的累计轮数（1 轮 = user+assistant，
           来源 chat_service 读短期记忆消息数 //2）；
    :param old_title: 旧会话标题（来源本轮预取生成，缺省空串）。
    :return: 新会话 id（int）；未触发（开关关/轮数不足）/抢锁失败/Redis
             不可用/搬迁异常时返回 None（调用方按旧会话继续，下一轮自动重试）。
    """
    if not settings.SESSION_AUTO_ROLLOVER_ENABLED:
        return None
    if not user_id or not old_session_id:
        return None
    if current_rounds < settings.SESSION_AUTO_ROLLOVER_TURNS:
        return None

    client = get_redis()
    if client is None:
        # 进程内降级模式下跨键搬迁缺乏一致性保障，直接跳过（单机开发可接受）
        logger.info("rollover skipped: Redis unavailable (uid=%s sid=%s)",
                    user_id, old_session_id)
        return None

    lock_key = f"{_LOCK_KEY_PREFIX}{user_id}:{old_session_id}"
    token = uuid.uuid4().hex
    try:
        acquired = client.set(lock_key, token, nx=True, ex=_LOCK_TTL_SECONDS)
    except Exception as e:
        logger.warning("rollover lock failed, skip: %s", e)
        return None
    if not acquired:
        # 已有滚换在进行（或 30s 内刚完成）：本次不重复滚换
        logger.info("rollover skipped: lock held (uid=%s sid=%s)",
                    user_id, old_session_id)
        return None

    new_sid: Optional[int] = None
    try:
        new_sid = _do_rollover(client, user_id, old_session_id, old_title)
        return new_sid
    except Exception as e:
        logger.exception("session rollover failed uid=%s sid=%s: %s",
                         user_id, old_session_id, e)
        if new_sid:
            _soft_delete_empty_session(user_id, new_sid)
        return None
    finally:
        try:
            # 比较 token 原子释放，避免误删他人的锁
            client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, lock_key, token,
            )
        except Exception as e:
            logger.debug("rollover lock release failed: %s", e)


def _do_rollover(client, user_id: int, old_sid: int, old_title: str) -> int:
    """滚换搬迁实现：旧会话上下文 → 新接续会话（调用方已持滚换锁）。

    被谁调用：maybe_rollover（抢锁成功后）。
    参数：client——Redis 客户端（调用方已确认非 None）；user_id (int，JWT)；
    old_sid (int)——旧会话 ID；old_title (str)——旧会话标题。
    返回：int——新会话 ID；旧短期记忆为空（刚被落库清空且未回填）时返回
    None 中止本轮滚换（下一轮再试）。
    迁移明细（全部数据去向新会话 {user_id}:{new_sid}）：
    1) mem:short 旧 list 尾部 2*_CARRY_ROUNDS 条原文 → 新 list，meta
       total=2N/flushed=0（后台 long_term flusher 会把它们落 MySQL
       session_information 的新会话行）；
    2) mem:ctx summary 原样复制、compressed_rounds 置 0；
    3) mem:kw SET 成员复制（TTL 同 CONTEXT_TTL_SECONDS）；
    4) 临时知识库经 TempKnowledgeStore.relocate 复制文件与 Chroma 向量；
    以上 1)-3) 用同一个 pipeline 一次 execute 原子提交；4) best-effort。
    异常：建会话失败/搬迁异常向上抛由 maybe_rollover 统一回滚（软删空会话）。
    """
    short_key = f"{_SHORT_KEY_PREFIX}{user_id}:{old_sid}"
    ctx_key = f"{_CTX_KEY_PREFIX}{user_id}:{old_sid}"
    kw_key = f"{_KW_KEY_PREFIX}{user_id}:{old_sid}"

    # 1) 读取待迁移状态（在创建新会话前完成，失败则什么都不留下）
    carry_items = client.lrange(short_key, -(_CARRY_ROUNDS * 2), -1)
    ctx_state = client.hgetall(ctx_key) or {}
    summary = ctx_state.get("summary", "") or ""
    kw_members = client.smembers(kw_key) or set()
    if not carry_items:
        # 极端情况：短期记忆刚被落库清空且尚未回填，本轮先不滚换
        logger.info("rollover aborted: no short-term messages (uid=%s sid=%s)",
                    user_id, old_sid)
        return None

    # 2) 创建接续会话
    new_sid = get_session_dao().create_session(user_id, _new_title(old_title))
    if not new_sid:
        raise RuntimeError("create continuation session failed")

    # 3) 迁移最近 N 轮原文（pipelined）：total=2N / flushed=0，
    #    后台落库任务负责把它们持久化进新会话
    new_short_key = f"{_SHORT_KEY_PREFIX}{user_id}:{new_sid}"
    new_meta_key = f"{new_short_key}:meta"
    pipe = client.pipeline()
    pipe.rpush(new_short_key, *carry_items)
    pipe.expire(new_short_key, settings.SHORT_TERM_TTL_SECONDS)
    pipe.hset(new_meta_key, mapping={
        "total": str(len(carry_items)),
        "flushed": "0",
        "title": _new_title(old_title),
    })
    pipe.expire(new_meta_key, settings.SHORT_TERM_TTL_SECONDS + 60)

    # 4) 迁移早期摘要（watermark 置 0：新会话本地轮次尚未压缩）
    new_ctx_key = f"{_CTX_KEY_PREFIX}{user_id}:{new_sid}"
    if summary:
        pipe.hset(new_ctx_key, mapping={
            "summary": summary,
            "compressed_rounds": "0",
        })
        pipe.expire(new_ctx_key, settings.CONTEXT_TTL_SECONDS)

    # 5) 迁移会话关键词
    if kw_members:
        new_kw_key = f"{_KW_KEY_PREFIX}{user_id}:{new_sid}"
        pipe.sadd(new_kw_key, *list(kw_members))
        pipe.expire(new_kw_key, settings.CONTEXT_TTL_SECONDS)
    pipe.execute()

    # 6) 迁移会话临时知识库（无则跳过；失败仅告警，不阻断滚换）
    try:
        from app.application.ports.vector import get_temp_store

        get_temp_store().relocate(user_id, old_sid, new_sid)
    except Exception as e:
        logger.warning("rollover temp knowledge relocate failed (uid=%s %s->%s): %s",
                       user_id, old_sid, new_sid, e)

    logger.info(
        "session rolled over: uid=%s %s -> %s, carried_rounds=%d, summary=%s, keywords=%d",
        user_id, old_sid, new_sid, len(carry_items) // 2,
        f"{len(summary)}chars" if summary else "none", len(kw_members),
    )
    return new_sid
