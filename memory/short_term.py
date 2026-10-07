"""
模块：memory.short_term —— 短期记忆（会话级，Redis）。

数据流总览：消息从对话接口进入（control/chat_control.py →
service/chat_service.py 的 handle/handle_stream，session_id 由 chat_control
从 JWT(user_id) 与请求体(session_id) 传入）→ 每轮问答经 append_round 只写
Redis（本模块）→ TTL 到期/条数兜底时由后台任务（memory/long_term.py）批量
写 MySQL（session_information）后删除本 key → 读取侧（memory/context_memory
构建注入 LLM 的上下文、control/history_control 会话详情）优先读 Redis，
未命中回源 MySQL 长期记忆并 warm_up 回填。

语义：
- 记录当前活跃会话的近期消息（1 轮 = user + assistant 两条）；
- 滑动保留时间 settings.SHORT_TERM_TTL_SECONDS：会话在该时间内没有新对话即
  被 Redis 自动过期清理；用户在原会话继续对话会重新计时（每次写入 EXPIRE 续期）；
- 上下文记忆优先读短期记忆（Redis 微秒级），未命中再回源 MySQL 长期记忆并回填；
- 长期记忆由短期记忆转变而来：对话期间只写 Redis 不写 MySQL，后台落库任务
  （memory/long_term.py）在会话静默临近过期时批量写入 MySQL 后删除本 key，
  减少数据库请求。

存储：
- Redis list，key = mem:short:{user_id}:{session_id}，元素为 JSON 消息；
  RPUSH 追加 + LTRIM 只保留最近 N 条 + EXPIRE 滑动续期；
- meta hash，key = mem:short:{user_id}:{session_id}:meta：
  title（会话标题，落库时一并 upsert）、total（累计消息条数）、
  flushed（已落库消息条数），pending = total - flushed 即待落库消息数；
- Redis 不可用时降级为进程内 TTL dict（单机开发语义，多副本部署必须配置 Redis）。

主要成员：
- ShortTermStore：短期记忆存储类（Redis list + meta hash，含进程内 dict 降级）；
- get_short_term_store()：应用级单例工厂（双重检查锁，非每请求新建）；
- reset_short_term_store_for_test()：测试辅助，重置单例。

被谁使用（全仓 import 位置）：
- service/chat_service.py：ChatService.__init__ 持有单例；_save_information
  调 append_round 写每轮对话，_maybe_rollover/recover 调 load 读消息；
- memory/long_term.py：LongTermFlusher 调 scan_sessions/ttl_seconds/
  pending_count/pending_messages/get_title/mark_flushed/drop 完成批量落库；
- memory/context_memory.py：_load_messages 调 load 读短期、warm_up 回填；
- control/history_control.py：/history/detail 拼 pending_messages，
  /history/delete/confirm 调 clear 联动清理。
"""
import json
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from core.config import settings
from app.infrastructure.redis.redis_client import get_redis

logger = logging.getLogger(__name__)

# Redis key 前缀：mem:short:{user_id}:{session_id} 为消息 list；
# 同名加 :meta 后缀为元数据 hash（title/total/flushed），:guard 为回填并发守卫
_KEY_PREFIX = "mem:short:"


def _key(user_id: int, session_id: int) -> str:
    """拼接消息 list 的 key：mem:short:{user_id}:{session_id}。"""
    return f"{_KEY_PREFIX}{user_id}:{session_id}"


def _meta_key(user_id: int, session_id: int) -> str:
    """拼接元数据 hash 的 key：消息 list key 加 :meta 后缀。"""
    return f"{_KEY_PREFIX}{user_id}:{session_id}:meta"


class ShortTermStore:
    """
    会话短期记忆存储（应用级单例，经 get_short_term_store 获取，非每请求新建；
    service/chat_service.py、memory/long_term.py、memory/context_memory.py、
    control/history_control.py 共用同一实例）。

    职责：对话期间承接每轮消息的 Redis 读写与滑动 TTL，维护 total/flushed
    落库水位供后台批量落库，并在 Redis 不可用时降级为进程内 TTL dict。
    """

    def __init__(self, client=None, ttl: int = None, max_messages: int = None):
        """
        形参（生产路径由 get_short_term_store() 无参构造，参数仅供测试注入）：
        - client：Redis 客户端，None 时由 client 属性惰性取 core.redis_client.get_redis()；
        - ttl：滑动过期秒数，None 取 settings.SHORT_TERM_TTL_SECONDS（默认 1800）；
        - max_messages：list 保留的最近消息条数，None 取
          settings.SHORT_TERM_MAX_MESSAGES（默认 40）。
        关键属性去向：_client/_ttl/_max_messages 供全部读写与落库协同方法使用；
        _mem + _mem_lock 为 Redis 不可用时的进程内降级存储（key -> 消息槽）。
        """
        # 允许测试注入 fake client；默认惰性取全局 Redis
        self._client = client
        self._ttl = ttl if ttl is not None else settings.SHORT_TERM_TTL_SECONDS
        self._max_messages = max_messages or settings.SHORT_TERM_MAX_MESSAGES
        # Redis 不可用时的进程内降级：key -> (messages, expires_at, total, flushed, title)
        self._mem: Dict[str, dict] = {}
        self._mem_lock = threading.Lock()

    @property
    def client(self):
        """惰性获取 Redis 客户端：注入实例优先，否则取全局连接；返回 None 表示 Redis 不可用，调用方走降级分支。"""
        if self._client is not None:
            return self._client
        return get_redis()

    # ------------------------------------------------------------------ 写入

    def append_round(
        self,
        user_id: int,
        session_id: int,
        user_content: str,
        ai_content: str,
        title: str = None,
    ) -> None:
        """
        追加一轮对话（user + assistant 两条 JSON 消息），滑动续期并累计待落库计数。

        被谁调用：service/chat_service.py 的 ChatService._save_information
        （handle/handle_stream 拿到完整 LLM 回答后，每轮恰好一次）。
        参数：
        - user_id (int)：JWT 注入的用户 ID；session_id (int)：当前会话 ID（请求体）；
        - user_content (str)：本轮用户提问，来源对话接口请求体；
        - ai_content (str)：本轮助手回答，来源 LLM 对话链路（AgentService 产出）；
        - title (str|None)：本轮预取生成的会话标题，写入 meta 供落库时 upsert。
        返回：None。数据去向：Redis list/meta（后台任务再批量落 MySQL）。
        异常：Redis 写入抛错时记 error 日志并降级 _mem_append 写进程内 dict，保证对话链路不中断。
        """
        if not user_id or not session_id:
            return
        key = _key(user_id, session_id)
        meta_key = _meta_key(user_id, session_id)
        items = [
            json.dumps({"role": "user", "content": user_content or ""}, ensure_ascii=False),
            json.dumps({"role": "assistant", "content": ai_content or ""}, ensure_ascii=False),
        ]
        client = self.client
        try:
            if client is not None:
                # pipeline 一次往返原子提交：追加/截断/续期/计数互为整体，避免出现半写状态
                pipe = client.pipeline()
                pipe.rpush(key, *items)
                # 只保留最近 max_messages 条（LTRIM start=-N,-1）
                pipe.ltrim(key, -self._max_messages, -1)
                pipe.expire(key, self._ttl)
                if title:
                    pipe.hset(meta_key, "title", title)
                # 累计消息数 +2（pending = total - flushed）
                pipe.hincrby(meta_key, "total", 2)
                pipe.expire(meta_key, self._ttl + 60)  # meta 略晚于 list 过期
                pipe.execute()
            else:
                self._mem_append(key, items, title)
        except Exception as e:
            logger.error("short-term append failed (uid=%s,sid=%s): %s", user_id, session_id, e)
            # Redis 故障时降级内存，保证链路不中断
            self._mem_append(key, items, title)

    def touch(self, user_id: int, session_id: int) -> None:
        """
        仅滑动续期不写消息（预留：对话开始时调用可确保活跃会话不被提前清理；
        当前生产链路每轮 append_round 已自带 EXPIRE 续期，故暂无外部调用方）。

        参数：user_id/session_id 来源同 append_round。返回：None。
        异常：Redis 异常仅记 debug 日志（续期失败不影响主流程，下轮写入会再续期）。
        """
        if not user_id or not session_id:
            return
        key = _key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                client.expire(key, self._ttl)
            elif key in self._mem:
                with self._mem_lock:
                    if key in self._mem:
                        self._mem[key]["expires_at"] = time.time() + self._ttl
        except Exception as e:
            logger.debug("short-term touch failed: %s", e)

    def clear(self, user_id: int, session_id: int) -> None:
        """
        删除会话的短期记忆（list + meta）及进程内降级副本。

        被谁调用：control/history_control.py 的 /history/delete/confirm
        （用户删除历史会话联动清理）；本类 drop() 落库后正常转变也复用本方法。
        参数：user_id (int，JWT)、session_id (int，请求体)。返回：None。
        异常：Redis 删除失败仅记 debug 日志，仍尽力清理进程内副本，不向上抛错。
        """
        if not user_id or not session_id:
            return
        key = _key(user_id, session_id)
        meta_key = _meta_key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                client.delete(key, meta_key)
        except Exception as e:
            logger.debug("short-term clear failed: %s", e)
        with self._mem_lock:
            self._mem.pop(key, None)

    # ------------------------------------------------------------------ 读取

    def load(self, user_id: int, session_id: int) -> Optional[List[Dict[str, str]]]:
        """
        读取短期记忆消息（时间升序）。

        被谁调用：memory/context_memory.py 的 _load_messages（构建注入 LLM 的
        上下文）；service/chat_service.py 的 _maybe_rollover（取消息数算轮数）
        与 recover（网络中断恢复查看本轮已生成内容）。
        参数：user_id (int，JWT)、session_id (int，请求体)。
        返回：Optional[List[Dict[str,str]]]——元素为 {"role","content"} 的消息列表；
        缓存未命中（key 不存在/已过期）返回 None，调用方应回源 MySQL 并 warm_up 回填；
        命中空列表（理论不会出现）返回 []。
        异常：Redis 读取失败记 error 日志并返回 None（按未命中处理，降级回源 MySQL）。
        """
        if not user_id or not session_id:
            return None
        key = _key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                raw = client.lrange(key, 0, -1)
                if not raw:
                    return None
                return [json.loads(x) for x in raw]
            return self._mem_load(key)
        except Exception as e:
            logger.error("short-term load failed (uid=%s,sid=%s): %s", user_id, session_id, e)
            return None

    def warm_up(self, user_id: int, session_id: int, messages: List[Dict[str, str]]) -> None:
        """
        缓存未命中回源 MySQL 后回填短期记忆（回填视为一次活跃访问，给予完整 TTL）。

        被谁调用：memory/context_memory.py 的 _load_messages 在 Redis 未命中、
        经 SessionDAO 从 MySQL 读完历史后调用，避免下一轮再次回源。
        参数：user_id/session_id 同上；messages (List[Dict])——时间升序的历史消息，
        来源 MySQL session_information，仅截取最近 max_messages 条回填。
        返回：None。
        异常：回填失败仅记 debug 日志（不影响本轮用 MySQL 数据继续构建上下文）。
        并发：SET NX 5 秒 guard + 二次 exists 检查串行化并发回填，避免覆盖期间新 append 的消息。
        """
        if not user_id or not session_id or not messages:
            return
        key = _key(user_id, session_id)
        recent = messages[-self._max_messages:]
        items = [
            json.dumps({"role": m.get("role"), "content": m.get("content", "")}, ensure_ascii=False)
            for m in recent
        ]
        client = self.client
        try:
            if client is not None:
                # setnx guard 串行化并发回填；guard 获取后再确认 key 仍不存在
                # （期间可能已有新对话 append），任一条件不满足都跳过，避免覆盖新消息
                guard_key = f"{key}:guard"
                guard = client.set(guard_key, "1", nx=True, ex=5)
                try:
                    if guard and not client.exists(key):
                        pipe = client.pipeline()
                        pipe.delete(key)
                        pipe.rpush(key, *items)
                        pipe.expire(key, self._ttl)
                        pipe.execute()
                finally:
                    if guard:
                        client.delete(guard_key)
            else:
                with self._mem_lock:
                    if key not in self._mem:
                        self._mem[key] = {
                            "messages": items,
                            "expires_at": time.time() + self._ttl,
                        }
        except Exception as e:
            logger.debug("short-term warm_up failed: %s", e)

    def ttl_seconds(self, user_id: int, session_id: int) -> int:
        """
        返回 key 剩余存活秒数，供后台落库判定“临期”。

        被谁调用：memory/long_term.py 的 LongTermFlusher.flush_due（每周期扫描时）。
        参数：user_id/session_id 同上。
        返回：int——Redis 语义：-2 key 不存在，-1 存在但无 TTL，非负数为剩余秒数；
        进程内降级模式返回按 expires_at 估算的非负剩余秒或 -2；异常时返回 -2。
        """
        key = _key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                return int(client.ttl(key))
            with self._mem_lock:
                slot = self._mem.get(key)
                return max(0, int(slot["expires_at"] - time.time())) if slot else -2
        except Exception:
            return -2

    # ------------------------------------------------- 落库协同（长期记忆由短期记忆转变而来）

    def pending_count(self, user_id: int, session_id: int) -> int:
        """
        待落库消息条数：meta.total - meta.flushed。

        被谁调用：memory/long_term.py 的 flush_one（落库前判断是否有活）
        与 flush_due（条数兜底判定）。
        参数：user_id/session_id 同上。返回：int——非负待落库条数；meta 不存在
        或 Redis 异常时返回 0（按“无待落库”处理，避免误触发落库）。
        """
        client = self.client
        try:
            if client is not None:
                data = client.hgetall(_meta_key(user_id, session_id))
                if not data:
                    return 0
                return max(0, int(data.get("total", "0")) - int(data.get("flushed", "0")))
            with self._mem_lock:
                slot = self._mem.get(_key(user_id, session_id))
                if not slot:
                    return 0
                return max(0, slot.get("total", 0) - slot.get("flushed", 0))
        except Exception as e:
            logger.error("short-term pending_count failed: %s", e)
            return 0

    def get_title(self, user_id: int, session_id: int) -> str:
        """
        读取 meta 中暂存的会话标题（落库时随消息一并 upsert 到 history_information）。

        被谁调用：memory/long_term.py 的 flush_one。
        参数：user_id/session_id 同上。返回：str——无标题或异常时返回空串。
        """
        client = self.client
        try:
            if client is not None:
                return client.hget(_meta_key(user_id, session_id), "title") or ""
            with self._mem_lock:
                slot = self._mem.get(_key(user_id, session_id))
                return (slot or {}).get("title") or ""
        except Exception as e:
            logger.debug("short-term get_title failed: %s", e)
            return ""

    def pending_messages(self, user_id: int, session_id: int) -> List[Dict[str, str]]:
        """
        读取未落库的消息（时间升序）：取 list 尾部 pending 条。

        被谁调用：memory/long_term.py 的 flush_one（批量写 MySQL 的消息来源）；
        control/history_control.py 的 /history/detail（拼接到 MySQL 已落库部分
        之后，返回完整会话记录给前端）。
        参数：user_id/session_id 同上。返回：List[Dict[str,str]]——时间升序消息；
        无待落库或异常时返回 []。pending 超过现存条数（理论仅截断兜底失效时
        出现）时返回现存全部。
        """
        pending = self.pending_count(user_id, session_id)
        if pending <= 0:
            return []
        client = self.client
        try:
            if client is not None:
                raw = client.lrange(_key(user_id, session_id), -pending, -1)
                return [json.loads(x) for x in raw]
            with self._mem_lock:
                slot = self._mem.get(_key(user_id, session_id))
                if not slot:
                    return []
                msgs = [json.loads(x) for x in slot["messages"]]
                return msgs[-pending:] if pending < len(msgs) else msgs
        except Exception as e:
            logger.error("short-term pending_messages failed: %s", e)
            return []

    def mark_flushed(self, user_id: int, session_id: int) -> None:
        """
        落库成功后推进水位：flushed = total（保留 key，会话可能仍在活跃）。

        被谁调用：memory/long_term.py 的 flush_one 在“条数兜底落库”
        （drop_after=False）成功后调用；此后 pending_count 归零，直到有新对话追加。
        参数：user_id/session_id 同上。返回：None。异常仅记 error 日志。
        """
        client = self.client
        try:
            if client is not None:
                meta_key = _meta_key(user_id, session_id)
                total = int(client.hget(meta_key, "total") or "0")
                client.hset(meta_key, "flushed", total)
                return
            with self._mem_lock:
                slot = self._mem.get(_key(user_id, session_id))
                if slot:
                    slot["flushed"] = slot.get("total", 0)
        except Exception as e:
            logger.error("short-term mark_flushed failed: %s", e)

    def drop(self, user_id: int, session_id: int) -> None:
        """
        临期落库成功后删除短期记忆（长期记忆已接管；语义为短期→长期的正常转变，
        区别于用户删会话的 clear，但实现复用 clear 删 list+meta）。

        被谁调用：memory/long_term.py 的 flush_one 在“临期落库”
        （drop_after=True）成功后，以及无待落库消息的临期清理分支。
        参数：user_id/session_id 同上。返回：None。
        """
        self.clear(user_id, session_id)

    def scan_sessions(self) -> List[Tuple[int, int]]:
        """
        扫描所有存活会话（供后台落库任务遍历）。
        Redis：SCAN mem:short:*（游标分批 count=200，排除 :meta/:guard 后缀）；
        降级内存：遍历 _mem。

        被谁调用：memory/long_term.py 的 LongTermFlusher.flush_due
        （扫描周期 settings.SHORT_TERM_FLUSH_INTERVAL_SECONDS，默认 60 秒）。
        参数：无。返回：List[Tuple[int,int]]——(user_id, session_id) 列表；
        扫描异常时返回已收集部分（可能为空），不向调用方抛错。
        """
        client = self.client
        results: List[Tuple[int, int]] = []
        try:
            if client is not None:
                cursor = 0
                while True:
                    cursor, keys = client.scan(cursor, match=f"{_KEY_PREFIX}*", count=200)
                    for k in keys:
                        parsed = self._parse_key(k)
                        if parsed:
                            results.append(parsed)
                    if cursor == 0:
                        break
            else:
                with self._mem_lock:
                    for k in list(self._mem.keys()):
                        parsed = self._parse_key(k)
                        if parsed:
                            results.append(parsed)
        except Exception as e:
            logger.error("short-term scan_sessions failed: %s", e)
        return results

    @staticmethod
    def _parse_key(key: str) -> Optional[Tuple[int, int]]:
        """mem:short:{uid}:{sid} -> (uid, sid)；meta/guard 等派生 key 返回 None。"""
        if not key.startswith(_KEY_PREFIX):
            return None
        rest = key[len(_KEY_PREFIX):]
        parts = rest.split(":")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    # ------------------------------------------------- 进程内降级实现

    def _mem_append(self, key: str, items: List[str], title: str = None) -> None:
        """降级实现：把一轮两条 JSON 消息追加进进程内槽位（与 Redis 路径同语义）。

        被谁调用：append_round 在 client 为 None 或 Redis 写入抛异常时。
        参数：key——消息 list 完整键；items——已 json.dumps 的 user/assistant
        两条消息；title——可选会话标题（覆盖槽位标题供落库 upsert）。
        行为：超出 _max_messages 时截尾、total 累加 len(items)、
        expires_at 重置为 now+ttl（滑动续期）。返回：None。
        """
        with self._mem_lock:
            slot = self._mem.get(key) or {
                "messages": [], "expires_at": 0.0, "total": 0, "flushed": 0, "title": "",
            }
            slot["messages"].extend(items)
            if len(slot["messages"]) > self._max_messages:
                slot["messages"] = slot["messages"][-self._max_messages:]
            slot["total"] = slot.get("total", 0) + len(items)
            if title:
                slot["title"] = title
            slot["expires_at"] = time.time() + self._ttl  # 滑动续期
            self._mem[key] = slot

    def _mem_load(self, key: str) -> Optional[List[Dict[str, str]]]:
        """降级实现：读进程内槽位消息并惰性过期（语义对齐 Redis LRANGE + TTL）。

        被谁调用：load 在 client 为 None 时。
        参数：key——消息 list 完整键。
        返回：Optional[List[Dict[str,str]]]——时间升序消息；槽位不存在或已
        过期（顺带删除）返回 None，调用方按缓存未命中处理（回源 MySQL）。
        """
        with self._mem_lock:
            slot = self._mem.get(key)
            if not slot:
                return None
            if slot["expires_at"] <= time.time():
                self._mem.pop(key, None)
                return None
            return [json.loads(x) for x in slot["messages"]]


# 模块级全局单例：进程内唯一 ShortTermStore；首次调用 get_short_term_store()
# 时惰性构造（ChatService.__init__ 等多处共用），_store_lock 保护双重检查锁。
_store: Optional[ShortTermStore] = None
_store_lock = threading.Lock()


def get_short_term_store() -> ShortTermStore:
    """应用级单例工厂（双重检查锁，非每请求新建）。

    被谁调用：service/chat_service.py（ChatService.__init__）、
    memory/long_term.py（store 属性惰性取）、memory/context_memory.py
    （_load_messages 读写/回填）、control/history_control.py（详情拼接/删除清理）。
    返回：ShortTermStore——共享单例（内部惰性取全局 Redis 连接）。
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ShortTermStore()
    return _store


def reset_short_term_store_for_test() -> None:
    """测试辅助：重置单例（配合 fake Redis 注入）。"""
    global _store
    with _store_lock:
        _store = None
