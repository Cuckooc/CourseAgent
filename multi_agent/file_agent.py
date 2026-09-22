"""
模块名：multi_agent.file_agent

作用：
    流水线阶段 3 的上传文件检索 Agent（File 路线）。消费 AnalysisAgent
    经 MessageBus 发布的检索任务（query），对当前会话上传文件构成的
    向量库（会话临时库 + 可选的本地 PDF 演示库）执行 retrieve_scoped
    混合检索（向量 + 关键词 RRF 融合 + rerank），把命中片段构造成
    results 经总线回传 SummaryAgent；同样内置 P1 影子模式旁路对比
    function calling 新工具链。

主要成员：
    - FileAgent：独立实现（不继承 BaseAgent），入口方法 handle()；
      __init__rag() 负责从本地 PDF 构建/加载演示向量库（懒加载）；
    - _BASE_DIR / _DEFAULT_PDF_PATH / _DEFAULT_PERSIST_PATH：项目根、
      默认演示 PDF（file_analysis/1.pdf）与其 Chroma 持久化目录
      （模块级常量）。

被谁使用（Grep 模块名结果）：
    - service/agent_service.py：AgentService._create_agents() 中
      FileAgent(message_bus=..., db=None, user_id=..., session_id=...)
      每请求实例化（刻意不注入共享持久库，文件路线只检索会话临时库）；
      _run_retrieval() 按 analysis_result["need_FileAgent"] 经非关键
      包装器调用 agents["file"].handle；
    - tests/phase/test_all_changes.py：测试中构造总线验证 FileAgent 收消息。

Agent 间数据流：
    - 输入（消费方）：bus.subscribe("FileAgent")，生产者为
      AnalysisAgent.create_agent（payload 含 query/top_k/
      has_uploaded_files）；
    - 输出（生产者）：bus.publish("FileAgent", "SummaryAgent", result_msg)，
      payload 为 {"query", "top_k", "results":[{content, metadata}]}，
      消费者 SummaryAgent.handle。
向量库来源：self.db 为本地 PDF 演示库（file_analysis/1.pdf，经
    file_analysis.file 的切块/embedding/Chroma 工具构建）；会话上传
    文件库由 service/temp_knowledge_store 按 user_id+session_id 提供，
    retrieve_scoped 在 self.db 为 None 时仍可独立检索临时库。
"""
import os
from .message_bus import MessageBus
from file_analysis.file import (
    pdf_text,
    split_str,
    get_embedding,
    to_documents,
    build_chromadb,
)
from model_llm.gateway import build_chat_model
from multi_agent.retrieval import retrieve_scoped
from typing import Optional
from langchain_chroma import Chroma
import logging

from core.config import settings
from tools.protocol import ToolContext

logger = logging.getLogger(__name__)

# 项目根目录（multi_agent 的上一级）：把默认资源路径锚定到项目而非 CWD
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 默认演示 PDF：本地文件问答的演示语料（file_analysis/1.pdf）
_DEFAULT_PDF_PATH = os.path.join(_BASE_DIR, "file_analysis", "1.pdf")
# 演示 PDF 向量库的 Chroma 持久化目录
_DEFAULT_PERSIST_PATH = os.path.join(_BASE_DIR, "chromadb_data")


class FileAgent:
    """上传文件检索 Agent（流水线阶段 3 的 File 分支；独立实现，不继承 BaseAgent）。

    类作用：
        订阅 AnalysisAgent 发布的文件检索任务，在当前会话上传文件向量库
        （及可选的本地 PDF 演示库）上执行 retrieve_scoped 混合检索，把
        命中片段经总线回传 SummaryAgent；TOOL_SHADOW_MODE 开启且本会话
        存在上传文件时，旁路运行 function calling 新工具链做对比。
    继承关系：无基类（不实现 BaseAgent 抽象接口 create_agent），
        编排入口为 handle()，由非关键 Agent 包装器调度并重试。
    实例化位置：service/agent_service.py 的 AgentService._create_agents()，
        每请求实例化且 db 固定传 None（不挂载应用级共享持久库，文件路线
        数据域限定为会话临时库 + 本地演示 PDF）。
    关键 self 属性含义与去向：
        - self.db：本地 PDF 演示库 Chroma 实例，可能为 None（PDF 缺失或
          初始化失败时），作为 retrieve_scoped 的 db 形参；None 不影响
          会话临时库检索；
        - self.user_id/self.session_id：来源编排层（JWT/会话上下文），
          决定会话临时库定位（temp/{user_id}_{session_id}）与 scope；
        - self.top_k：默认返回条数，可被消息中的 top_k 覆盖；
        - self.pdf_path/self.persist_path：演示 PDF 与其持久化目录；
        - self._shadow_llm：影子模式专用 LLM，惰性创建并绑定 user_id。
    """

    def __init__(
        self,
        message_bus: MessageBus,
        pdf_path: str = None,
        persist_path: str = None,
        top_k: int = 3,
        db: Optional[Chroma] = None,
        user_id: int = None,
        session_id: int = None,
    ):
        """初始化文件检索 Agent。

        被谁调用：AgentService._create_agents()（service/agent_service.py，
                  每请求一次，固定 db=None）。
        参数：
        - message_bus：本次请求专属 MessageBus（编排层注入），用于订阅
          文件检索任务、回传检索结果；
        - pdf_path：演示 PDF 路径（默认 _DEFAULT_PDF_PATH）；
        - persist_path：演示 PDF 向量库持久化目录（默认 _DEFAULT_PERSIST_PATH）；
        - top_k：默认检索返回条数（消息未带 top_k 时使用）；
        - db：显式注入的 Chroma 实例（主流程传 None；非 None 时直接
          采用并跳过本地 PDF 库初始化）；
        - user_id：JWT 当前用户 ID（来源请求上下文），定位会话临时库；
        - session_id：会话号（来源请求上下文），定位会话临时库。
        初始化行为：PDF 存在时尝试 __init__rag 构建/加载向量库，失败
                  仅告警并置 self.db=None（懒重试，不阻断应用启动与后续
                  会话临时库检索）；PDF 缺失时记录 info 并禁用文件演示库。
        """
        self.bus = message_bus
        # 知识库范围过滤：公共 + user_id 的私有 + session_id 的会话临时库
        self.user_id = user_id
        self.session_id = session_id
        # 影子模式决策 LLM：惰性创建（默认关闭时不增加每请求对象开销）
        self._shadow_llm = None
        # 修复旧实现使用相对路径（../file_analysis/1.pdf）导致按 CWD 解析错误
        self.pdf_path = pdf_path or _DEFAULT_PDF_PATH
        self.persist_path = persist_path or _DEFAULT_PERSIST_PATH
        self.top_k = top_k
        self.db: Optional[Chroma] = db
        if self.db is not None:
            return
        if os.path.exists(self.pdf_path):
            try:
                self.__init__rag()
            except Exception as e:
                logger.warning("FileAgent vector db init failed, will retry lazily: %s", e)
                self.db = None
        else:
            logger.info(
                "FileAgent: pdf not found (%s), file retrieval disabled until a file is uploaded",
                self.pdf_path,
            )
            self.db = None

    def __init__rag(self):
        """初始化本地 PDF 演示向量库（懒加载，文件缺失/失败时不阻断应用启动）。

        被谁调用：__init__ 中检测到 self.pdf_path 存在且未注入 db 时调用一次。
        参数：无（路径来自 self.pdf_path/self.persist_path）。
        返回：None（产出写入 self.db）。
        构建逻辑：pdf_text 抽取 PDF 文本 → split_str 切块 → get_embedding
                  取 embedding 模型；持久目录非空则直接打开已有 Chroma，
                  否则 to_documents 包装文档后 build_chromadb 构建并落盘。
        """
        text = pdf_text(self.pdf_path)
        splitted_docs = split_str(text)
        embeddings = get_embedding()
        if os.path.exists(self.persist_path) and os.path.isdir(self.persist_path) and os.listdir(
            self.persist_path
        ):
            logger.info("chromadb_data exists")
            self.db = Chroma(
                persist_directory=self.persist_path,
                embedding_function=embeddings,
            )
        else:
            docs = to_documents(splitted_docs, self.pdf_path)
            self.db = build_chromadb(docs, embeddings, persist_path=self.persist_path)
        logger.info("FileAgent initialized successfully")

    def _get_shadow_llm(self):
        """惰性获取影子模式专用 LLM（仅 TOOL_SHADOW_MODE 开启且命中影子
        条件时才创建，避免每请求平白增加对象开销）。

        被谁调用：_run_shadow()。
        返回：聊天模型实例；若模型支持 set_user_id 则绑定 self.user_id
              （用量计量/画像透传），绑定失败静默忽略。
        """
        if self._shadow_llm is None:
            self._shadow_llm = build_chat_model()
            if hasattr(self._shadow_llm, "set_user_id"):
                try:
                    self._shadow_llm.set_user_id(self.user_id)
                except Exception:
                    pass
        return self._shadow_llm

    def handle(self):
        """执行上传文件检索并把结果回传 SummaryAgent（流水线阶段 3 的 File 分支入口）。

        被谁调用：service/agent_service.py 的 _run_retrieval()，经非关键
                  包装器 _run_non_critical_agent(sm, "retrieval_file",
                  agents["file"].handle, fallback_value=None) 调用；异常按
                  retrieval 重试上限重试，耗尽后编排层以空结果继续。
        参数：无（任务在方法内经 bus.subscribe("FileAgent") 获取，
              生产者为 AnalysisAgent.create_agent，payload 含
              query/top_k/has_uploaded_files 等决策字段）。
        返回：None（结果通过总线传递，编排层只取"是否调用"标记）。
        消息输出：bus.publish("FileAgent", "SummaryAgent", result_msg)，
              payload = {"query": str, "top_k": int, "results":
              [{"content": 命中文档正文, "metadata": 元数据}]}；
              检索经 retrieve_scoped 同时覆盖 self.db（PDF 演示库，
              可为 None）与当前会话临时库（service/temp_knowledge_store）。
        空 query：告警并跳过该消息；无消息时发布空 results，
              SummaryAgent 侧据此走"未检索到"降级。
        影子模式：TOOL_SHADOW_MODE 开启、收到消息且上游标记
              has_uploaded_files 时，调 _run_shadow 旁路对比（不影响主流程）。
        """
        messages = self.bus.subscribe("FileAgent")
        result_msg = {"query": "", "top_k": self.top_k, "results": []}

        for msg in messages:
            data = msg.get("message", {})
            query = data.get("query", "")
            top_k = data.get("top_k", self.top_k)
            if not query:
                logger.warning("query is empty")
                continue
            # retrieve_scoped 独立处理 temp store 检索，self.db 为 None 时仍可检索会话临时文件
            results = retrieve_scoped(
                self.db, query, top_k, user_id=self.user_id, session_id=self.session_id
            )
            result_msg = {
                "query": query,
                "top_k": top_k,
                "results": [
                    {"content": result.page_content, "metadata": result.metadata}
                    for result in results
                ],
            }
        self.bus.publish("FileAgent", "SummaryAgent", result_msg)
        logger.info("FileAgent handled successfully")

        # P1 影子模式：本会话有上传文件时旁路执行新工具链并对比，不影响主流程
        if settings.TOOL_SHADOW_MODE and messages and data.get("has_uploaded_files"):
            self._run_shadow(data, result_msg)

    def _run_shadow(self, payload: dict, legacy_result: dict) -> None:
        """P1 影子模式：旁路执行 session_file_search 新工具链并与旧结果对比。

        被谁调用：handle() 末尾——settings.TOOL_SHADOW_MODE 开启、收到消息
                  且 payload 标记 has_uploaded_files（本会话确有上传文件）时。
        参数：
        - payload：上游 AnalysisAgent 的消息 dict（影子新链路的输入，
          含 query/user_id/session_id 等）；
        - legacy_result：旧链路刚产出的 result_msg（对比基准）。
        返回：None。影子结果只用于观测对比（tools.shadow.run_shadow 内部
              记录/落库），任何异常都被吞掉记 debug 日志，绝不影响主链路。
        """
        try:
            from tools.shadow import run_shadow

            ctx = ToolContext(
                user_id=self.user_id,
                session_id=self.session_id,
                role="user",
                task_id=getattr(self.bus, "task_id", ""),
            )
            run_shadow("FileAgent", self._get_shadow_llm(), payload, legacy_result, ctx)
        except Exception as e:  # noqa: BLE001 影子链路任何异常都不得影响主链路
            logger.debug("FileAgent shadow run failed: %s", e, exc_info=True)
