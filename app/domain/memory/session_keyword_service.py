"""
模块：app.domain.memory.session_keyword_service —— 会话关键词累积服务（每轮对话提取
关键词，按会话隔离保存，注入模型输入防压缩丢失）。

作用：把分散在各轮（以及上下文压缩、RAG 汇总环节）产出的主题词累积为
会话级关键词集合，读取时渲染为【会话关键词】前缀注入 LLM prompt
（经 app/application/chat/chat_service → AgentService → app/domain/agents/ChatAgent 的
session_keywords 形参），让早期轮次的主题焦点在长对话中持续可见，
不被上下文压缩摘要稀释。数据链路：关键词累积 → Redis SET（滑动 TTL，
标记脏集合）→ 后台 daemon 线程定期整串 UPSERT 到 MySQL
（dao/session_keyword.SessionKeywordDAO，表 session_keywords，一行一
(user_id,session_id)）；Redis 未命中时回源 MySQL 并回填 Redis。

存储：
- Redis SET，key = mem:kw:{user_id}:{session_id}，SADD 追加去重 +
  EXPIRE 滑动续期（TTL 取 settings.CONTEXT_TTL_SECONDS，默认 7200 秒）；
- Redis 不可用时降级为进程内 dict（key = "{user_id}:{session_id}" -> set）；
- MySQL 为持久化回源：后台 daemon 线程每 settings.SESSION_KEYWORD_FLUSH_INTERVAL
  （默认 600 秒）把脏集合批量 UPSERT；
- 单会话关键词上限 settings.SESSION_KEYWORD_MAX（默认 100）；
  注入前缀字符上限 settings.SESSION_KEYWORD_INJECT_CHARS（默认 200）。

关键词严格按 (user_id, session_id) 二元组隔离，不同用户/不同会话绝不交叉。

主要成员：
- SessionKeywordService：关键词服务类（Redis SET + 脏集合追踪 + 后台落库
  线程，含进程内 dict 降级；MySQL 经 dao/session_keyword.SessionKeywordDAO）；
- get_session_keyword_service()：应用级单例工厂（双重检查锁）；
- reset_session_keyword_service_for_test()：测试辅助，重置单例。

被谁使用（全仓 import 位置）：
- app/application/chat/chat_service.py：ChatService.__init__ 持单例；handle/handle_stream
  每轮调 render_prefix 取注入文本；_save_information 每轮调
  extract_and_accumulate（jieba 抽取）；
- app/domain/memory/context_app.domain.memory.py：上下文 LLM 压缩成功后调 accumulate 累积摘要关键词；
- app/application/chat/agent_service.py：SummaryAgent 汇总相关性通过后调 accumulate
  累积汇总关键词；
- dao/soft_delete.py：软删会话时调 clear 联动清理；
- control/app.py 的 lifespan 启动钩子：start_background_flusher 启守护线程；
- app/domain/memory/session_rollover.py：滚换时直接按 Redis key 复制 SET 到新会话。
"""
import logging
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from core.config import settings
from app.infrastructure.redis.redis_client import get_redis
from app.infrastructure.persistence.repositories.session_keyword import SessionKeywordDAO

logger = logging.getLogger(__name__)

# 模块级常量：关键词 SET 的 Redis key 前缀，完整键 mem:kw:{user_id}:{session_id}
_KEY_PREFIX = "mem:kw:"


def _redis_key(user_id: int, session_id: int) -> str:
    """拼接 Redis SET 键：mem:kw:{user_id}:{session_id}。

    参数：user_id (int，JWT)、session_id (int，请求体)。返回：str。
    """
    return f"{_KEY_PREFIX}{user_id}:{session_id}"


def _mem_key(user_id: int, session_id: int) -> str:
    """拼接进程内降级字典的键："{user_id}:{session_id}"（不带业务前缀）。"""
    return f"{user_id}:{session_id}"


class SessionKeywordService:
    """
    会话关键词累积服务（应用级单例，经 get_session_keyword_service 获取；
    chat_service、agent_service、context_memory、soft_delete 共用同一实例）。

    存储与策略：
    - Redis 可用：关键词存 SET（天然去重，SADD + EXPIRE 滑动续期，TTL 取
      settings.CONTEXT_TTL_SECONDS 默认 7200 秒）；变更的会话记入进程内
      _dirty 集合，后台线程 flush_to_mysql 整串 UPSERT 到 MySQL
      session_keywords（周期 settings.SESSION_KEYWORD_FLUSH_INTERVAL，
      默认 600 秒；注意多副本下未用 Redis 周期锁，UPSERT 按唯一键幂等）；
    - Redis 不可用/异常：降级为进程内 dict（_mem，set 语义 + 上限截断），
      仍标记 _dirty 供后台落库；
    - 读取未命中 Redis 时回源 MySQL（SessionKeywordDAO.get）并回填 Redis；
    - 上限 settings.SESSION_KEYWORD_MAX（默认 100）、注入字符上限
      settings.SESSION_KEYWORD_INJECT_CHARS（默认 200）。

    实例化位置：生产仅由模块底部 get_session_keyword_service() 无参构造；
    client/dao 形参保留给测试注入。
    """

    def __init__(self, client=None, dao: SessionKeywordDAO = None):
        """
        形参（生产由单例工厂无参构造，以下仅测试注入用）：
        - client：Redis 客户端，None 时由 client 属性惰性取
          core.redis_client.get_redis() 全局连接；
        - dao：会话关键词 DAO（dao/session_keyword.SessionKeywordDAO，
          MySQL session_keywords 表 get/upsert/软删），None 时自行 new。
        关键属性去向：_ttl/_max_keywords/_inject_chars 来自 settings
        （7200/100/200）；_mem + _mem_lock 为降级存储；_dirty +
        _dirty_lock 追踪有变更待落库的会话（后台线程 flush_to_mysql 消费）；
        _flusher_started 保证后台线程幂等启动一次。
        """
        self._client = client
        self._dao = dao or SessionKeywordDAO()
        self._ttl = settings.CONTEXT_TTL_SECONDS
        self._max_keywords = settings.SESSION_KEYWORD_MAX
        self._inject_chars = settings.SESSION_KEYWORD_INJECT_CHARS
        # 降级内存：mem_key -> set of keywords
        self._mem: Dict[str, Set[str]] = {}
        self._mem_lock = threading.Lock()
        # 脏集合追踪（后台落库用）
        self._dirty: Set[str] = set()
        self._dirty_lock = threading.Lock()
        self._flusher_started = False

    @property
    def client(self):
        """惰性获取 Redis 客户端：注入实例优先，否则取全局 get_redis()；
        返回 None 表示 Redis 不可用，各方法据此走进程内 dict 降级分支。"""
        if self._client is not None:
            return self._client
        return get_redis()

    def accumulate(self, user_id: int, session_id: int, keywords: List[str]) -> None:
        """累积一批会话关键词（去空白、SET 去重、滑动续期并标记脏）。

        被谁调用：app/application/chat/chat_service.py 的 _save_information（经
        extract_and_accumulate，每轮 jieba 抽取后）；
        app/domain/memory/context_app.domain.memory.py 的 _maybe_compress（LLM 压缩产出关键词）；
        app/application/chat/agent_service.py 汇总相关性通过后（SummaryAgent 关键词）。
        参数：user_id (int，JWT)、session_id (int，请求体)；
        keywords (List[str])——本批关键词（来源 jieba/LLM，元素自动 strip
        并丢弃空串）。
        返回：None。数据去向：Redis SET（后台线程再 UPSERT MySQL）；
        Redis 异常时降级写进程内 set（超 _max_keywords 时截尾保留最新部分）。
        """
        if not user_id or not session_id or not keywords:
            return
        filtered = [kw.strip() for kw in keywords if kw and kw.strip()]
        if not filtered:
            return
        rkey = _redis_key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                # pipeline 原子提交 SADD + EXPIRE：追加去重与滑动续期一次往返
                pipe = client.pipeline()
                pipe.sadd(rkey, *filtered)
                pipe.expire(rkey, self._ttl)
                pipe.execute()
                self._mark_dirty(user_id, session_id)
                return
        except Exception as e:
            # Redis 故障：降级内存，保证关键词不丢（后台仍可落 MySQL）
            logger.debug("keyword accumulate Redis failed, falling back to memory: %s", e)
        with self._mem_lock:
            mk = _mem_key(user_id, session_id)
            if mk not in self._mem:
                self._mem[mk] = set()
            self._mem[mk].update(filtered)
            if len(self._mem[mk]) > self._max_keywords:
                # set 无序，截尾仅控规模；最终展示顺序不保证与时间一致
                self._mem[mk] = set(list(self._mem[mk])[-self._max_keywords:])
        self._mark_dirty(user_id, session_id)

    def get_keywords(self, user_id: int, session_id: int) -> List[str]:
        """读取会话全部关键词：Redis SET → 回源 MySQL → 进程内降级，逐级回退。

        被谁调用：render_prefix（拼注入前缀）；flush_to_mysql 不调本方法
        （直接 SMEMBERS 以免触发回源干扰）。
        参数：user_id (int，JWT)、session_id (int，请求体)。
        返回：List[str]——关键词列表（SET 无序，顺序不保证）；
        Redis 命中非空直接返回；Redis 空/异常时查 MySQL（keywords 列以
        顿号拼接存储），命中则回填 Redis 后返回；都没有时返回
        进程内降级集合或空列表。
        """
        if not user_id or not session_id:
            return []
        rkey = _redis_key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                members = client.smembers(rkey)
                if members:
                    return list(members)
        except Exception as e:
            logger.debug("keyword get_keywords Redis failed: %s", e)
        # Redis 空或失败 → 回源 MySQL
        raw = self._dao.get(user_id, session_id)
        if raw:
            kws = [kw.strip() for kw in raw.split("\u3001") if kw.strip()]
            if kws:
                try:
                    if client is not None:
                        # 回填 Redis：下次读取免回源，pipeline 保证 SADD+EXPIRE 原子
                        pipe = client.pipeline()
                        pipe.sadd(rkey, *kws)
                        pipe.expire(rkey, self._ttl)
                        pipe.execute()
                except Exception:
                    pass  # 回填失败不影响本次返回
                return kws
        # 降级内存
        with self._mem_lock:
            mk = _mem_key(user_id, session_id)
            s = self._mem.get(mk)
            return list(s) if s else []

    def render_prefix(self, user_id: int, session_id: int) -> str:
        """渲染【会话关键词】注入前缀（超字符上限截断）。

        被谁调用：app/application/chat/chat_service.py 的 handle/handle_stream
        （每轮构建 prompt 时，注入 app/domain/agents/ChatAgent 的 session_keywords）。
        参数：user_id (int，JWT)、session_id (int，请求体)。
        返回：str——【会话关键词】开头的顿号拼接串；无关键词返回 ""；
        拼接长度超 settings.SESSION_KEYWORD_INJECT_CHARS（默认 200）时
        按字符硬截断（代码中中文以 \\uXXXX 转义书写，语义不变）。
        """
        kws = self.get_keywords(user_id, session_id)
        if not kws:
            return ""
        joined = "\u3001".join(kws)
        if len(joined) > self._inject_chars:
            joined = joined[:self._inject_chars]
        return f"\u3010\u4f1a\u8bdd\u5173\u952e\u8bcd\u3011{joined}"

    def extract_and_accumulate(
        self, user_id: int, session_id: int, user_text: str, ai_text: str
    ) -> None:
        """用 jieba 从本轮问答抽取 top5 关键词并累积（无 jieba/文本过短则跳过）。

        被谁调用：app/application/chat/chat_service.py 的 ChatService._save_information
        （每轮拿到完整回答后一次）。
        参数：user_id (int，JWT)、session_id (int，请求体)；
        user_text/ai_text (str)——本轮用户提问与助手回答（来源对话请求体
        与 AgentService 产出），拼接后不足 4 字不抽取。
        返回：None。抽取结果交 accumulate 落 Redis/MySQL。
        异常：jieba 未安装直接静默返回（可选依赖）；抽取异常仅记 debug 日志。
        """
        try:
            import jieba.analyse
        except ImportError:
            return
        combined = f"{user_text} {ai_text}".strip()
        if not combined or len(combined) < 4:
            return
        try:
            tags = jieba.analyse.extract_tags(combined, topK=5)
            if tags:
                self.accumulate(user_id, session_id, tags)
        except Exception as e:
            logger.debug("jieba extract failed, skipping: %s", e)

    def clear(self, user_id: int, session_id: int) -> None:
        """删除会话关键词：Redis SET + 进程内副本 + MySQL 软删，三处联动清理。

        被谁调用：dao/soft_delete.py 的软删会话流程（用户删除历史会话时）；
        与 short_term.clear、context_app.domain.memory.clear 在同一删除链路中。
        参数：user_id (int，JWT)、session_id (int，请求体)。返回：None。
        异常：Redis/DAO 各自 try，失败仅记 debug 日志，不向上抛错
        （MySQL 软删失败不阻断删会话主流程）。
        """
        if not user_id or not session_id:
            return
        rkey = _redis_key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                client.delete(rkey)
        except Exception as e:
            logger.debug("keyword clear Redis failed: %s", e)
        with self._mem_lock:
            self._mem.pop(_mem_key(user_id, session_id), None)
        try:
            self._dao.delete(user_id, session_id)
        except Exception as e:
            logger.debug("keyword clear DAO failed: %s", e)

    # ============================================================== 后台落库

    def _mark_dirty(self, user_id: int, session_id: int) -> None:
        """把会话加入脏集合（有关键词变更待后台落库 MySQL）。

        被谁调用：accumulate（Redis 成功与降级两条路径都调）。
        参数：user_id/session_id。返回：None。
        """
        with self._dirty_lock:
            self._dirty.add(_mem_key(user_id, session_id))

    def flush_to_mysql(self) -> None:
        """把脏会话的关键词整串 UPSERT 到 MySQL（一轮快照式批量落库）。

        被谁调用：后台守护线程 _loop（周期 settings.SESSION_KEYWORD_FLUSH_INTERVAL
        默认 600 秒；启动后先睡一个周期再首轮执行）。
        逻辑：先取脏集合快照并清空（落库期间的新变更会进入新一轮脏集合，
        不丢）；逐会话从 Redis SMEMBERS（空则取进程内集合）读关键词，
        超 _max_keywords 截尾，以顿号拼接后 SessionKeywordDAO.upsert
        （INSERT ... ON DUPLICATE KEY UPDATE，按唯一键幂等整串覆盖）。
        返回：None。单会话解析/读取异常跳过该会话，不影响其他会话。
        """
        with self._dirty_lock:
            # 快照+立即清空：落库耗时较长，期间 accumulate 的新变更进新脏集合
            dirty_keys = list(self._dirty)
            self._dirty.clear()
        client = self.client
        for mk in dirty_keys:
            parts = mk.split(":")
            if len(parts) != 2:
                continue
            try:
                uid, sid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            kws = []
            try:
                if client is not None:
                    members = client.smembers(_redis_key(uid, sid))
                    if members:
                        kws = list(members)
            except Exception:
                pass
            if not kws:
                # Redis 不可用/读空：退到进程内降级集合取数
                with self._mem_lock:
                    s = self._mem.get(mk)
                    if s:
                        kws = list(s)
            if not kws:
                continue
            if len(kws) > self._max_keywords:
                kws = kws[-self._max_keywords:]
            joined = "\u3001".join(kws)
            self._dao.upsert(uid, sid, joined)

    def start_background_flusher(self) -> None:
        """启动后台落库守护线程（幂等，仅启动一次）。

        被谁调用：control/app.py 的 lifespan 启动钩子（每进程一次）。
        周期：settings.SESSION_KEYWORD_FLUSH_INTERVAL（默认 600 秒）；
        线程先 sleep 一个周期再首轮 flush（与另两个 flusher 启动即扫不同）。
        返回：None。单周期异常仅 warning，不杀死 daemon 线程。
        注意：本线程未使用 core.locks 周期锁，多副本下同周期可能重复 UPSERT，
        依赖 MySQL 唯一键幂等保证正确性（同内容整串覆盖，无副作用）。
        """
        if self._flusher_started:
            return
        self._flusher_started = True
        interval = settings.SESSION_KEYWORD_FLUSH_INTERVAL

        def _loop():
            while True:
                time.sleep(interval)
                try:
                    self.flush_to_mysql()
                except Exception as e:
                    # 线程级兜底：单周期异常不杀死守护线程
                    logger.warning("keyword flusher cycle failed: %s", e)

        t = threading.Thread(target=_loop, name="kw-flusher", daemon=True)
        t.start()
        logger.info("SessionKeywordService background flusher started (interval=%ds)", interval)

    def scan_sessions(self) -> List[Tuple[int, int]]:
        """扫描所有存有关键词的会话（Redis SCAN 或遍历降级 dict）。

        被谁调用：当前生产链路无调用（后台落库走 _dirty 而非全量扫描），
        保留给运维/补偿任务全量重落库使用。
        返回：List[Tuple[int,int]]——(user_id, session_id) 列表；
        Redis 游标分批 count=200，扫描异常返回已收集部分。
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
                    for mk in list(self._mem.keys()):
                        parts = mk.split(":")
                        if len(parts) == 2:
                            try:
                                results.append((int(parts[0]), int(parts[1])))
                            except ValueError:
                                pass
        except Exception as e:
            logger.error("keyword scan_sessions failed: %s", e)
        return results

    @staticmethod
    def _parse_key(key: str) -> Optional[Tuple[int, int]]:
        """mem:kw:{uid}:{sid} -> (uid, sid)；格式不符/非数字返回 None。

        参数：key (str)——Redis SCAN 返回的原始键。
        返回：Optional[Tuple[int,int]]——解析出的 (user_id, session_id)。
        """
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


# 模块级全局单例：进程内唯一 SessionKeywordService；首次调用
# get_session_keyword_service() 时惰性构造（ChatService.__init__ 等），
# _service_lock 保护双重检查锁。
_service: Optional[SessionKeywordService] = None
_service_lock = threading.Lock()


def get_session_keyword_service() -> SessionKeywordService:
    """应用级单例工厂（双重检查锁，非每请求新建）。

    被谁调用：app/application/chat/chat_service.py（ChatService.__init__）、
    app/domain/memory/context_app.domain.memory.py（压缩侧写）、app/application/chat/agent_service.py
    （汇总关键词）、dao/soft_delete.py（删会话清理）、control/app.py
    lifespan（启动后台落库线程）。
    返回：SessionKeywordService——共享单例（内部惰性取全局 Redis 连接）。
    """
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = SessionKeywordService()
    return _service


def reset_session_keyword_service_for_test() -> None:
    """测试辅助：重置单例（配合 fake Redis/DAO 重新注入构造）。"""
    global _service
    with _service_lock:
        _service = None
