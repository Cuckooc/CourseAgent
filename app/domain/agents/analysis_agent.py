"""
模块名：app.domain.agents.analysis_agent

作用：
意图分析 Agent：根据用户问题语义判断需要调用的下游 Agent（RAGAgent / FileAgent）。

v3 改造：
- _check_uploaded_files 改为当前用户/会话范围检查（不再扫描全局目录）；
- prompt 围绕课程咨询场景做语义理解，不再依赖关键词精确匹配；
- 单次 LLM 直判输出 JSON（弃用 ReAct 循环：旧实现反复调用工具直至迭代上限）。

主要成员：
- _ALLOWED_EXTS：可参与上传文件检查的扩展名白名单（模块级常量）；
- AnalysisAgent：BaseAgent 子类，create_agent() 输出 need_RAGAgent/
  need_FileAgent/has_uploaded_files 决策字段并向总线发布检索任务。

被谁使用（Grep 模块名结果）：
- app/application/chat/agent_service.py：AgentService._create_agents() 中
  AnalysisAgent(bus=..., user_id=..., session_id=..., history_summary=...)
  每请求实例化；run_agent()/run_agent_stream() 阶段 2 经
  _run_critical_agent(..., "analysis", agents["analysis"].create_agent, query)
  调度；其 need_* 字段随后被 _run_retrieval() 读取决定执行哪些检索；
- tests/phase/test_all_changes.py：测试中实例化验证分发判定。
"""
import datetime
import json
import re

from .base_agent import BaseAgent
from typing import Dict, Any
from app.application.ports.llm import LLMUnavailableError
from app.application.ports.llm_business import build_analysis_prompt
from langchain_core.prompts import PromptTemplate
from .message_bus import MessageBus

from core.config import settings

logger = __import__("logging").getLogger(__name__)

# 上传文件检查的扩展名白名单：仅这些类型计入"当前用户/会话是否有可检索文件"
_ALLOWED_EXTS = (".pdf", ".docx", ".txt", ".pptx", ".html", ".ipynb", ".md")


class AnalysisAgent(BaseAgent):
    """下游工具分发判定 Agent（流水线阶段 2；BaseAgent 子类）。

    类作用：
        单次 LLM 调用对用户问题做语义理解，输出 JSON 决策——是否需要
        RAGAgent（知识问答）/FileAgent（上传文件问答），并把同一份决策
        dict 经 MessageBus 发布给被选中的下游 Agent；附带检查当前
        用户/会话是否存在上传文件（has_uploaded_files）供下游参考。
    继承关系：实现 BaseAgent 抽象接口 create_agent（基类另一子类为
        VagueAgent，见 app/domain/agents/base_agent.py）；LLM/总线等公共能力
        由 BaseAgent.__init__ 构建。
    实例化位置：app/application/chat/agent_service.py 的 AgentService._create_agents()，
        AnalysisAgent(bus=..., user_id=..., session_id=...,
        history_summary=...) 每请求实例化；run_agent/run_agent_stream
        阶段 2 经 _run_critical_agent(..., "analysis",
        agents["analysis"].create_agent, query) 调度。
    关键 self 属性含义与去向：
        - self.prompt：分发判定提示词模板（来源 AnalysisLLM().generate()），
          在 create_agent 中经 PromptTemplate 渲染；
        - self.user_id/self.session_id：来源编排层注入（JWT 用户/会话号），
          仅用于 _check_uploaded_files 的上传目录范围匹配；
        - self.history_summary：早期对话摘要（来源 app/domain/memory/context_memory
          经 ChatService 透传），注入 prompt 消解多轮指代。
    """

    def __init__(self, bus: MessageBus, memory: Any = None, tools: Any = None,
                 user_id: int = None, session_id: int = None,
                 history_summary: str = None):
        """初始化分析 Agent。

        被谁调用：AgentService._create_agents()（app/application/chat/agent_service.py，
                  每请求一次），随后经 super().__init__ 完成基类公共组件构建。
        参数：
        - bus：本次请求专属 MessageBus（编排层注入），用于发布检索任务；
        - memory：记忆管理器（预留，当前调用方传 None）；
        - tools：工具管理器（预留，当前调用方传 None）；
        - user_id：JWT 当前用户 ID（来源：请求上下文），决定上传文件
          检查的私有范围（{user_id}_ 前缀文件）；
        - session_id：会话号（来源：请求上下文），决定会话临时目录
          UPLOAD_DIR/temp/{user_id}_{session_id}/ 的检查范围；
        - history_summary：早期对话摘要（来源：app/domain/memory/context_memory），
          无摘要时 create_agent 内回填"无"。
        """
        super().__init__(agent_name="AnalysisAgent", bus=bus, memory=memory, tools=tools)
        self.prompt = build_analysis_prompt()
        self.verbose = self.agent_verbose
        self.user_id = user_id
        self.session_id = session_id
        # 早期对话摘要：消解多轮承接式提问中的指代（无摘要时为"无"）
        self.history_summary = history_summary

    def _check_uploaded_files(self) -> bool:
        """检查**当前用户/会话**是否存在可用上传文件。

        范围（与存储命名规则一致）：
        - 私有/公共上传：UPLOAD_DIR 根目录下 {user_id}_ 前缀的文件；
        - 会话临时文件：UPLOAD_DIR/temp/{user_id}_{session_id}/ 目录。
        （修复：旧实现扫描全局 UPLOAD_DIR 与旧 file_analysis/ 目录，
        任何用户上传过文件后全站用户 need_FileAgent 恒为 True，
        导致问候语等无关问题也触发文件检索。）

        被谁调用：create_agent()（决策前与异常分支各调用一次）。
        参数：无（范围由 self.user_id/self.session_id 决定）。
        返回：bool——True 表示当前用户/会话存在白名单类型的上传文件，
              作为 output["has_uploaded_files"] 元数据随决策发给下游；
              不直接决定 need_FileAgent（分发由 LLM 语义判定）。
        """
        upload_dir = settings.UPLOAD_DIR
        if not upload_dir.is_dir():
            return False

        uid_prefix = "{}_".format(int(self.user_id)) if self.user_id else None
        for f in upload_dir.iterdir():
            if f.is_file() and f.suffix.lower() in _ALLOWED_EXTS:
                if uid_prefix is None or f.name.startswith(uid_prefix):
                    return True

        if self.user_id and self.session_id:
            session_dir = upload_dir / "temp" / "{}_{}".format(
                int(self.user_id), int(self.session_id)
            )
            if session_dir.is_dir() and any(
                sf.is_file() and sf.suffix.lower() in _ALLOWED_EXTS
                for sf in session_dir.iterdir()
            ):
                return True
        return False

    def create_agent(self, query: str, context: Dict[str, Any] = {}) -> Dict[str, Any]:
        """
        工具分发判断：单次 LLM 调用直接输出 JSON。
        （修复：旧实现经 ReAct 循环执行，LLM 反复调用 course/file 工具直至
        迭代上限（Agent stopped due to iteration limit），每条消息白耗约 7s
        且常伴随 Invalid Format 解析错误重试；工具分发判定本身无需多轮工具调用。）

        被谁调用：app/application/chat/agent_service.py 的 _run_critical_agent()
                  （run_agent/run_agent_stream 阶段 2，以 func(query) 包装调用；
                  _retry_chat_for_rollback 回滚重跑时也会再次调用）。
        参数：
        - query：用户问题（来源：状态机调度链传入的 ChatService 改写查询，
          即明确意图分支的输入；VagueAgent 的判定产物不直接入参）；
        - context：上游产物/状态上下文 dict（默认 {}）；本方法写入
          input（本轮 query）与 history_summary（优先 context 自带，
          其次构造注入的早期摘要，缺省"无"）后渲染进 prompt。
        返回：dict 决策结果——
          * success：LLM 调用是否成功；
          * query/context/answer：回显问题、实际送模上下文、LLM 原始 JSON 文本；
          * tool_results：兼容旧工具链的 [{"tool_name","tool_output"}] 列表；
          * need_RAGAgent / need_FileAgent：下游分发开关（解析失败时保守
            默认 True/False，保证教育类问题仍走知识检索）；
          * has_uploaded_files：当前用户/会话是否有上传文件；
          * timestamp：决策时间戳；异常分支额外含 error 且两个 need_* 为 False。
          去向：经状态机 record_output 记入链路日志；need_* 字段随后被
          AgentService._run_retrieval() 读取决定执行哪些检索 Agent。
        消息分流（生产者 AnalysisAgent）：need_FileAgent=True 时 publish
          给 FileAgent，need_RAGAgent=True 时 publish 给 RAGAgent，
          payload 即整个 output dict（消费者在阶段 3 订阅取 query）。
        异常：LLMUnavailableError（主备模型全不可用）上抛交 ChatService
              统一降级；JSON 解析失败不抛错，按保守默认继续；其他异常
              捕获后返回 success=False 结果，由状态机重试/兜底链处理。
        """
        context = context if context else {}
        context["input"] = query
        context["history_summary"] = context.get("history_summary") or self.history_summary or "无"

        # 当前用户/会话是否有上传文件（仅作为输出元数据供下游参考）
        has_uploaded_files = self._check_uploaded_files()

        prompt = PromptTemplate.from_template(self.prompt)
        chain = prompt | self.llm
        try:
            response = chain.invoke(context)
            answer = response.content if hasattr(response, "content") else str(response)
            answer = (answer or "").strip()

            # 解析 JSON：提取首个 {...} 片段；解析失败时保守默认
            # need_RAGAgent=True（保证教育类问题仍走检索）、need_FileAgent=False
            need_rag = True
            need_file = False
            try:
                match = re.search(r"\{.*\}", answer, re.DOTALL)
                if match:
                    parsed = json.loads(match.group())
                    need_rag = bool(parsed.get("need_RAGAgent", True))
                    need_file = bool(parsed.get("need_FileAgent", False))
                else:
                    logger.warning(
                        "AnalysisAgent: no JSON in answer, using conservative defaults: %r",
                        answer[:120],
                    )
            except (ValueError, AttributeError) as e:
                logger.warning(
                    "AnalysisAgent: JSON parse failed (%s), using conservative defaults: %r",
                    e, answer[:120],
                )

            tool_result = [{
                "tool_name": "analysis",
                "tool_output": answer,
            }]

            output = {
                "success": True,
                "query": query,
                "context": context,
                "answer": answer,
                "tool_results": tool_result,
                "need_RAGAgent": need_rag,
                "need_FileAgent": need_file,
                "has_uploaded_files": has_uploaded_files,
                "timestamp": datetime.datetime.now().isoformat()
            }

            logger.info(
                "analysis decision: need_RAGAgent=%s need_FileAgent=%s (has_files=%s)",
                need_rag, need_file, has_uploaded_files,
            )
            if need_file:
                self.bus.publish(self.agent_name, "FileAgent", output)
            if need_rag:
                self.bus.publish(self.agent_name, "RAGAgent", output)

            return output

        except Exception as e:
            if isinstance(e, LLMUnavailableError):
                # 模型服务整体不可用：交由 ChatService 统一降级
                raise
            output = {
                "success": False,
                "query": query,
                "error": str(e),
                "need_RAGAgent": False,
                "need_FileAgent": False,
                "has_uploaded_files": self._check_uploaded_files()
            }
            return output
