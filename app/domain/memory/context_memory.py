"""
模块：app.domain.memory.context_memory —— 上下文记忆（会话级压缩摘要，Redis 缓存）。

作用：在短期记忆（Redis 原文）与长期记忆（MySQL session_information）之上，
为每轮对话构建注入 LLM 的上下文文本——【早期对话摘要】+【最近 N 轮原文】；
当对话轮数/字符数超过阈值时调用 LLM 压缩旧轮次，并以水位（compressed_rounds）
保证压缩幂等。数据链路：短期记忆（Redis，滑动 TTL）→ 未命中回源 MySQL 长期
记忆（dao/session.SessionDAO.get_session_detail）并 warm_up 回填短期记忆 →
压缩产出的摘要留在本模块 Redis hash，关键词侧写 session_keyword_service。
注意：本模块不参与文档 RAG 检索（app/domain/agents/retrieval.py 的 RAG 读 Chroma
文档向量库），只负责对话历史的上下文注入。

规则（产品约定）：
- 最近 N 轮对话【永远原文代入、不压缩】；N 按近期内容多少动态选择：
  近期对话内容多 → 3 轮（settings.CONTEXT_ROUNDS_DENSE，默认 3），
  内容少 → 5 轮（settings.CONTEXT_ROUNDS_SPARSE，默认 5），
  疏密判定阈值 settings.CONTEXT_DENSE_CHARS_PER_ROUND（默认每轮 800 字）。
- 超过 N 轮的更早信息（辅助条件），或 摘要+近期原文达到上下文窗口字符上限
  （主要条件 settings.CONTEXT_WINDOW_CHARS，默认 6000），触发压缩：把窗口外
  且尚未压缩的旧轮次交给 LLM 提取关键信息，与历史摘要合并为一段【早期对话摘要】。
- 幂等防重：已压缩轮数水位 compressed_rounds 与摘要一同缓存（key 的 TTL 取
  settings.CONTEXT_TTL_SECONDS，默认 7200 秒，随会话滑动、略长于短期记忆）。
  网络中断恢复后重建上下文时，已压缩轮次直接读缓存摘要，绝不重复调用 LLM；
  只有水位之外的新旧轮次才会触发新的压缩调用。
- 缓存未命中时回源 MySQL 长期记忆（session_information）重建并回填短期记忆。

注入格式（chat_service 拼到模型输入）：
    【早期对话摘要】…
    【最近N轮对话】
    用户：…
    助手：…

主要成员：
- ContextMemoryService：上下文记忆服务类（Redis hash 存 summary/watermark，
  含进程内 dict 降级；压缩调 model_llm.gateway）；
- get_context_memory_service()：应用级单例工厂（双重检查锁）；
- reset_context_memory_service_for_test()：测试辅助，重置单例；
- _COMPRESS_PROMPT：压缩用 LLM 提示词模板（输出 JSON：summary + keywords）。

被谁使用（全仓 import 位置）：
- app/application/chat/chat_service.py：ChatService.__init__ 持有单例；_get_recent_history
  每轮调 build_context 取注入文本（再交 AgentService → app/domain/agents/ChatAgent）；
- app/api/v1/history.py：删除会话确认接口调 clear 联动清理摘要缓存；
- app/domain/memory/context_memory 内部压缩成功后回调
  app/domain/memory/session_keyword_service.accumulate 累积压缩关键词。
"""
import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from core.config import settings
from app.infrastructure.redis.redis_client import get_redis
from app.infrastructure.persistence.repositories.session import SessionDAO
from app.domain.memory.short_term import get_short_term_store

logger = logging.getLogger(__name__)

# 模块级常量：上下文状态 hash 的 Redis key 前缀，
# 完整键形如 mem:ctx:{user_id}:{session_id}，hash 字段 summary/compressed_rounds。
_KEY_PREFIX = "mem:ctx:"

# 模块级常量：压缩提示词。进程启动 import 时即定义（纯字符串常量，无初始化成本）；
# 占位符 {old_summary}/{old_rounds} 由 _invoke_compressor 填充；
# {{ }} 为 str.format 转义，输出给模型的是单层 JSON 花括号。
_COMPRESS_PROMPT = """你是对话摘要助手。请基于"历史摘要"与"新增的较早对话"，输出JSON。
要求：
1. 合并去重，保留关键事实：用户身份/目标、已确认的约束与偏好、已问过的核心问题与结论、待办/未解决事项；
2. 摘要用简短要点分条表述，总字数不超过 300 字；
3. 提取 3-5 个核心关键词（名词/动名词），反映该段对话的主题焦点；
4. 仅返回JSON字符串，无任何多余内容。

JSON输出格式：
{{"summary": "更新后的摘要（100-300字）", "keywords": ["关键词1", "关键词2", "关键词3"]}}

历史摘要：
{old_summary}

新增的较早对话：
{old_rounds}"""


def _key(user_id: int, session_id: int) -> str:
    """拼接上下文状态 hash 的 key：mem:ctx:{user_id}:{session_id}。

    参数：user_id（JWT 注入的用户 ID）、session_id（当前会话 ID，请求体）。
    返回：str——Redis hash key。
    """
    return f"{_KEY_PREFIX}{user_id}:{session_id}"


class ContextMemoryService:
    """
    上下文记忆服务（应用级单例，经 get_context_memory_service 获取；
    app/application/chat/chat_service.py、app/api/v1/history.py 共用同一实例）。

    存储策略：
    - Redis 可用：状态存 Redis hash（mem:ctx:{uid}:{sid}，字段 summary 与
      compressed_rounds），写入时 pipeline 原子执行 HSET + EXPIRE，TTL 取
      settings.CONTEXT_TTL_SECONDS（默认 7200 秒，随每次压缩滑动续期）；
    - Redis 不可用：降级为进程内 dict（_mem，按 expires_at 手工过期），
      仅保证单机开发语义，多副本部署必须配置 Redis；
    - 消息原文不落本类：读短期记忆（app.domain.memory.short_term），未命中回源 MySQL
      session_information（长期记忆，经 SessionDAO）并回填短期记忆。

    实例化位置：生产路径仅由模块底部 get_context_memory_service() 无参构造
    （双重检查锁单例）；形参全部保留给测试注入 fake client / 假 compressor。
    """

    def __init__(
        self,
        client=None,
        compressor: Optional[Callable[[str], str]] = None,
        session_dao: SessionDAO = None,
        ttl: int = None,
    ):
        """
        形参（生产由单例工厂无参构造，以下仅测试注入用）：
        - client：Redis 客户端，None 时由 client 属性惰性取
          core.redis_client.get_redis()（来源：应用启动时初始化的全局连接）；
        - compressor：压缩函数 (prompt:str)->str，None 表示走默认 LLM 网关
          model_llm.gateway.build_chat_model；测试可注入假实现免调 LLM；
        - session_dao：会话 DAO（dao/session.SessionDAO），缓存未命中时回源
          MySQL 长期记忆；None 时自行 new SessionDAO()；
        - ttl：上下文 hash 过期秒数，None 取 settings.CONTEXT_TTL_SECONDS
          （默认 7200）。
        关键属性去向：_client/_compressor/_session_dao/_ttl 供 build_context
        全链路读写使用；_mem + _mem_lock 为 Redis 不可用时的进程内降级状态
        （key -> {"summary","watermark","expires_at"}）。
        """
        self._client = client
        # compressor(str)->str：默认走 LLM 网关，测试可注入假实现
        self._compressor = compressor
        self._session_dao = session_dao or SessionDAO()
        self._ttl = ttl if ttl is not None else settings.CONTEXT_TTL_SECONDS
        # 降级内存：key -> {"summary": str, "watermark": int, "expires_at": float}
        self._mem: Dict[str, dict] = {}
        self._mem_lock = threading.Lock()

    @property
    def client(self):
        """惰性获取 Redis 客户端：注入实例优先，否则取全局 get_redis()；
        返回 None 表示 Redis 不可用，各方法据此走 _mem 降级分支。"""
        if self._client is not None:
            return self._client
        return get_redis()

    # ============================================================== 对外主入口

    def build_context(self, user_id: int, session_id: int) -> str:
        """
        构建本轮对话注入用的上下文文本（摘要 + 最近 N 轮原文）。
        当前轮的用户输入尚未落库，不在 messages 内（由调用方作为 query 传入）。

        被谁调用：app/application/chat/chat_service.py 的 ChatService._get_recent_history
        （handle/handle_stream 每轮对话开始时一次），返回文本作为 history 注入
        AgentService → app/domain/agents/ChatAgent 的模型输入。
        参数：
        - user_id (int)：JWT 注入的用户 ID；
        - session_id (int)：当前会话 ID（对话请求体，per-user 序列）。
        返回：str——"【早期对话摘要】…\\n【最近N轮对话】…"注入文本，
        去向：LLM prompt；无入参/无历史消息/无配对轮次时返回 ""（调用方降级
        为无历史对话，不阻断主链路）。
        异常：内部各步骤自行捕获并降级（回源失败返回 []、压缩失败保留原摘要），
        本方法总体不向调用方抛错。
        """
        if not user_id or not session_id:
            return ""
        messages = self._load_messages(user_id, session_id)
        if not messages:
            return ""

        rounds = self._pair_rounds(messages)
        if not rounds:
            return ""
        total = len(rounds)

        # 1) 动态决定近期保留轮数 N（内容多 3 轮 / 内容少 5 轮）
        keep_n = self._choose_keep_rounds(rounds)

        # 2) 读取摘要/水位，必要时压缩（压缩结果写回 Redis，并侧写关键词）
        summary, watermark = self._load_state(user_id, session_id)
        summary, watermark = self._maybe_compress(
            user_id, session_id, rounds, keep_n, summary, watermark
        )

        # 3) 最近 N 轮原文（永不压缩）；N >= 总轮数时取全部
        recent = rounds[-keep_n:] if keep_n < total else rounds
        recent_text = self._render_rounds(recent)

        parts = []
        if summary:
            parts.append(f"【早期对话摘要】\n{summary}")
        if recent_text:
            parts.append(f"【最近{len(recent)}轮对话】\n{recent_text}")
        return "\n".join(parts).strip()

    def get_summary(self, user_id: int, session_id: int) -> str:
        """读取当前缓存的早期摘要文本（不触发压缩）。

        被谁调用：供调试/恢复端点查看当前摘要（当前生产链路无强制依赖，
        ChatService.recover 依赖的是摘要水位的幂等性而非本方法）。
        参数：user_id (int，JWT)、session_id (int，请求体)。
        返回：str——缓存摘要；无缓存/异常时返回空串。
        """
        return self._load_state(user_id, session_id)[0]

    def clear(self, user_id: int, session_id: int) -> None:
        """删除会话时联动清理上下文缓存（Redis hash + 进程内降级副本）。

        被谁调用：app/api/v1/history.py 的 /history/delete/confirm
        （与 short_term.clear 同处调用，用户删除历史会话时联动）。
        参数：user_id (int，JWT)、session_id (int，请求体)。返回：None。
        异常：Redis 删除失败仅记 debug 日志，仍尽力清理进程内副本，不向上抛错。
        """
        if not user_id or not session_id:
            return
        key = _key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                client.delete(key)
        except Exception as e:
            logger.debug("context clear failed: %s", e)
        with self._mem_lock:
            self._mem.pop(key, None)

    # ============================================================== 消息加载

    def _load_messages(self, user_id: int, session_id: int) -> List[Dict[str, str]]:
        """加载会话全部消息：短期记忆（Redis）优先；未命中回源 MySQL 并回填。

        被谁调用：build_context。
        参数：user_id (int，JWT)、session_id (int，请求体)。
        返回：List[Dict[str,str]]——时间升序的 {"role","content"} 消息列表；
        短期未命中且回源异常时返回 []（build_context 据此产出空上下文）。
        数据链路：Redis 短期记忆 → 未命中经 SessionDAO.get_session_detail 读
        MySQL session_information（长期记忆，DAO 内部严格按归属过滤）→
        ShortTermStore.warm_up 回填短期记忆。
        """
        store = get_short_term_store()
        messages = store.load(user_id, session_id)
        if messages is not None:
            return messages
        try:
            rows = self._session_dao.get_session_detail(user_id, session_id)
        except Exception as e:
            # 回源 MySQL 失败：降级为空上下文，绝不阻断本轮对话
            logger.error("context fallback to MySQL failed: %s", e)
            return []
        # DAO 返回 role/content/created_at，且严格按归属过滤；升序
        messages = [{"role": r.get("role"), "content": r.get("content", "")} for r in rows]
        store.warm_up(user_id, session_id, messages)
        return messages

    # ============================================================== 轮次/窗口

    @staticmethod
    def _pair_rounds(messages: List[Dict[str, str]]) -> List[List[Dict[str, str]]]:
        """
        消息按 (user, assistant) 配对成轮。落库是一轮两条同事务写入，
        容错：跳过无法配对的残余消息（如历史脏数据）。

        被谁调用：build_context。
        参数：messages——_load_messages 返回的时间升序消息列表。
        返回：List[List[Dict]]——每元素为 [user 消息, assistant 消息]；
        配对失败的残余消息被跳过（不抛错），去向：保留轮数判定与压缩区间计算。
        """
        rounds: List[List[Dict[str, str]]] = []
        i = 0
        n = len(messages)
        while i + 1 < n:
            if messages[i].get("role") == "user" and messages[i + 1].get("role") == "assistant":
                rounds.append([messages[i], messages[i + 1]])
                i += 2
            else:
                i += 1
        return rounds

    @staticmethod
    def _rounds_text(rounds: List[List[Dict[str, str]]]) -> str:
        """把若干轮渲染为纯文本（每轮两行：用户：…／助手：…）。

        被谁调用：_maybe_compress（拼待压缩旧轮次、算近期窗口字符数）与
        _render_rounds。参数：轮次列表。返回：str——换行拼接的文本，
        去向：压缩提示词 {old_rounds} 或注入文本的最近 N 轮段。
        """
        lines = []
        for u, a in rounds:
            lines.append(f"用户：{u.get('content', '')}")
            lines.append(f"助手：{a.get('content', '')}")
        return "\n".join(lines)

    def _render_rounds(self, rounds: List[List[Dict[str, str]]]) -> str:
        """渲染最近 N 轮原文（当前等价转发 _rounds_text，保留为独立覆写点）。

        被谁调用：build_context。参数：最近 N 轮。返回：str——注入文本。
        """
        return self._rounds_text(rounds)

    def _choose_keep_rounds(self, rounds: List[List[Dict[str, str]]]) -> int:
        """根据最近最多 sparse 轮的平均每轮字符数，判定内容多/少，返回 3 或 5。

        被谁调用：build_context 步骤 1。
        参数：rounds——会话全部已配对轮次。
        返回：int——平均每轮字符数 ≥ settings.CONTEXT_DENSE_CHARS_PER_ROUND
        （默认 800）判定为“内容多”，返回 settings.CONTEXT_ROUNDS_DENSE
        （默认 3）；否则返回 settings.CONTEXT_ROUNDS_SPARSE（默认 5）。
        采样窗口固定取最近 CONTEXT_ROUNDS_SPARSE 轮（不足则全部参与平均）。
        """
        window = rounds[-settings.CONTEXT_ROUNDS_SPARSE:]
        total_chars = sum(
            len(u.get("content", "")) + len(a.get("content", "")) for u, a in window
        )
        avg = total_chars / max(len(window), 1)
        return settings.CONTEXT_ROUNDS_DENSE if avg >= settings.CONTEXT_DENSE_CHARS_PER_ROUND else settings.CONTEXT_ROUNDS_SPARSE

    # ============================================================== 压缩

    def _maybe_compress(
        self,
        user_id: int,
        session_id: int,
        rounds: List[List[Dict[str, str]]],
        keep_n: int,
        summary: str,
        watermark: int,
    ):
        """
        按水位与窗口判定是否压缩，必要时调 LLM 合并摘要并落缓存。

        被谁调用：build_context 步骤 2（每轮构建上下文时）。
        需要压缩的旧轮次区间为 [watermark, total-keep_n)。
        触发条件（任一）：
          辅助：存在超出最近 N 轮且未压缩的轮次（boundary > watermark）；
          主要：摘要+最近N轮原文达到窗口字符上限 settings.CONTEXT_WINDOW_CHARS
          （默认 6000，且确有旧轮次可压）。
        最近 N 轮永不进入压缩区间。
        参数：user_id/session_id 来源同 build_context；rounds 为全部配对轮次；
        keep_n 为本轮动态保留轮数；summary/watermark 为 _load_state 读到的
        缓存摘要与已压缩轮数水位。
        返回：(summary, watermark)——未触发或压缩失败时原样返回；成功压缩
        返回新摘要与新水位（=boundary），并已写回 Redis（TTL 滑动续期）。
        副作用：LLM 同时产出的关键词 best-effort 侧写
        session_keyword_service.accumulate（失败仅 debug 日志，不影响压缩）。
        """
        total = len(rounds)
        boundary = max(0, total - keep_n)
        eligible = rounds[watermark:boundary]
        if not eligible:
            # 水位之外没有旧轮次：绝不重复压缩（中断恢复幂等的关键分支）
            return summary, watermark

        recent_len = len(self._rounds_text(rounds[-keep_n:]))
        window_exceeded = (len(summary) + recent_len) >= settings.CONTEXT_WINDOW_CHARS
        beyond_window = boundary > watermark  # 辅助条件：有超出 N 轮的旧轮次
        if not (window_exceeded or beyond_window):
            return summary, watermark

        old_rounds_text = self._rounds_text(eligible)
        new_summary, new_keywords = self._invoke_compressor(summary, old_rounds_text)
        if not new_summary:
            # LLM 失败/返回空：保留旧摘要与旧水位，下轮重试，不阻断注入
            return summary, watermark

        if new_keywords:
            try:
                # 压缩抽取的关键词累积进会话关键词服务（Redis → 后台落 MySQL），
                # 供后续轮次以【会话关键词】注入，防止摘要压缩丢失主题词
                from app.domain.memory.session_keyword_service import get_session_keyword_service
                get_session_keyword_service().accumulate(user_id, session_id, new_keywords)
            except Exception as e:
                logger.debug("keyword accumulate from compression failed: %s", e)

        new_watermark = boundary
        self._save_state(user_id, session_id, new_summary, new_watermark)
        logger.info(
            "context compressed: uid=%s sid=%s rounds %s->%s, summary_len=%s",
            user_id, session_id, watermark, new_watermark, len(new_summary),
        )
        return new_summary, new_watermark

    def _invoke_compressor(self, old_summary: str, old_rounds_text: str) -> Tuple[str, List[str]]:
        """调用 LLM 把旧摘要与新增旧轮次合并为新摘要 + 关键词。

        被谁调用：_maybe_compress。
        参数：old_summary (str)——现有缓存摘要（空串时填“（无）”）；
        old_rounds_text (str)——本轮待压缩区间渲染文本。
        返回：Tuple[str, List[str]]——(新摘要正文, 关键词列表)；
        模型返回合法 JSON 时解析 summary/keywords；返回非 JSON 纯文本时
        整体作为摘要、关键词为空；任何异常返回 ("", [])，调用方据此放弃本次压缩。
        异常：LLM 调用/网络异常仅记 error 日志并返回空元组，不向上抛错。
        """
        try:
            if self._compressor is not None:
                text = self._compressor(
                    _COMPRESS_PROMPT.format(
                        old_summary=old_summary or "（无）",
                        old_rounds=old_rounds_text,
                    )
                )
            else:
                from app.infrastructure.llm.gateway import build_chat_model

                llm = build_chat_model()
                resp = llm.invoke(
                    _COMPRESS_PROMPT.format(
                        old_summary=old_summary or "（无）",
                        old_rounds=old_rounds_text,
                    )
                )
                text = getattr(resp, "content", "") or str(resp)
            text = (text or "").strip()
            if not text:
                return "", []
            try:
                # 正常路径：模型按约定返回 JSON {"summary":..., "keywords":[...]}
                data = json.loads(text)
                summary = (data.get("summary", "") or "").strip()
                keywords = data.get("keywords", []) or []
                if isinstance(keywords, list):
                    keywords = [str(kw).strip() for kw in keywords if kw]
                else:
                    keywords = []
                return summary, keywords
            except (json.JSONDecodeError, TypeError, AttributeError):
                # 模型未严格遵循 JSON 约定：退化为“整段文本即摘要”，关键词留空
                return text, []
        except Exception as e:
            logger.error("context compression LLM call failed: %s", e)
            return "", []

    # ============================================================== 状态读写

    def _load_state(self, user_id: int, session_id: int):
        """读取缓存的摘要与压缩水位（Redis hash 优先，失败/不可用走进程内降级）。

        被谁调用：build_context、get_summary。
        参数：user_id (int，JWT)、session_id (int，请求体)。
        返回：Tuple[str, int]——(summary, compressed_rounds)；
        无缓存/已过期/异常时返回 ("", 0)，调用方按“从未压缩”处理。
        """
        key = _key(user_id, session_id)
        client = self.client
        try:
            if client is not None:
                data = client.hgetall(key)
                summary = (data or {}).get("summary", "")
                try:
                    watermark = int((data or {}).get("compressed_rounds", 0) or 0)
                except (TypeError, ValueError):
                    # 水位脏数据兜底：按 0 处理（最多多压一次，不影响正确性）
                    watermark = 0
                return summary, max(0, watermark)
            with self._mem_lock:
                # 降级模式：惰性删除已过期槽位
                slot = self._mem.get(key)
                if not slot or slot["expires_at"] <= time.time():
                    self._mem.pop(key, None)
                    return "", 0
                return slot.get("summary", ""), int(slot.get("watermark", 0))
        except Exception as e:
            # Redis 异常按无缓存处理：本轮只用原文，不阻断对话
            logger.error("context state load failed: %s", e)
            return "", 0

    def _save_state(self, user_id: int, session_id: int, summary: str, watermark: int) -> None:
        """写回摘要与水位并滑动续期（Redis pipeline 原子；不可用时写进程内 dict）。

        被谁调用：_maybe_compress 压缩成功后。
        参数：user_id/session_id 同上；summary (str)——新摘要；
        watermark (int)——新已压缩轮数（= 压缩边界 boundary）。
        返回：None。异常仅记 error 日志（下次构建会重新尝试压缩，可自愈）。
        """
        key = _key(user_id, session_id)
        client = self.client
        payload = {"summary": summary, "compressed_rounds": str(watermark)}
        try:
            if client is not None:
                # pipeline 原子提交 HSET + EXPIRE，避免“有数据无 TTL”的半写状态
                pipe = client.pipeline()
                pipe.hset(key, mapping=payload)
                pipe.expire(key, self._ttl)
                pipe.execute()
                return
            with self._mem_lock:
                # 降级模式：连带刷新 expires_at，语义与滑动 TTL 一致
                self._mem[key] = {
                    "summary": summary,
                    "watermark": watermark,
                    "expires_at": time.time() + self._ttl,
                }
        except Exception as e:
            logger.error("context state save failed: %s", e)


# 模块级全局单例：进程内唯一 ContextMemoryService 实例；
# 首次 import 后并不立即构造，首次调用 get_context_memory_service() 时
# （ChatService.__init__）惰性初始化；_service_lock 保护双重检查锁。
_service: Optional[ContextMemoryService] = None
_service_lock = threading.Lock()


def get_context_memory_service() -> ContextMemoryService:
    """应用级单例工厂（双重检查锁，非每请求新建）。

    被谁调用：app/application/chat/chat_service.py 的 ChatService.__init__、
    app/api/v1/history.py 删除会话联动清理。
    返回：ContextMemoryService——共享单例（内部惰性取全局 Redis 连接）。
    """
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = ContextMemoryService()
    return _service


def reset_context_memory_service_for_test() -> None:
    """测试辅助：重置单例（配合 fake Redis / 假 compressor 重新注入构造）。"""
    global _service
    with _service_lock:
        _service = None
