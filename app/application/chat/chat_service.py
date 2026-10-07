"""
模块名：app.application.chat.chat_service
作用：对话编排服务（应用级单例），是 app/api/v1/chat.py 与
      AgentService 之间的业务门面。负责会话创建与归属校验、上下文记忆/
      画像/关键词注入、上下文改写（LLM+embedding 相似度判定）、标题并行
      预取、调用 AgentService 执行非流式/流式流水线、对话落库（Redis
      短期记忆 + 标题即时持久化 + 画像异步提取）、长会话自动滚换与
      网络中断恢复。

架构修复：
1. 单例化：Embedding/LLM 客户端等重对象只构建一次，通过 get_chat_service() 复用；
2. 请求隔离：所有对话状态（final_result）改为 handle() 内局部变量，
   修复旧实现 self.final_result 实例属性在单例下跨请求串数据的问题；
3. 身份来源：user_id 由控制层从 JWT 注入，不信任请求体；
4. 移除未使用的自建 MessageBus/RAGAgent（统一由 AgentService 管理）。

主要成员：
- ChatService：对话编排服务类（单例）。
- ChatService.handle()：非流式对话入口；handle_stream()：流式对话生成器入口。
- ChatService.recover()：网络中断恢复（仅读 Redis 短期记忆，不重复调 LLM）。
- 其余 _ 开头方法：上下文拼接/改写、记忆构建、画像/关键词注入、标题预取、
  落库、会话滚换等内部步骤。
- get_chat_service()：lru_cache 单例工厂。

被谁使用：
- app/api/v1/chat.py：/chat/send 调 handle()、/chat/stream 调
  handle_stream()（SSE）、/chat/recover 调 recover()；user_id 从 JWT
  current_user 注入，session_id 来自请求体。
"""
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Dict, Optional

import numpy as np

from app.application.chat.agent_service import get_agent_service
from app.infrastructure.embeddings.embedding_model import get_embedding
from config.setting import LLMConfig, agent
from core.config import settings
from core.usage import reset_current_user_id, set_current_user_id
from app.domain.memory.context_memory import get_context_memory_service
from app.domain.memory.profile_service import get_profile_service, render_profile_prefix
from app.domain.memory.session_keyword_service import get_session_keyword_service
from app.domain.memory.session_rollover import maybe_rollover
from app.domain.memory.short_term import get_short_term_store
from app.infrastructure.llm.gateway import LLMUnavailableError
from core.degradation_alert import alert_degradation
from core.content_filter import filter_text
from app.application.chat.context import ContextService
from app.application.chat.title import Title
from app.util.result import handle_result
from app.infrastructure.persistence.repositories.session import SessionDAO
from app.infrastructure.persistence.repositories.history import Information_history

# 模块级日志器：对话链路的阶段日志与各类降级/失败告警统一走该 logger
logger = logging.getLogger(__name__)


class ChatService:
    """对话编排服务：处理用户输入，调用 AgentService 执行自动化流程并落库结果。

    类作用：对外提供非流式 handle()、流式 handle_stream() 与中断 recover()
            三个入口；对内持有 Agent 编排服务、embedding 模型、上下文改写/
            标题工具、会话/历史 DAO 与四类记忆服务，串起"会话校验 → 上下文
            构建/改写 → Agent 流水线 → 内容过滤 → 落库 → 会话滚换"全链路。

    实例化位置：不在 control 层直接 new；唯一创建处为本模块末尾的
            get_chat_service()（@lru_cache 应用级单例），由
            app/api/v1/chat.py 的 send/stream/recover 三个接口调用。

    关键 self 属性（均为进程级共享重对象，请求间无状态）：
    - agent_service：AgentService 单例（run_agent/run_agent_stream 去向）。
    - embedding_model：embedding 客户端（context 相似度判定，来源
      embedding/embedding_model.get_embedding）。
    - context_service：util/context.ContextService，上下文强相关判定与 LLM 改写。
    - title_service：util/title.Title，会话标题生成（LLM）。
    - session_dao：dao/session.SessionDAO，会话创建与归属校验（写/查 MySQL）。
    - history_dao：dao/history.Information_history，标题即时 upsert 等持久化。
    - short_term_store：Redis 短期记忆（滑动 TTL），由 _save_information/recover/
      _maybe_rollover 读写，后台任务批量转存 MySQL。
    - context_memory：上下文记忆服务（3/5 轮原文 + 早期摘要压缩）。
    - profile_service：用户画像服务（长期画像读取与异步提取）。
    - keyword_service：会话关键词服务（累积/渲染关键词前缀）。
    - _title_pool：标题预取线程池（4 worker，与主链路并发）。
    """

    def __init__(self):
        # __init__ 无形参：所有协作者均经各自单例工厂获取，保证单例下只构建一次
        self.agent_service = get_agent_service()
        self.embedding_model = get_embedding()
        self.context_service = ContextService()
        self.title_service = Title()
        self.session_dao = SessionDAO()
        self.history_dao = Information_history()
        # 记忆模块：短期记忆（Redis 滑动 TTL）/ 上下文记忆（3/5 轮 + 摘要压缩）/ 用户画像
        self.short_term_store = get_short_term_store()
        self.context_memory = get_context_memory_service()
        self.profile_service = get_profile_service()
        self.keyword_service = get_session_keyword_service()
        # 标题预取线程池：title 只依赖 user_input+上下文（不依赖回答），
        # 与改写/意图/检索/汇总/流式输出整条链路并行，完成后在落库前取结果。
        self._title_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="title")
        logger.info("ChatService initialized successfully")

    # 类级常量：由登录态/会话管理注入的内部键，不参与用户上下文的拼接与 embedding
    # （_context 拼接时剔除，避免 user_id 等内部字段污染改写与相似度计算）
    _INTERNAL_CONTEXT_KEYS = {"user_id", "session_id", "user_name", "history"}

    def _context(self, context: Optional[Dict[str, Any]] = None) -> str:
        """处理上下文信息，拼接成一段文字（仅用户提供的上下文字段）。

        功能：把 context 字典中除内部键（user_id/session_id/user_name/
              history）外的用户字段用 "key:value" 分号连接，作为后续 LLM
              改写与 embedding 相似度判定的上下文文本。
        被谁调用：_context_query()。
        参数：context (dict|None)——请求上下文，来源：app/api/v1/chat.py
              组装（JWT 的 user_id + 请求体 session_id + 内部追加的 history）。
        返回：str——拼接后的用户上下文文本；无用户字段时为空串。
        """
        context = context or {}
        return ";".join(
            f"{key}:{value}"
            for key, value in context.items()
            if key not in self._INTERNAL_CONTEXT_KEYS
        )

    def _get_recent_history(self, user_id: int, session_id: int) -> str:
        """
        上下文记忆：构建【早期对话摘要 + 最近 N 轮原文】注入文本。

        功能：委托 app/domain/memory/context_app.domain.memory.build_context 组装多轮上下文；
        - 短期记忆（Redis）未命中时自动回源 MySQL 并回填；
        - 超过 3/5 轮或上下文窗口达上限时压缩更早信息，已压缩轮次走缓存水位，
          网络中断恢复后不重复调用 LLM。
        被谁调用：_handle_impl() / _handle_stream_impl()，每轮对话开始时构建。
        参数：
        - user_id (int)：JWT 注入的用户 ID。
        - session_id (int)：当前会话 ID（per-user 序列）。
        返回：str——注入文本（数据来源：Redis 短期记忆，未命中回源 MySQL
              历史表，摘要由 app/domain/memory/context_memory 调 LLM 压缩）；空入参或
              异常时返回空串（降级为无历史，不阻断主链路）。去向：作为
              history 传给 AgentService，并用于提取早期摘要供意图判定。
        """
        if not user_id or not session_id:
            return ""
        try:
            return self.context_app.domain.memory.build_context(user_id, session_id)
        except Exception as e:
            logger.error("Error building context memory: %s", e)
            return ""

    @staticmethod
    def _extract_early_summary(context_text: str) -> str:
        """从 build_context 注入文本中提取【早期对话摘要】段，供意图判定消解
        多轮指代（如"那这个呢"）。无摘要（新会话/未达压缩水位）时返回空串。

        被谁调用：_get_intent_summary()。
        参数：context_text (str)——_get_recent_history 返回的注入文本
              （数据来源：app/domain/memory/context_memory）。
        返回：str——摘要段正文（截止到【最近N轮对话】标记之前）；无摘要
              标记时返回空串。去向：作为 history_summary 传给 AgentService
              的 VagueAgent/AnalysisAgent。
        """
        if not context_text:
            return ""
        marker = "【早期对话摘要】"
        if marker not in context_text:
            return ""
        rest = context_text.split(marker, 1)[1]
        # 摘要段截止到【最近N轮对话】之前
        return rest.split("【最近", 1)[0].strip()

    def _get_intent_summary(self, recent_history: str) -> str:
        """按开关决定是否向意图模型提供早期对话摘要。

        功能：配置 agent.INTENT_WITH_HISTORY 关闭时直接返回空串（省一次
              文本处理并避免误导），开启时从上下文注入文本提取早期摘要。
        被谁调用：_handle_impl() / _handle_stream_impl()。
        参数：recent_history (str)——_get_recent_history 的返回文本。
        返回：str——早期对话摘要或空串；去向：AgentService 的 history_summary。
        """
        if not agent.INTENT_WITH_HISTORY:
            return ""
        return self._extract_early_summary(recent_history)

    def _get_profile_prefix(self, user_id: int) -> str:
        """用户画像注入文本（长期画像，随每轮提问一起交给模型）。

        功能：读取用户画像并渲染为注入前缀，供 ChatAgent 个性化回答。
        被谁调用：_handle_impl() / _handle_stream_impl()。
        参数：user_id (int)——JWT 注入的用户 ID。
        返回：str——画像前缀文本；数据来源：app/domain/memory/profile_service
              （Redis 暂存 + MySQL 长期画像）；user_id 为空或异常时返回空串，
              画像加载失败不阻断对话。
        """
        if not user_id:
            return ""
        try:
            return render_profile_prefix(self.profile_service.get_profile(user_id))
        except Exception as e:
            logger.error("Error loading user profile: %s", e)
            return ""

    def _context_query(
        self, context: Optional[Dict[str, Any]] = None, query: str = ""
    ) -> str:
        """结合上下文信息和用户输入，生成新的查询。

        功能：无用户上下文字段时原样返回 query；ContextService 判定为
              "force"（强相关）时直接走 LLM 改写；否则分别 embed 用户问题
              与上下文文本做 numpy 余弦相似度比较，达到
              LLMConfig.SIMILARITY_THRESHOLD 才拼接上下文，避免无关上下文
              干扰检索与意图。
        被谁调用：_handle_impl() / _handle_stream_impl()。
        参数：
        - context (dict|None)：请求上下文（含内部键与用户字段，来源：
          control 层 JWT + 请求体，并追加 history/session_id）。
        - query (str)：用户当轮原始输入（app/api/v1/chat.py 请求体
          Chat.user_input）。
        返回：str——改写/拼接后的最终查询，去向：AgentService.run_agent(_stream)
              的 query 参数；任何异常都降级返回原始 query，保证主链路不中断。
        """
        context = context or {}
        try:
            context_text = self._context(context)
            if not context_text.strip():
                return query
            if self.context_service.context_model(context) == "force":
                rewritten = self.context_service.context_query(context, query)
                return rewritten or query
            query_embedding = self.embedding_model.embed_query(query)
            context_embedding = self.embedding_model.embed_query(context_text)
            # numpy 余弦相似度（替代 sklearn：sklearn 的 OpenBLAS 与 chromadb native
            # 扩展在 Windows 下存在 0xC0000005 冲突，见 2026-09 排查记录）
            q = np.asarray(query_embedding, dtype=np.float32)
            c = np.asarray(context_embedding, dtype=np.float32)
            denom = float(np.linalg.norm(q) * np.linalg.norm(c))
            score = float(np.dot(q, c) / denom) if denom else 0.0
            if score >= LLMConfig.SIMILARITY_THRESHOLD:
                return f"上下文信息：{context_text}\n当前用户问题：{query}"
            return query
        except Exception as e:
            logger.warning("上下文改写失败，降级为原始 query: %s", e)
            return query

    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """非流式对话入口：处理用户输入并返回最终结果。

        功能：从 context 取出 JWT 注入的 user_id/session_id，把 user_id
              写入线程局部变量（供 LLM 网关按用户计量 token），随后委托
              _handle_impl 执行完整链路，结束后复位线程局部。
        被谁调用：app/api/v1/chat.py 的 /chat/send 接口（send）。
        参数：
        - user_input (str)：用户本轮提问，来源：请求体 Chat.user_input。
        - context (dict|None)：{"user_id": JWT 解析, "session_id": 请求体}，
          来源：app/api/v1/chat.py 组装。
        返回：Dict[str, Any]——成功时含 status/title/user_input/user_id/
              session_id/ai_output 等，去向：/chat/send JSON 响应返回前端；
              失败时 {"status": "fail", "message": 友好错误文案}。
        """
        context = context or {}
        user_id = context.get("user_id")
        session_id = context.get("session_id") or 0

        # 将 user_id 注入线程局部，供 LLM 网关计量用户维度 token 用量
        set_current_user_id(user_id)
        try:
            return self._handle_impl(user_input, context, user_id, session_id)
        finally:
            reset_current_user_id()

    def _handle_impl(
        self, user_input: str, context: Dict[str, Any], user_id, session_id
    ) -> Dict[str, Any]:
        """handle() 的实际实现：非流式对话完整业务链路。

        功能：①无 session_id 时经 SessionDAO 创建会话，否则校验会话归属；
              ②构建上下文记忆/早期摘要，并行预取标题；③上下文改写并独立
              携带画像/关键词调用 AgentService.run_agent；④handle_result
              归一化 + filter_text 内容安全过滤；⑤_save_information 写短期
              记忆/标题/关键词/画像；⑥判定长会话滚换。LLM 整体不可用与
              其他异常分别降级为友好 fail 响应。
        被谁调用：handle()（线程局部 user_id 已设置的上下文内）。
        参数：
        - user_input (str)：用户本轮提问（请求体用户输入）。
        - context (dict)：请求上下文（JWT user_id + session_id，内部追加 history）。
        - user_id：当前用户 ID（来源：JWT，会话隔离与计量用）。
        - session_id：当前会话 ID（无则为 0，内部新建；per-user 序列）。
        返回：Dict[str, Any]——成功/失败结果 dict，去向：handle() 透传给
              app/api/v1/chat.py 的 /chat/send 接口 → 前端。
        异常：LLMUnavailableError 与 Exception 在本方法内捕获并转为 fail
              结果（不向 control 层抛出）。
        """
        # 请求内局部状态，保证单例下请求间隔离
        final_result: Dict[str, Any] = {}
        try:
            # 未指定会话则自动创建（并发安全由 SessionDAO 保证）
            if not session_id:
                session_id = self.session_dao.create_session(user_id, "新会话")
                if not session_id:
                    return {"status": "fail", "message": "会话创建失败"}
            elif not self.session_dao.is_session_owner(user_id, session_id):
                # 会话归属校验：会话号为 per-user 序列，传入他人/不存在的会话号直接拒绝，
                # 保证会话内容、上下文与临时知识库绝不跨用户/跨会话交错
                return {"status": "fail", "message": "会话不存在或无权限操作"}

            recent_history = self._get_recent_history(user_id, session_id)
            history_summary = self._get_intent_summary(recent_history)
            context_with_history = dict(context)
            context_with_history["session_id"] = session_id
            if recent_history:
                context_with_history["history"] = recent_history

            # 标题与整条前置链路（改写/意图/检索/汇总/回答）并行预取
            title_future = self._prefetch_title(user_input, context_with_history, user_id)

            final_context = self._context_query(context=context_with_history, query=user_input)
            # 画像/关键词不再拼进 query（旧实现污染意图判断与检索 embedding，
            # 导致答非所问），改为独立字段仅传给最终回答的 ChatAgent
            profile_prefix = self._get_profile_prefix(user_id)
            kw_prefix = self.keyword_service.render_prefix(user_id, session_id)
            result = self.agent_service.run_agent(
                query=final_context, user_id=user_id, session_id=session_id,
                history=recent_history, session_keywords=kw_prefix,
                user_profile=profile_prefix, history_summary=history_summary,
            )
            logger.info("Agent pipeline finished, success=%s", result.get("success"))

            final_result = handle_result(result)
            final_result["title"] = title_future.result()
            final_result["user_input"] = user_input
            final_result["user_id"] = user_id
            final_result["session_id"] = session_id

            if "chat_result" in result and isinstance(result["chat_result"], dict):
                final_result["ai_output"] = filter_text(
                    result["chat_result"].get("answer", "")
                )

            self._save_information(final_result)
            # 长对话漂移治理：达到轮数阈值后自动滚换到新会话（上下文整体迁移）。
            # 失败返回 None，本轮结果仍按旧会话正常返回。
            new_sid = self._maybe_rollover(user_id, session_id, final_result.get("title"))
            if new_sid:
                final_result["rolled_over"] = True
                final_result["previous_session_id"] = session_id
                final_result["session_id"] = new_sid
            final_result["status"] = "success"
            return final_result
        except LLMUnavailableError:
            # 主模型与全部降级模型均不可用：统一友好降级，不泄露内部细节
            logger.error("LLM 服务整体不可用（重试与降级均失败），本次对话降级返回")
            alert_degradation(
                stage="llm_unavailable", severity="critical",
                query=user_input, user_id=user_id, session_id=session_id,
                error="all LLM models exhausted after retries",
            )
            return {"status": "fail", "message": "模型服务暂时不可用，请稍后重试"}
        except Exception as e:
            logger.exception("ChatService handle failed: %s", e)
            return {"status": "fail", "message": "服务处理失败，请稍后重试"}

    def handle_stream(
        self, user_input: str, context: Optional[Dict[str, Any]] = None
    ):
        """
        流式对话处理：generator 逐帧 yield 字典（控制层负责序列化为 SSE）。

        功能：handle() 的流式版本，负责设置/复位线程局部 user_id，实际
              帧生成在 _handle_stream_impl；二者前置逻辑与持久化逻辑一致。
        被谁调用：app/api/v1/chat.py 的 /chat/stream 接口（stream 的
                  event_gen 逐帧 json.dumps 为 SSE）。
        参数：
        - user_input (str)：用户本轮提问，来源：请求体 Chat.user_input。
        - context (dict|None)：{"user_id": JWT 解析, "session_id": 请求体}。
        返回：generator——帧类型：
        - status: {"type": "status", "stage", "message"}（前置阶段提示，前端渲染后可覆盖）
        - delta:  {"type": "delta", "content"}（文本增量，已经 filter_text 过滤）
        - done:   {"type": "done", "session_id", "title", "ai_output"}
        - error:  {"type": "error", "message"}
        去向：经 app/api/v1/chat.py 的 SSE 流式返回前端。
        """
        context = context or {}
        user_id = context.get("user_id")
        session_id = context.get("session_id") or 0

        # 将 user_id 注入线程局部，供 LLM 网关计量用户维度 token 用量
        set_current_user_id(user_id)
        try:
            yield from self._handle_stream_impl(user_input, context, user_id, session_id)
        finally:
            reset_current_user_id()

    def _handle_stream_impl(
        self, user_input: str, context: Dict[str, Any], user_id, session_id
    ):
        """handle_stream() 的实际实现：流式对话帧生成器。

        功能：与 _handle_impl 同构的流式版本——会话创建/归属校验、记忆构建、
              标题并行预取、先发 rewrite 状态帧保证首帧即时反馈、上下文改写
              （跨 next() 切线程前重设线程局部 user_id）、消费
              AgentService.run_agent_stream 的事件帧（delta 逐块过滤后
              转发、status 透传）、拼接完整回答后落库并判定会话滚换，
              最后 yield done 帧；空回答/异常分别 yield 兜底文案或 error 帧。
        被谁调用：handle_stream()。
        参数：同 _handle_impl（user_input/context/user_id/session_id）。
        返回：generator——status/delta/done/error 四类字典帧，去向：
              app/api/v1/chat.py 序列化为 SSE 返回前端。
        异常：LLMUnavailableError/Exception 在生成器内捕获并 yield error 帧。
        """
        try:
            # 未指定会话则自动创建（并发安全由 SessionDAO 保证）
            if not session_id:
                session_id = self.session_dao.create_session(user_id, "新会话")
                if not session_id:
                    yield {"type": "error", "message": "会话创建失败"}
                    return
            elif not self.session_dao.is_session_owner(user_id, session_id):
                # 会话归属校验（同 _handle_impl）：杜绝跨用户/跨会话交错
                yield {"type": "error", "message": "会话不存在或无权限操作"}
                return

            recent_history = self._get_recent_history(user_id, session_id)
            history_summary = self._get_intent_summary(recent_history)
            context_with_history = dict(context)
            context_with_history["session_id"] = session_id
            if recent_history:
                context_with_history["history"] = recent_history

            # 标题与整条前置链路（改写/意图/检索/汇总/流式输出）并行预取
            title_future = self._prefetch_title(user_input, context_with_history, user_id)

            # 改写是 LLM 调用（~数秒）：先产出首帧状态提示，
            # 让用户在改写/意图/检索全链路期间都有即时反馈
            yield {"type": "status", "stage": "rewrite", "message": "正在理解您的问题…"}

            # context 改写用共享 LLM 实例，依赖线程局部携带 user_id；
            # 流式生成器可能跨 next() 切线程，故在此处重新设置。
            set_current_user_id(user_id)
            final_context = self._context_query(context=context_with_history, query=user_input)
            # 画像/关键词不再拼进 query，改为独立字段传给 ChatAgent（与 handle 一致）
            profile_prefix = self._get_profile_prefix(user_id)

            parts = []
            kw_prefix = self.keyword_service.render_prefix(user_id, session_id)
            for event in self.agent_service.run_agent_stream(
                query=final_context, user_id=user_id, session_id=session_id,
                history=recent_history, session_keywords=kw_prefix,
                user_profile=profile_prefix, history_summary=history_summary,
            ):
                etype = (event or {}).get("type")
                if etype == "delta":
                    content = event.get("content", "")
                    if content:
                        filtered = filter_text(content)
                        parts.append(filtered)
                        yield {"type": "delta", "content": filtered}
                elif etype == "status":
                    yield {
                        "type": "status",
                        "stage": event.get("stage", ""),
                        "message": event.get("message", ""),
                    }
                # 其他事件类型忽略（向前兼容）

            answer = "".join(parts).strip()
            if not answer:
                answer = "抱歉，我暂时没有理解您的意思，可以再详细说明一下吗？"

            title = title_future.result()
            final_result = {
                "user_input": user_input,
                "user_id": user_id,
                "session_id": session_id,
                "ai_output": answer,
                "title": title,
            }
            self._save_information(final_result)
            # 长对话漂移治理：达到轮数阈值后自动滚换，done 帧把新会话 id 交回前端
            new_sid = self._maybe_rollover(user_id, session_id, title)
            done_frame = {"type": "done", "session_id": new_sid or session_id,
                          "title": title, "ai_output": answer}
            if new_sid:
                done_frame["rolled_over"] = True
                done_frame["previous_session_id"] = session_id
            yield done_frame
        except LLMUnavailableError:
            logger.error("LLM 服务整体不可用（重试与降级均失败），流式对话降级返回")
            alert_degradation(
                stage="llm_unavailable", severity="critical",
                query=user_input, user_id=user_id, session_id=session_id,
                error="all LLM models exhausted after retries",
            )
            yield {"type": "error", "message": "模型服务暂时不可用，请稍后重试"}
        except Exception as e:
            logger.exception("ChatService stream failed: %s", e)
            yield {"type": "error", "message": "服务处理失败，请稍后重试"}

    def _save_information(self, final_result: Dict[str, Any]) -> None:
        """
        记忆写入：长期记忆由短期记忆转变而来——对话期间只写 Redis 短期记忆
        （不写 MySQL，减少数据库请求）；后台落库任务在会话静默临近过期时
        把短期记忆批量转存 MySQL 后删除缓存（app/domain/memory/long_term.py）。
        - 短期记忆：本轮对话 + 会话标题写入 Redis 并滑动续期；
        - 用户画像：异步从本轮对话提取/合并画像信号（内部节流，不阻塞响应）。

        被谁调用：_handle_impl() 与 _handle_stream_impl() 在拿到完整回答后。
        参数：final_result (dict)——本轮结果，读取 user_id/session_id/
              user_input/ai_output/title；来源：AgentService 返回经
              handle_result 归一化 + filter_text 过滤后的结果。
        返回：无。数据去向：①Redis 短期记忆（short_term_store.append_round）；
              ②标题即时 upsert 到 MySQL（dao/history.Information_history，
              幂等且不覆盖自定义标题）；③会话关键词累积（Redis）；
              ④画像提取任务异步投递。各子步骤独立 try，单点失败只记日志，
              不影响已生成的回答返回。
        """
        try:
            if final_result.get("ai_output"):
                user_id = final_result.get("user_id")
                session_id = final_result.get("session_id")
                if user_id and session_id:
                    try:
                        self.short_term_store.append_round(
                            user_id,
                            session_id,
                            final_result.get("user_input") or "",
                            final_result.get("ai_output") or "",
                            title=final_result.get("title"),
                        )
                    except Exception as e:
                        logger.error("Error writing short-term memory: %s", e)
                    # 标题即时持久化：不等静默落库（延迟可达数十分钟，期间刷新
                    # 页面/重新登录从 DB 拉到的一直是"新会话"）。upsert 幂等且
                    # 仅覆盖默认标题，不覆盖用户自定义标题。
                    title_now = final_result.get("title")
                    if title_now and title_now not in ("新会话", "未命名会话"):
                        try:
                            self.history_dao.save_information(
                                {"user_id": user_id, "session_id": session_id,
                                 "title": title_now}
                            )
                        except Exception as e:
                            logger.error("Error persisting session title: %s", e)
                    try:
                        self.keyword_service.extract_and_accumulate(
                            user_id, session_id,
                            final_result.get("user_input") or "",
                            final_result.get("ai_output") or "",
                        )
                    except Exception as e:
                        logger.error("Error extracting keywords: %s", e)
                try:
                    self.profile_service.extract_from_conversation_async(
                        user_id,
                        final_result.get("user_input") or "",
                        final_result.get("ai_output") or "",
                    )
                except Exception as e:
                    logger.error("Error scheduling profile extraction: %s", e)
        except Exception as e:
            logger.error("Error saving information: %s", e)

    def _maybe_rollover(self, user_id: int, session_id: int, title: str):
        """本轮落库后判定是否达到滚换阈值并执行会话滚换。

        功能：轮数直接取短期记忆消息数（零 SQL；阈值 15 轮 < 短期记忆
        20 轮截断上限，计数准确），委托 app/domain/memory/session_rollover.maybe_rollover
        决定并执行上下文整体迁移（含临时知识库复制）。
        被谁调用：_handle_impl() / _handle_stream_impl() 落库之后。
        参数：
        - user_id (int)：JWT 注入的用户 ID。
        - session_id (int)：当前会话 ID。
        - title (str)：本轮生成的会话标题，随上下文迁移到新会话。
        返回：int|None——触发滚换时返回新会话 ID（去向：写入结果/done 帧
              的 session_id，并附带 rolled_over/previous_session_id 交回
              前端）；未触发或异常时返回 None，本轮结果仍按旧会话正常返回。
        """
        try:
            messages = self.short_term_store.load(user_id, session_id) or []
            rounds = len(messages) // 2
            return maybe_rollover(user_id, session_id, rounds, title or "")
        except Exception as e:
            logger.error("session rollover check failed: %s", e)
            return None

    def recover(self, user_id: int, session_id: int) -> Dict[str, Any]:
        """
        网络中断恢复：查看短期记忆（当前会话已生成的对话，含中断前已生成的内容），
        不查长期记忆、不重新生成、不重复调用 LLM。

        被谁调用：app/api/v1/chat.py 的 /chat/recover 接口（recover）；
                  control 层会把返回的 status 字段改名为 recover_status。
        参数：
        - user_id (int)：JWT 注入的用户 ID。
        - session_id (int)：请求体 RecoverRequest.session_id。
        返回：Dict[str, Any]——
        - completed：短期记忆中最后一轮 user/assistant 成对完整，返回
          {"status": "completed", "session_id", "user_content", "ai_output"}，
          由前端比对 user_content 是否就是中断等待的那一轮提问（数据来源：
          Redis 短期记忆）；
        - missing：短期记忆中无完整上一轮（该轮尚未生成完成，或会话记录已
          过期清理），返回 {"status": "missing", "message"}，前端提示重发；
        - 上下文摘要/已加载信息由 context_memory 缓存水位保证幂等，下一轮重建不重复压缩。
        去向：/chat/recover JSON 响应返回前端。
        """
        if not user_id or not session_id:
            return {"status": "missing", "message": "会话信息缺失"}
        try:
            messages = self.short_term_store.load(user_id, session_id) or []
        except Exception as e:
            logger.error("recover load short-term memory failed: %s", e)
            return {"status": "missing", "message": "恢复失败，请重新发送"}
        if (
            len(messages) >= 2
            and messages[-1].get("role") == "assistant"
            and messages[-2].get("role") == "user"
        ):
            return {
                "status": "completed",
                "session_id": session_id,
                "user_content": messages[-2].get("content", ""),
                "ai_output": messages[-1].get("content", ""),
            }
        return {"status": "missing", "message": "上一条回答未完成，请重新发送"}

    def _handle_title(self, user_input: str, context: Dict[str, Any], user_id) -> str:
        """标题预取线程的执行体：生成会话标题。

        功能：在线程池 worker 中设置线程局部 user_id（标题生成的 LLM 调用
              同样计入用户用量），调用 Title.title 生成标题，结束后复位。
        被谁调用：由 _prefetch_title() 提交到 self._title_pool 异步执行。
        参数：
        - user_input (str)：用户本轮提问（标题只依赖输入不依赖回答）。
        - context (dict)：含 session_id/history 的请求上下文。
        - user_id：JWT 注入的用户 ID。
        返回：str——LLM 生成的标题；任何异常都兜底返回 "新会话"，
              保证 future.result() 不抛业务异常。数据来源：model_llm 网关。
        """
        # 线程池 worker 内重新注入 user_id：线程局部变量不随提交线程继承
        set_current_user_id(user_id)
        try:
            return self.title_service.title(context, user_input)
        except Exception as e:
            logger.error("Error handling title: %s", e)
            return "新会话"
        finally:
            reset_current_user_id()

    def _prefetch_title(self, user_input: str, context: Dict[str, Any], user_id) -> "Future":
        """并行预取标题（_handle_title 内部兜底，future 不会抛业务异常）。

        功能：把标题生成提交到 self._title_pool，使其与改写/意图/检索/
              汇总/回答整条链路并发执行，调用方在落库前 future.result() 取结果。
        被谁调用：_handle_impl() / _handle_stream_impl() 链路开始处。
        参数：同 _handle_title（user_input/context/user_id）。
        返回：concurrent.futures.Future——result 为标题字符串（兜底"新会话"）。
        """
        return self._title_pool.submit(self._handle_title, user_input, context, user_id)


@lru_cache(maxsize=1)
def get_chat_service() -> ChatService:
    """ChatService 应用级单例工厂。

    功能：lru_cache 保证全进程仅构建一个 ChatService（embedding/LLM 客户端、
          DAO、记忆服务与线程池等重对象只初始化一次）。
    被谁调用：app/api/v1/chat.py 的 send/stream/recover 接口。
    返回：ChatService 唯一实例。
    """
    return ChatService()
