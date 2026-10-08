"""
模块名：app.domain.agents.rag_agent

作用：
    流水线阶段 3 的知识库检索 Agent（RAG 路线）。消费 AnalysisAgent
    经 MessageBus 发布的检索任务（query），在应用级共享 Chroma 持久
    向量库（公共 + 当前用户私有）及当前会话临时库上做混合检索
    （retrieve_scoped：向量 + 关键词 RRF 融合 + rerank），把命中文档
    构造成 results 经总线回传给 SummaryAgent；并内置 P1 影子模式，
    在开关打开时旁路执行 function calling 新工具链做结果对比。

主要成员：
    - load_or_build_db：模块级函数，加载持久向量库，缺失时从内置 JSON
      知识库（data/LearnPlan_Dialogue_Collection）构建；
    - RAGAgent：独立实现（不继承 BaseAgent），入口方法 handle()；
      类方法 build_shared_db() 提供应用级共享向量库单例；
    - _DEFAULT_JSON_PATH / _DEFAULT_PERSIST_PATH：内置知识库种子 JSON
      与 Chroma 持久化目录的默认路径（模块级常量，基于项目根目录拼接）。

被谁使用（Grep 模块名结果）：
    - app/application/chat/agent_service.py：AgentService._get_rag_db() 经
      RAGAgent.build_shared_db() 懒加载共享库；_create_agents() 中
      RAGAgent(message_bus=..., db=shared_db, user_id=..., session_id=...)
      每请求实例化；_run_retrieval() 按 analysis_result["need_RAGAgent"]
      经非关键包装器调用 agents["rag"].handle；
    - app/infrastructure/persistence/repositories/knowledge.py：知识库管理/删除
      经 app.infrastructure.vector_store.persistent.get_persistent_db() 取得同一 Chroma collection 单例，
      避免写竞争。

Agent 间数据流：
    - 输入（消费方）：bus.subscribe("RAGAgent")，生产者为
      AnalysisAgent.create_agent（payload 含 query/top_k）；
    - 输出（生产者）：bus.publish("RAGAgent", "SummaryAgent", result_msg)，
      payload 为 {"query", "top_k", "results":[{content, metadata}]}，
      消费者 SummaryAgent.handle。
向量库来源：app/infrastructure/vector_store/persistent.get_persistent_db 持有的应用级共享
    Chroma（含进程写锁 persistent_lock）；会话临时库由
    app/infrastructure/vector_store/temp_store 提供，检索细节见 app/domain/agents/retrieval.py。
"""
import os
from functools import lru_cache
from .message_bus import MessageBus
from app.infrastructure.embeddings.text_embedding import (
    build_chromadb,
    split_documents,
    get_embedding,
    load_json_data,
    json_to_documents,
)
from app.application.ports.llm import build_chat_model
from app.domain.agents.retrieval import retrieve_scoped
from typing import Optional
from langchain_chroma import Chroma
import logging

from core.config import settings
from app.domain.tools.protocol import ToolContext

logger = logging.getLogger(__name__)

# 项目根目录（multi_agent 的上一级）：用于把默认资源路径锚定到项目而非 CWD
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 内置公共知识库种子 JSON：首次启动且持久库为空时据此播种课程咨询对话语料
_DEFAULT_JSON_PATH = os.path.join(
    _BASE_DIR, "data", "LearnPlan_Dialogue_Collection", "LearnPlan_Dialogue_Collection.json"
)
# Chroma 持久化目录默认值（向量库落盘位置；主流程实际由 app.infrastructure.vector_store.persistent 统一管理）
_DEFAULT_PERSIST_PATH = os.path.join(_BASE_DIR, "chromadb_data")


def load_or_build_db(json_path: str, persist_path: str) -> Chroma:
    """加载已有向量库；不存在则从 JSON 知识库构建。

    被谁调用：RAGAgent.__init__ 在未注入共享 db（db=None）时的回退构造
              路径（主流程经 build_shared_db 注入应用级单例，通常不走此函数）。
    参数：
    - json_path：内置知识库 JSON 路径（来源：_DEFAULT_JSON_PATH 或显式传入）；
    - persist_path：Chroma 持久化目录（来源：_DEFAULT_PERSIST_PATH 或显式传入）。
    返回：Chroma 实例——持久目录已存在且非空则直接打开，否则执行
          读 JSON → 转 Document → 切块 → build_chromadb 构建并持久化；
          去向：作为 RAGAgent.db 供 retrieve_scoped 检索。
    """
    embeddings = get_embedding()
    if os.path.exists(persist_path) and os.path.isdir(persist_path) and os.listdir(persist_path):
        return Chroma(persist_directory=persist_path, embedding_function=embeddings)
    json_data = load_json_data(json_path)
    docs = json_to_documents(json_data)
    splitted_docs = split_documents(docs)
    return build_chromadb(splitted_docs, embeddings, persist_path=persist_path)


class RAGAgent:
    """持久知识库检索 Agent（流水线阶段 3 的 RAG 分支；独立实现，不继承 BaseAgent）。

    类作用：
        订阅 AnalysisAgent 发布的检索任务，在公共/私有持久向量库与当前
        会话临时库上执行 retrieve_scoped 混合检索，把命中片段回传给
        SummaryAgent；开关 settings.TOOL_SHADOW_MODE 打开时旁路运行
        function calling 新工具链（app.domain.tools.shadow）做新旧结果对比。
    继承关系：无基类（不实现 BaseAgent 抽象接口 create_agent），
        编排入口为 handle()，由非关键 Agent 包装器调度并重试。
    实例化位置：app/application/chat/agent_service.py 的 AgentService._create_agents()，
        每请求实例化，db 形参由 _get_rag_db() 注入 build_shared_db()
        返回的应用级共享向量库单例。
    关键 self 属性含义与去向：
        - self.db：持久 Chroma 实例（来源 app.infrastructure.vector_store.persistent 共享单例），
          作为 retrieve_scoped 的 db 形参（公共 + user_id 私有范围）；
        - self.user_id/self.session_id：来源编排层（JWT/会话上下文），
          决定检索 scope 过滤与是否合并会话临时库；
        - self.top_k：默认每路检索返回条数，可被消息中的 top_k 覆盖；
        - self._shadow_llm：影子模式专用 LLM，惰性创建并绑定 user_id。
    """

    def __init__(
        self,
        message_bus: MessageBus,
        json_path: str = None,
        persist_path: str = None,
        top_k: int = 3,
        db: Optional[Chroma] = None,
        user_id: int = None,
        session_id: int = None,
    ):
        """初始化 RAG 检索 Agent。

        被谁调用：AgentService._create_agents()（app/application/chat/agent_service.py，
                  每请求一次）。
        参数：
        - message_bus：本次请求专属 MessageBus（编排层注入），用于订阅
          检索任务、回传检索结果；
        - json_path：内置知识库 JSON 路径（默认 _DEFAULT_JSON_PATH，
          仅未注入 db 自行构建库时使用）；
        - persist_path：Chroma 持久化目录（默认 _DEFAULT_PERSIST_PATH）；
        - top_k：默认检索返回条数（消息未带 top_k 时使用）；
        - db：应用级共享持久向量库（来源：RAGAgent.build_shared_db()，
          经 app/application/chat/agent_service.py 的 _get_rag_db() 注入）；None 时
          回退 load_or_build_db 自建；
        - user_id：JWT 当前用户 ID（来源请求上下文），做私有库 scope 过滤；
        - session_id：会话号（来源请求上下文），非空时合并检索会话临时库。
        """
        self.bus = message_bus
        self.json_path = json_path or _DEFAULT_JSON_PATH
        self.persist_path = persist_path or _DEFAULT_PERSIST_PATH
        self.top_k = top_k
        self.user_id = user_id
        self.session_id = session_id
        # 影子模式决策 LLM：惰性创建（默认关闭时不增加每请求对象开销）
        self._shadow_llm = None
        # 支持注入应用级共享向量库（单例），避免每个请求重复加载
        self.db: Optional[Chroma] = db
        if self.db is None:
            self.db = load_or_build_db(self.json_path, self.persist_path)
        logger.info("RAGAgent initialized successfully")

    def _get_shadow_llm(self):
        """惰性获取影子模式专用 LLM（仅 TOOL_SHADOW_MODE 开启且命中影子
        条件时才创建，避免每请求平白增加对象开销）。

        被谁调用：_run_shadow()。
        返回：聊天模型实例；若模型支持 set_user_id 则绑定 self.user_id
              （用于影子链路的用量计量/画像透传），绑定失败静默忽略。
        """
        if self._shadow_llm is None:
            self._shadow_llm = build_chat_model()
            if hasattr(self._shadow_llm, "set_user_id"):
                try:
                    self._shadow_llm.set_user_id(self.user_id)
                except Exception:
                    pass
        return self._shadow_llm

    @classmethod
    @lru_cache(maxsize=1)
    def build_shared_db(cls) -> Chroma:
        """构建/加载应用级共享向量库（单例：AgentService / 知识库管理 / 上传写入共用同一实例，
        避免同 persist 目录的多个 Chroma 客户端产生写竞争）。

        实例由 app.infrastructure.vector_store.persistent 统一持有（含进程写锁）；首次使用且库为空时，
        在此播种内置 JSON 公共知识库（保持历史启动行为）。
        """
        from app.infrastructure.vector_store.persistent import get_persistent_db, persistent_lock

        db = get_persistent_db()
        if db._collection.count() == 0:
            json_data = load_json_data(_DEFAULT_JSON_PATH)
            docs = json_to_documents(json_data)
            splitted_docs = split_documents(docs)
            with persistent_lock():
                db.add_documents(splitted_docs)
        return db

    def handle(self):
        """执行 RAG 检索并把结果回传 SummaryAgent（流水线阶段 3 的 RAG 分支入口）。

        被谁调用：app/application/chat/agent_service.py 的 _run_retrieval()，经非关键
                  包装器 _run_non_critical_agent(sm, "retrieval_rag",
                  agents["rag"].handle, fallback_value=None) 调用；异常按
                  retrieval 重试上限重试，耗尽后编排层以空结果继续。
        参数：无（任务在方法内经 bus.subscribe("RAGAgent") 获取，
              生产者为 AnalysisAgent.create_agent，payload 含
              query/top_k/need_* 等决策字段）。
        返回：None（结果通过总线传递，编排层只取"是否调用"标记）。
        消息输出：bus.publish("RAGAgent", "SummaryAgent", result_msg)，
              payload = {"query": str, "top_k": int, "results":
              [{"content": 文本, "metadata": 元数据}]}；content 取值规则：
              内置 JSON 公共库命中间 output 字段，用户上传父子结构取父块正文。
        空 query / 无消息：result_msg 保持空 results 发布，SummaryAgent
              侧据此走"未检索到"降级。
        """
        messages = self.bus.subscribe("RAGAgent")
        # 初始化为空结果，修复旧实现无消息时 result_msg 未定义（UnboundLocalError）
        result_msg = {"query": "", "top_k": self.top_k, "results": []}
        for msg in messages:
            data = msg.get("message", {})
            query = data.get("query", "")
            top_k = data.get("top_k", self.top_k)
            if not query:
                logger.warning("query is empty")
                continue
            results = retrieve_scoped(
                self.db, query, top_k, user_id=self.user_id, session_id=self.session_id
            )
            result_msg = {
                "query": query,
                "top_k": top_k,
                "results": [
                    {
                        # 内置 JSON 公共库取 output 字段；用户文件父子结构取父块正文
                        "content": result.metadata.get("output") or result.page_content,
                        "metadata": result.metadata,
                    }
                    for result in results
                ],
            }
        self.bus.publish("RAGAgent", "SummaryAgent", result_msg)
        logger.info("RAGAgent handled successfully")

        # P1 影子模式：旁路执行 function calling 工具链并与旧结果对比，不影响主流程
        if settings.TOOL_SHADOW_MODE and messages:
            self._run_shadow(data, result_msg)

    def _run_shadow(self, payload: dict, legacy_result: dict) -> None:
        """P1 影子模式：旁路执行 function calling 新工具链并与旧链路结果对比。

        被谁调用：handle() 末尾——settings.TOOL_SHADOW_MODE 开启且本 Agent
                  实际收到消息时（RAGAgent 无额外文件条件，FileAgent 还
                  要求 has_uploaded_files）。
        参数：
        - payload：上游 AnalysisAgent 的消息 dict（影子新链路的输入）；
        - legacy_result：本 Agent 旧链路刚产出的 result_msg（对比基准）。
        返回：None。影子结果只用于观测对比（app.domain.tools.shadow.run_shadow 内部
              记录/落库），任何异常都被吞掉记 debug 日志，绝不影响主链路。
        """
        try:
            from app.domain.tools.shadow import run_shadow

            ctx = ToolContext(
                user_id=self.user_id,
                session_id=self.session_id,
                role="user",
                task_id=getattr(self.bus, "task_id", ""),
            )
            run_shadow("RAGAgent", self._get_shadow_llm(), payload, legacy_result, ctx)
        except Exception as e:  # noqa: BLE001 影子链路任何异常都不得影响主链路
            logger.debug("RAGAgent shadow run failed: %s", e, exc_info=True)
