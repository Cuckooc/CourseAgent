"""
模块：app.domain.memory.long_term —— 长期记忆落库器：短期记忆 → MySQL 长期记忆的转变通道。

作用：承接 app/domain/memory/short_term 的 Redis 短期记忆，在会话静默临期或消息条数
逼近缓存上限时，把未落库消息批量持久化到 MySQL。数据链路：
短期记忆（Redis，滑动 TTL）→ 本模块后台扫描/落库 → MySQL 长期记忆
（dao/information.Information.save_messages_batch 写 session_information；
 dao/history.Information_history.save_information upsert history_information
 的会话标题）→ 读取侧 control/history_control 拼 MySQL + 未落库部分返回，
 app/domain/memory/context_memory 未命中短期记忆时回源 MySQL 并 warm_up 回填。
注意：落库后的对话消息不进入文档 RAG 检索（app/domain/agents/retrieval.py 的 RAG
只检索上传文档的 Chroma 向量）；长期记忆服务于历史回看与上下文重建。

产品语义：长期记忆由短期记忆转变而来。对话期间只写 Redis 短期记忆，
不写数据库；后台任务定期扫描短期记忆，满足以下任一条件时把未落库消息
批量写入 MySQL，然后（按条件）删除 Redis 中的短期记忆：

1. 临期落库：剩余 TTL ≤ SHORT_TERM_FLUSH_TTL_SECONDS（默认 300 秒；
   会话已静默临近过期，即设定保留时间内无更新/继续对话）→ 落库后删除
   Redis 短期记忆；
2. 条数兜底：未落库消息数即将超过缓存上限（pending > 上限-8，防止
   LTRIM 截断造成消息丢失）→ 落库后推进水位、保留缓存（会话可能仍在活跃）。

扫描周期取 settings.SHORT_TERM_FLUSH_INTERVAL_SECONDS（默认 60 秒）。

落库内容：未落库消息（单事务批量 INSERT session_information）+ 会话标题
（upsert history_information，不覆盖用户自定义标题）。

读取侧配套：会话详情 = MySQL 已落库部分 + 短期记忆未落库部分拼接
（见 app/api/v1/history.py）；上下文记忆短期未命中时回源 MySQL 并
warm_up 回填，落库删除 key 后该回源路径自然接管。

主要成员：
- LongTermFlusher：批量落库任务类（应用级单例 + 守护线程，DAO 注入）；
- get_long_term_flusher()：应用级单例工厂（双重检查锁）；
- reset_long_term_flusher_for_test()：测试辅助，重置单例；
- _FLUSH_LOCK_KEY：多副本周期互斥锁的逻辑键。

被谁使用（全仓 import 位置）：
- control/app.py 的 lifespan 启动钩子：get_long_term_flusher()
  .start_background_flusher() 启动守护线程（启动时先补扫一次）；
- app/domain/memory/short_term.py 不反向依赖本模块；本模块单向调用 ShortTermStore 的
  scan_sessions/pending_count/pending_messages/get_title/mark_flushed/drop。
"""
import logging
import threading
import time
from typing import List, Optional, Tuple

from core.config import settings
from app.application.ports.kv import try_acquire_cycle_lock
from app.infrastructure.persistence.repositories.history import Information_history
from app.infrastructure.persistence.repositories.information import Information
from app.domain.memory.short_term import get_short_term_store

logger = logging.getLogger(__name__)

# 多 worker/多副本部署时各进程都会启动本 flusher，用 Redis 周期锁互斥
# （见 core.locks.try_acquire_cycle_lock），避免并发「读 pending → 写 MySQL →
# 删键」造成长期记忆重复落库；Redis 不可用时维持单实例旧行为。
_FLUSH_LOCK_KEY = "flusher:long_term"


class LongTermFlusher:
    """
    短期记忆 → 长期记忆（MySQL）的批量落库任务（应用级单例 + 后台守护线程）。

    存储与策略：本类不持有消息数据，只编排 ShortTermStore（Redis 短期记忆）
    与两个 DAO（dao/information.Information 写 session_information、
    dao/history.Information_history 写 history_information 标题）；
    临期阈值 settings.SHORT_TERM_FLUSH_TTL_SECONDS（默认 300 秒）。
    实例化位置：生产仅由模块底部 get_long_term_flusher() 无参构造，
    control/app.py lifespan 调 start_background_flusher 启动后台线程；
    构造形参全部保留给测试注入。
    """

    def __init__(
        self,
        store=None,
        information_dao: Information = None,
        history_dao: Information_history = None,
        flush_ttl_threshold: int = None,
    ):
        """
        形参（生产由单例工厂无参构造，以下仅测试注入用）：
        - store：ShortTermStore 实例（Redis 短期记忆读写入口），None 时由
          store 属性惰性取 get_short_term_store() 全局单例；
        - information_dao：消息落库 DAO（dao/information.Information，
          操作 MySQL session_information），None 时自行 new；
        - history_dao：会话条目 DAO（dao/history.Information_history，
          upsert 标题到 history_information），None 时自行 new；
        - flush_ttl_threshold：临期阈值秒数，None 取
          settings.SHORT_TERM_FLUSH_TTL_SECONDS（默认 300），剩余 TTL
          不大于该值即判定“会话静默临期”，落库后删除短期记忆。
        关键属性去向：_started 保证后台线程幂等启动一次。
        """
        self._store = store
        self._information_dao = information_dao or Information()
        self._history_dao = history_dao or Information_history()
        self._flush_ttl_threshold = (
            flush_ttl_threshold
            if flush_ttl_threshold is not None
            else settings.SHORT_TERM_FLUSH_TTL_SECONDS
        )
        self._started = False

    @property
    def store(self):
        """惰性获取短期记忆存储：注入实例优先，否则取 get_short_term_store() 单例。"""
        return self._store if self._store is not None else get_short_term_store()

    # ------------------------------------------------------------------ 落库

    def flush_one(self, user_id: int, session_id: int, drop_after: bool) -> bool:
        """
        把单个会话的未落库消息批量写入 MySQL。

        被谁调用：flush_due（后台扫描每周期）；测试也可直接调用。
        参数：
        - user_id (int)：JWT 体系的用户 ID（来源 scan_sessions 解析 key）；
        - session_id (int)：会话 ID（同上）；
        - drop_after (bool)：True=临期落库，成功后删除 Redis 短期记忆
          （store.drop，长期记忆接管）；False=条数兜底落库，仅推进水位
          （store.mark_flushed，flushed=total），保留缓存供活跃会话继续。
        返回：bool——是否发生了落库（无待落库消息/取消息为空/DAO 写失败
        均返回 False）。数据去向：MySQL session_information（消息，
        Information.save_messages_batch 单事务批量 INSERT）与
        history_information（标题 upsert，DAO 内部保证不覆盖自定义标题）。
        异常：DAO 写失败时不推进水位、不删缓存，返回 False 等下周期重试；
        单会话异常由 flush_due 外层捕获，不影响其他会话。
        """
        store = self.store
        pending = store.pending_count(user_id, session_id)
        title = store.get_title(user_id, session_id)
        if pending <= 0:
            # 无待落库消息：临期场景直接清理缓存（数据早已全部落库/回填）
            if drop_after:
                store.drop(user_id, session_id)
            return False
        messages = store.pending_messages(user_id, session_id)
        if not messages:
            # 水位显示有待落库但 list 取不到（极端竞态/已截断）：不动缓存
            return False
        ok = self._information_dao.save_messages_batch(user_id, session_id, messages)
        if not ok:
            logger.error(
                "long-term flush failed, keep short-term memory (uid=%s,sid=%s)",
                user_id, session_id,
            )
            return False  # 失败不推进水位、不删缓存，下轮重试
        if title:
            # 标题随消息一并 upsert；DAO 层保证仅覆盖默认标题、不覆盖自定义标题
            self._history_dao.save_information(
                {"user_id": user_id, "session_id": session_id, "title": title}
            )
        if drop_after:
            store.drop(user_id, session_id)
        else:
            store.mark_flushed(user_id, session_id)
        logger.info(
            "short-term memory flushed to long-term (uid=%s,sid=%s,count=%s,drop=%s)",
            user_id, session_id, len(messages), drop_after,
        )
        return True

    def flush_due(self, limit: int = 200) -> int:
        """
        扫描全部存活短期记忆，落库满足条件的会话。

        被谁调用：后台守护线程 _run（启动补扫一次 + 之后每周期一次，
        周期 settings.SHORT_TERM_FLUSH_INTERVAL_SECONDS 默认 60 秒）。
        参数：limit (int)——单周期最多落库的会话数（默认 200，达到即停止
        遍历，剩余下周期处理）。
        返回：int——本周期实际发生落库的会话数。
        判定：ttl == -2（key 已过期消失）跳过；near_expiry=剩余 TTL ≤
        settings.SHORT_TERM_FLUSH_TTL_SECONDS（默认 300；-1 无 TTL 也视为
        需处理）；overflow=待落库条数 > 缓存上限-8（预留 8 条余量，在
        LTRIM 截断前抢先落库防丢消息）。近因优先：临期落库 drop_after=True，
        仅溢出时 drop_after=False 保留缓存。
        异常：单会话异常记 error 后继续处理后续会话。
        """
        store = self.store
        flushed = 0
        candidates: List[Tuple[int, int]] = store.scan_sessions()
        for user_id, session_id in candidates:
            try:
                ttl = store.ttl_seconds(user_id, session_id)
                if ttl == -2:  # key 已过期消失
                    continue
                pending = store.pending_count(user_id, session_id)
                near_expiry = ttl <= self._flush_ttl_threshold  # 临期（-1 无 TTL 也视为需处理）
                overflow = pending > store._max_messages - 8  # 截断兜底：预留 8 条余量
                if near_expiry or overflow:
                    if self.flush_one(user_id, session_id, drop_after=near_expiry):
                        flushed += 1
            except Exception as e:
                logger.error(
                    "long-term flush error (uid=%s,sid=%s): %s", user_id, session_id, e
                )
            if flushed >= limit:
                break
        return flushed

    # ------------------------------------------------------------------ 后台任务

    def start_background_flusher(self, interval_seconds: int = None) -> None:
        """启动守护线程定期落库（幂等；进程重启后启动钩子补扫一次）。

        被谁调用：control/app.py 的 lifespan 启动钩子（每进程一次）。
        参数：interval_seconds (int|None)——扫描周期，None 取
        settings.SHORT_TERM_FLUSH_INTERVAL_SECONDS（默认 60 秒）。
        返回：None。
        并发：每周期先经 core.locks.try_acquire_cycle_lock 抢 Redis 周期锁
        （TTL=周期-5s），多 worker/多副本下每周期只有一个进程真正落库，
        抢不到则跳过本周期；锁异常/Redis 不可用时放行（漏跑无正确性风险，
        下周期补偿）。线程为 daemon，主进程退出即结束。
        """
        if self._started:
            return
        self._started = True
        interval = (
            interval_seconds
            if interval_seconds is not None
            else settings.SHORT_TERM_FLUSH_INTERVAL_SECONDS
        )

        def _run():
            # 启动即补扫一次：处理进程停机期间已临期/溢出的短期记忆（不抢锁，
            # 单进程启动瞬间竞争概率低，且落库以水位幂等兜底）
            self.flush_due()
            while True:
                time.sleep(interval)
                try:
                    if not try_acquire_cycle_lock(_FLUSH_LOCK_KEY, interval):
                        continue  # 其他 worker 正在本周期落库，跳过
                    self.flush_due()
                except Exception as e:
                    # 线程级兜底：单周期异常绝不能杀死守护线程
                    logger.error("long-term background flush error: %s", e)

        t = threading.Thread(target=_run, name="long-term-flusher", daemon=True)
        t.start()
        logger.info("long-term flusher started, interval=%ss", interval)


# 模块级全局单例：进程内唯一 LongTermFlusher；首次调用 get_long_term_flusher()
# 时惰性构造（control/app.py lifespan 启动钩子），_flusher_lock 双重检查锁。
_flusher: Optional[LongTermFlusher] = None
_flusher_lock = threading.Lock()


def get_long_term_flusher() -> LongTermFlusher:
    """应用级单例工厂（双重检查锁）。

    被谁调用：control/app.py 的 lifespan 启动钩子启动后台落库线程。
    返回：LongTermFlusher——共享单例（内部惰性取 ShortTermStore 单例与 DAO）。
    """
    global _flusher
    if _flusher is None:
        with _flusher_lock:
            if _flusher is None:
                _flusher = LongTermFlusher()
    return _flusher


def reset_long_term_flusher_for_test() -> None:
    """测试辅助：重置单例（配合 fake store/DAO 重新注入构造）。"""
    global _flusher
    with _flusher_lock:
        _flusher = None
