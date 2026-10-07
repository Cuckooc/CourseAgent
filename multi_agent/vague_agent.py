"""
模块名：multi_agent.vague_agent

作用：
    流水线阶段 1 的意图模糊判定 Agent。单次 LLM 调用判断用户问题是
    "模糊意图"还是"明确意图"，并据此经 MessageBus 把请求分流给
    ChatAgent（模糊：直接澄清/闲聊）或 AnalysisAgent（明确：进入分析）。

主要成员：
    - VagueAgent：BaseAgent 的子类，实现 create_agent() 判定入口；
      提示词模板来自 model_llm.llm_business.PredictLLM。

被谁使用（Grep 模块名结果）：
    - service/agent_service.py：AgentService._create_agents() 中
      VagueAgent(bus=message_bus, history_summary=history_summary)
      每请求实例化；run_agent()/run_agent_stream() 阶段 1 经
      _run_critical_agent(..., "vague", agents["vague"].create_agent, query)
      调度执行。
"""
import datetime
from .base_agent import BaseAgent
from typing import Dict,Any
from app.infrastructure.llm.llm_business import PredictLLM
from langchain_core.prompts import PromptTemplate
from .message_bus import MessageBus
import logging
logger = logging.getLogger(__name__)

class VagueAgent(BaseAgent):
    """意图模糊判定 Agent（BaseAgent 子类，流水线第一跳）。

    类作用：调用 LLM 对用户 query 做二分类（模糊/明确），把判定结果
    构造成统一 output dict 并发布到总线，决定流水线走向闲聊还是分析。
    实例化位置：service/agent_service.py 的 AgentService._create_agents()。
    """

    def __init__(self, bus: MessageBus, memory: Any = None, tools: Any = None,
                 history_summary: str = None):
        """初始化 VagueAgent。

        被谁调用：AgentService._create_agents()（每请求一次）。
        参数：
        - bus：本次请求专属 MessageBus（由编排层注入），用于发布判定结果；
        - memory：记忆管理器（预留，调用方未传，基类存为 None）；
        - tools：工具管理器（预留，调用方未传，基类存为 None）；
        - history_summary：早期对话摘要（来源：memory/context_memory
          经 ChatService 透传），注入 prompt 以消解多轮承接式提问中的
          指代（如"那它呢"）；无摘要时为 None，模板内回填"无"。
        关键属性去向：self.prompt 供 create_agent 构建 PromptTemplate；
        LLM 与总线在基类 BaseAgent 中构建。
        """
        super().__init__(agent_name="VagueAgent", bus=bus, memory=memory, tools=tools)
        # 意图判定提示词模板：来源 PredictLLM（model_llm/llm_business.py）
        self.prompt = PredictLLM().generate()
        self.verbose = self.agent_verbose
        # 早期对话摘要：消解多轮承接式提问中的指代（无摘要时为"无"）
        self.history_summary = history_summary

    def create_agent(self,query:str,context:Dict[str, Any]={})->Dict[str, Any]:
        """
        意图模糊判断：单次 LLM 调用直接输出「模糊意图」/「明确意图」。
        （修复：旧实现经 ReAct 循环执行，LLM 反复调用 vague 工具直至迭代上限
        （Agent stopped due to iteration limit），每条消息白耗 2-3 次 LLM 调用
        且常伴随 Invalid Format 解析错误重试；意图判定本身无需多轮工具调用。）

        被谁调用：service/agent_service.py 的 _run_critical_agent()
                  （run_agent/run_agent_stream 阶段 1，以 func(query) 包装调用）。
        参数：
        - query：用户原始问题（来源：状态机调度链传入的 ChatService 改写查询）；
        - context：上游上下文字典（默认 {}）；本方法写入 input 与
          history_summary（优先取 context 自带，其次构造注入的摘要）。
        返回：dict——
          * success：bool，True 表示意图模糊（注意：此处 success 语义是
            "命中模糊分支"，给编排层作为 is_vague 判定）；
          * query：回显原始问题；
          * context：实际送模的上下文；
          * answer：LLM 原始判定文本（去向：链路日志 record_output）；
          * tool_results：兼容旧工具格式的判定说明列表；
          * timestamp：判定时间戳；
          * error：异常时的错误文本（success=False）。
          输出去向：返回给状态机决策（模糊则直接调 ChatAgent 并 COMPLETED，
          明确则进入 ANALYZING），同时按下文分支 publish 到总线。
        消息分流：模糊 -> publish 给 ChatAgent（自带 query，供其澄清回答）；
                  明确 -> publish 给 AnalysisAgent。
        异常：LLMUnavailableError（主备模型全不可用）上抛交 ChatService
              统一降级；其他异常捕获后仍发消息给 AnalysisAgent 并返回
              success=False 结果（由状态机重试/兜底链处理）。
        """
        context=context if context else {}
        # 用户问题与早期摘要写入模板变量：input 为本轮 query，history_summary 消解指代
        context["input"]=query
        context["history_summary"]=context.get("history_summary") or self.history_summary or "无"
        prompt=PromptTemplate.from_template(self.prompt)
        chain = prompt | self.llm
        try:
            response = chain.invoke(context)
            answer = response.content if hasattr(response, "content") else str(response)
            answer = (answer or "").strip()

            # 判定：含「模糊意图」且不含「明确意图」→ 模糊；异常输出保守视为明确
            is_vague = ("模糊意图" in answer) and ("明确意图" not in answer)
            # 兼容旧 ReAct 工具链的结果结构：tool_name/tool_output 形式
            tool_result = [{
                "tool_name": "vague",
                "tool_output": (
                    "用户输入意图模糊，需要进一步澄清" if is_vague
                    else "用户问题意图明确，不需要进一步澄清"
                ),
            }]
            output = {
                "success": is_vague,
                "query": query,
                "context": context,
                "answer": answer or ("模糊意图" if is_vague else "明确意图"),
                "tool_results": tool_result,
                "timestamp": datetime.datetime.now().isoformat(),
            }
            if is_vague:
                # 模糊分支：消息去向 ChatAgent，编排层随后直接生成澄清式回答并结束
                logger.info(f"意识模糊：{{'query': {query!r}, 'answer': {answer!r}}}")
                self.bus.publish(self.agent_name,"ChatAgent",output)
            else:
                # 明确分支：消息去向 AnalysisAgent，进入下游工具分发判定
                logger.info(f"意图清楚：{{'query': {query!r}, 'answer': {answer!r}}}")
                self.bus.publish(self.agent_name,"AnalysisAgent",output)
            return output
        except Exception as e:
                from app.infrastructure.llm.gateway import LLMUnavailableError
                if isinstance(e, LLMUnavailableError):
                    # 模型服务整体不可用：交由 ChatService 统一降级
                    raise
                output = {
                "success": False,
                "query": query,
                "error": str(e)

             }
                # 业务异常：保守地把失败消息送 AnalysisAgent（success=False 由编排层重试/兜底处理）
                self.bus.publish(self.agent_name,"AnalysisAgent",output)
                return output
