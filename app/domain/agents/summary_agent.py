"""
模块名：app.domain.agents.summary_agent

作用：
    流水线阶段 4 的检索结果汇总 Agent。订阅 MessageBus 中 RAGAgent /
    FileAgent 回传的检索结果（results），拼接为上下文交给 InformationLLM
    做提炼汇总，输出 JSON（summary 汇总文本 + keywords 关键词列表），
    再经总线把汇总素材发布给 ChatAgent 作为最终回答依据；无检索结果或
    LLM 失败时降级为"基于通用知识回答"的兜底文案，不阻断主链路。

主要成员：
    - _parse_information_json：模块级函数，解析汇总 LLM 的 JSON 输出
      （先 Schema 校验，失败回退宽松解析）；
    - SummaryAgent：独立实现（不继承 BaseAgent），入口方法 handle()。

被谁使用（Grep 模块名结果）：
    - app/application/chat/agent_service.py：AgentService._create_agents() 中
      SummaryAgent(message_bus=message_bus) 每请求实例化；
      _run_summary_with_relevance() 经非关键包装器
      _run_non_critical_agent(sm, "summary", agents["summary"].handle, ...)
      调度，最多 AGENT_SUMMARY_MAX_ROUNDS 轮（相关性不足触发回退重检索）。

Agent 间数据流：
    - 输入（消费方）：bus.subscribe("SummaryAgent")，生产者为
      RAGAgent.handle / FileAgent.handle（payload 含 query/top_k/results）；
    - 输出（生产者）：bus.publish("SummaryAgent", "ChatAgent", output)，
      payload 含 answer（汇总文本/兜底文案）与 keywords（汇总关键词，
      相关性通过后由编排层累积进 app/domain/memory/session_keyword_service）。
"""
from .message_bus import MessageBus
from core.config import llm, agent
from app.application.ports.llm import build_chat_model, LLMUnavailableError
from core.degradation_alert import alert_degradation
from core.output_validator import validate_json_output, SummaryOutput
from langchain_core.prompts import PromptTemplate
from app.application.ports.llm_business import get_information_llm
import json
import logging
from typing import List, Tuple
logger = logging.getLogger(__name__)


def _parse_information_json(text: str) -> Tuple[str, List[str]]:
    """解析 SummaryAgent JSON 输出：先用 Schema 验证，失败则回退到宽松解析。

    被谁调用：SummaryAgent.handle() 解析 InformationLLM 原始返回文本。
    参数：text——LLM 输出原文（来源：prompt | InformationLLM 链的返回 content）。
    返回：(summary, keywords)——
      - summary：汇总文本 str，去向 output["answer"] 交总线给 ChatAgent；
      - keywords：关键词 str 列表，去向 output["keywords"]，相关性通过后
        由编排层累积进会话关键词（app/domain/memory/session_keyword_service）。
    解析策略（JSON 解析失败兜底）：Schema（core.output_validator.
      SummaryOutput）校验失败时先用 json.loads 宽松解析；再失败则把
      整段文本当作 summary、keywords 置空，保证汇总链路不因格式问题中断。
    """
    model, error = validate_json_output(text, SummaryOutput)
    if model is not None:
        return model.summary, model.keywords
    # Schema 验证失败，回退到宽松解析
    logger.warning("SummaryOutput schema validation failed: %s, falling back to loose parse", error)
    try:
        data = json.loads(text)
        summary = data.get("summary", "") or ""
        keywords = data.get("keywords", []) or []
        if isinstance(keywords, list):
            keywords = [str(kw).strip() for kw in keywords if kw]
        else:
            keywords = []
        return summary.strip(), keywords
    except (json.JSONDecodeError, TypeError, AttributeError):
        return text, []

class SummaryAgent:
    """检索结果汇总 Agent（流水线阶段 4；独立实现，不继承 BaseAgent）。

    类作用：
        消费 RAGAgent/FileAgent 的检索结果，调用 InformationLLM 把多路
        片段提炼为结构化汇总（summary + keywords），发布给 ChatAgent；
        在"无检索结果/LLM 调用失败"两种情况下产出通用知识兜底文案，
        保证最终回答链路不被阻断。
    继承关系：无基类（不实现 BaseAgent 抽象接口 create_agent），
        编排入口为 handle()，由非关键 Agent 包装器调度并重试。
    实例化位置：app/application/chat/agent_service.py 的 AgentService._create_agents()，
        SummaryAgent(message_bus=message_bus) 每请求实例化。
    关键 self 属性含义与去向：
        - self.bus：订阅 receiver="SummaryAgent" 邮箱取检索结果，
          汇总后向 receiver="ChatAgent" 邮箱发布产物；
        - self.llm：经 model_llm.gateway 构建的聊天模型（含主备网关）；
        - self.agent_verbose / self.max_iterations：来源 config/setting.py
          的 agent 段（日志开关与 ReAct 迭代上限预留）。
    """

    def __init__(self,
                 message_bus: MessageBus):
        """初始化汇总 Agent。

        被谁调用：AgentService._create_agents()（app/application/chat/agent_service.py，
                  每请求一次）。
        参数：
        - message_bus：本次请求专属 MessageBus（来源：编排层新建并注入），
          SummaryAgent 据其订阅 RAGAgent/FileAgent 的检索结果并向
          ChatAgent 发布汇总产物。
        """
        self.bus=message_bus
        self.model_name = llm.MODEL
        self.api_key = llm.API_KEY
        self.base_url = llm.BASE_URL
        self.temperature = llm.TEMPERATURE
        self.llm = build_chat_model(temperature=self.temperature)
        self.agent_verbose = agent.VERBOSE
        self.max_iterations = agent.MAX_ITERATIONS

    def handle(self):
        """汇总检索结果并向 ChatAgent 发布素材（流水线阶段 4 入口）。

        被谁调用：app/application/chat/agent_service.py 的 _run_summary_with_relevance()
                  经非关键包装器 _run_non_critical_agent(sm, "summary",
                  agents["summary"].handle, ...) 调用；失败按非关键 Agent
                  重试上限重试，耗尽后由编排层用降级值收场。
        参数：无（输入在方法内经 bus.subscribe("SummaryAgent") 获取，
              消息生产者为 RAGAgent.handle / FileAgent.handle）。
        返回：dict——
          * 正常：{"success": True, "answer": 汇总文本,
            "keywords": [关键词...]}；
          * 无检索结果 / LLM 业务异常：{"success": True,
            "answer": 通用知识兜底文案}（并经 core.degradation_alert
            上报告警），保证不阻断最终回答。
          去向：返回值交编排层做 IntentVerifier.score_relevance 相关性
          评分（低于 AGENT_SUMMARY_RELEVANCE_THRESHOLD 时回退重检索，
          最多 AGENT_SUMMARY_MAX_ROUNDS 轮）；同一份 dict 同步
          publish 到 ChatAgent 邮箱作为最终回答素材。
        异常：LLMUnavailableError（主备模型全不可用）上抛交 ChatService
              统一降级；其他异常捕获后降级为通用知识文案。
        """
        # 消费检索 Agent 的消息邮箱（生产者：RAGAgent/FileAgent）
        message=self.bus.subscribe("SummaryAgent")
        contest_parts=[]

        for msg in message:
            data=msg.get("message",{})
            # 跳过上游明确标记失败的消息，避免把错误信息当检索结果拼入汇总
            if data.get("success") is False:
                continue
            result=data.get("results",[])
            if isinstance(result,list):
                for item in result:
                    content=item.get("content","")
                    if content:
                        contest_parts.append(content)
            else:
                contest_parts.append(str(result))
        context="\n\n".join(contest_parts)

        # 兜底：无检索结果时跳过 LLM 汇总，直接告知 ChatAgent 用通用知识回答
        if not context.strip():
            logger.info("SummaryAgent: no retrieval results, falling back to general knowledge")
            alert_degradation(
                stage="summary", severity="warn",
                error="no retrieval results, falling back to general knowledge",
            )
            output={
                "success": True,
                "answer": "未检索到相关知识库内容，请基于通用知识简要回答用户问题，并注明为一般性建议。"
            }
            self.bus.publish("SummaryAgent","ChatAgent",output)
            return output

        # 汇总提示词模板：来源 model_llm/llm_business.py 的 InformationLLM
        prompt=get_information_llm().generate()
        prompt=PromptTemplate.from_template(prompt)
        # LCEL 链：模板变量 rag_results 为拼接后的全部检索片段；输出要求 JSON（summary/keywords）
        chain=prompt | get_information_llm().llm

        try:
             result=chain.invoke({ "rag_results": context})
             if hasattr(result,"content"):
                 raw=result.content.strip()
             else:
                 raw=str(result).strip()

             summary_text, keywords = _parse_information_json(raw)

             output={
                 "success": True,
                 "answer": summary_text,
                 "keywords": keywords,
             }

             self.bus.publish("SummaryAgent","ChatAgent",output)
             return output
        except LLMUnavailableError:
             # 模型服务整体不可用：交由 ChatService 统一降级，不吞成业务错误
             raise
        except Exception as e:
             # 兜底：汇总 LLM 调用失败时降级为通用知识回答，不阻断最终输出
             logger.warning("SummaryAgent LLM call failed, falling back to general knowledge: %s", e)
             alert_degradation(
                 stage="summary", severity="warn",
                 error=str(e),
             )
             output={
                 "success": True,
                 "answer": "检索结果汇总失败，请基于通用知识简要回答用户问题，并注明为一般性建议。"
             }
             self.bus.publish("SummaryAgent","ChatAgent",output)
             return output
