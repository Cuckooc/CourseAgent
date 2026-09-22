"""
模块名：multi_agent.chat_agent

作用：
    流水线阶段 5 的最终回答 Agent。订阅 MessageBus 中上游各 Agent 的产物
    （VagueAgent 的澄清分支消息、SummaryAgent 的检索汇总素材），结合用户
    原始问题、对话历史、会话关键词、用户画像组装 ChatLLM 提示词，经 LCEL
    链生成最终自然语言回答；并提供流式生成入口供 SSE 逐字推送前端。

主要成员：
    - ChatAgent：独立实现（不继承 BaseAgent），关键方法 _build_chain
      （订阅总线 + 组装 LCEL 链，同步/流式共用）、handle（同步生成）、
      handle_stream（流式生成）。

被谁使用（Grep 模块名结果）：
    - service/agent_service.py：AgentService._create_agents() 中
      ChatAgent(message_bus=..., query=..., history=...,
      session_keywords=..., user_profile=...) 每请求实例化；
      run_agent() 阶段 1（意图模糊直接澄清）与阶段 5 调
      agents["chat"].handle()；run_agent_stream() 经 _stream_chat()
      消费 handle_stream() 的文本增量，包装为 {"type":"delta"} 帧，
      经 ChatService → control/chat_control.py 的 SSE 流返回前端逐字渲染。

Agent 间数据流：
    - 输入（消费方）：MessageBus 中 receiver="ChatAgent" 的消息，生产者为
      VagueAgent（模糊分支，自带 query）、SummaryAgent（answer/keywords）
      及 agent_service 编排层补发的兜底素材；
    - 输出：返回 dict（success/answer）给 agent_service 状态机决策，
      或 yield 文本增量给 SSE 流（最终回答去向：前端用户）。
"""
from .message_bus import MessageBus
from config.setting import llm,agent
from model_llm.gateway import build_chat_model, LLMUnavailableError
from model_llm.llm_business import ChatLLM
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from datetime import datetime
import logging
logger = logging.getLogger(__name__)

class ChatAgent:
    """最终回答生成 Agent（流水线终点；独立实现，不继承 BaseAgent）。

    类作用：
        消费总线上游素材，把"用户问题 + 多轮历史 + 检索汇总 + 画像/关键词"
        填入 ChatLLM 模板，调用 LLM 产出面向用户的最终回答（同步或流式）。
    继承关系：无基类（与 RAGAgent/FileAgent/SummaryAgent 一样为独立类，
        不实现 BaseAgent 抽象接口 create_agent，调度入口为 handle/handle_stream）。
    实例化位置：service/agent_service.py 的 AgentService._create_agents()，
        每对话请求创建一个，注入本次请求专属 MessageBus。
    关键 self 属性含义与去向：
        - self.bus：订阅 receiver="ChatAgent" 邮箱获取上游产物；
        - self.query/self.history：用户问题与多轮历史（来源 ChatService
          上下文改写与 memory/context_memory），进入 prompt 的 input/history；
        - self.session_keywords/self.user_profile：会话关键词与用户画像
          注入前缀（来源 memory/session_keyword_service、profile_service），
          作为独立 prompt 字段，避免污染上游意图判断与检索 embedding；
        - self.llm：经 model_llm.gateway 构建的聊天模型（含主备网关）。
    """

    def __init__(self,
                 message_bus: MessageBus,
                 query: str = None,
                 history: str = None,
                 session_keywords: str = None,
                 user_profile: str = None):
        """初始化最终回答 Agent。

        被谁调用：AgentService._create_agents()（service/agent_service.py，
                  每请求一次）。
        参数：
        - message_bus：本次请求专属 MessageBus（来源：编排层新建并注入），
          ChatAgent 据其订阅上游消息；
        - query：改写后的用户最终查询（来源：ChatService 上下文改写），
          进入 prompt 的 input；为 None 时回退用总线消息携带的 query；
        - history：早期摘要 + 最近 N 轮对话原文（来源：
          memory/context_memory，经 ChatService 透传）；
        - session_keywords：会话累积关键词注入前缀（来源：
          memory/session_keyword_service）；
        - user_profile：用户画像注入前缀（来源：memory/profile_service）。
        """
        self.bus=message_bus
        # 用户原始问题 + 对话历史：修复旧实现 prompt 的 input 仅含上游 Agent
        # 元数据（意图描述/检索汇总）、用户问题与多轮历史从未进入 LLM
        # 导致答非所问、会话内记忆失效的缺陷
        self.query = query
        self.history = history
        self.session_keywords = session_keywords
        # 用户画像作为独立 prompt 字段（不再拼接进 query，
        # 避免污染上游意图判断与检索 embedding）
        self.user_profile = user_profile
        self.model_name = llm.MODEL
        self.api_key = llm.API_KEY
        self.base_url = llm.BASE_URL
        self.temperature = llm.TEMPERATURE
        self.llm = build_chat_model(temperature=self.temperature)
        self.agent_verbose = agent.VERBOSE
        self.max_iterations = agent.MAX_ITERATIONS

    def _build_chain(self):
        """构建对话链：订阅总线上游消息并组装 LCEL 链（invoke 与 stream 共用）。

        被谁调用：ChatAgent.handle()（同步）与 ChatAgent.handle_stream()
                  （流式）的入口，两个生成分支共用同一上下文构造逻辑。
        输入来源：self.bus.subscribe("ChatAgent")——消费 VagueAgent（模糊
                  澄清分支，消息含 query）与 SummaryAgent（含 answer/content）
                  发到 ChatAgent 邮箱的消息；subscribe 读取后邮箱即清空。
        返回：可执行 LCEL 链（RunnablePassthrough.assign 注入变量 →
              ChatPromptTemplate（模板来源 ChatLLM().generate()）→ self.llm
              → StrOutputParser）；去向：调用方 invoke({}) 同步取结果或
              stream({}) 取 token 流。
        """
        # 消费自己邮箱中的全部上游消息（生产者：VagueAgent/SummaryAgent/编排层）
        message=self.bus.subscribe("ChatAgent")
        context_parts = []
        question = self.query or ""
        for msg in message:
            msg_data = msg.get("message", {})
            # 兼容总线携带的原始问题（VagueAgent 澄清分支自带 query 字段）
            if not question:
                question = msg_data.get("query", "") or ""
            content = msg_data.get("answer", "") or msg_data.get("content", "")
            if content:
                context_parts.append(content)

        context = "\n".join(context_parts)
        if not context:
            context = "暂无相关信息"

        # 最终回答提示词模板：来源 model_llm/llm_business.py 的 ChatLLM
        prompt = ChatPromptTemplate.from_template(ChatLLM().generate())

    # 构建链式调用（LCEL）：先注入日期/检索上下文/历史/画像/关键词/问题等模板变量
        chain = (

        RunnablePassthrough.assign(
            current_date=lambda x: datetime.now().strftime("%Y年%m月%d日"),
            rag_context=lambda x: context,
            history=lambda x: self.history or "无",
            session_keywords=lambda x: self.session_keywords or "无",
            user_profile=lambda x: self.user_profile or "无",
            input=lambda x: question or context
        )
        | prompt
        | self.llm
        | StrOutputParser()
    )
        return chain

    def handle(self):
        """同步生成最终回答（流水线阶段 5 的主入口）。

        被谁调用：service/agent_service.py 的 run_agent()——阶段 1 意图
                  模糊时直接调用产出澄清回答；阶段 5 正常流程调用产出最终
                  回答；_retry_chat_for_rollback() 回滚重跑时也调用本方法。
        参数：无（上游素材在 _build_chain 内部经总线订阅获得）。
        返回：dict——
          * 成功：{"success": True, "answer": 最终回答文本}，去向：编排层
            经 IntentVerifier 做意图一致性校验，通过后包装进 run_agent
            返回值的 chat_result，最终经 ChatService 返回前端；
          * 业务异常：{"success": False, "error": 异常文本,
            "answer": 友好错误文案}，由编排层替换为保底提示。
        异常：LLMUnavailableError（主备模型全不可用）直接上抛，交
              ChatService 统一降级；其他异常在方法内捕获转为 success=False。
        """
        try:
             result = self._build_chain().invoke({})
             logger.info(f"  ChatAgent {result}")
             return {
                 "success": True,
                 "answer": result
             }
        except LLMUnavailableError:
             # 模型服务整体不可用：交由 ChatService 统一降级，不吞成业务错误
             raise
        except Exception as e:
             output={
                 "success": False,
                 "error": str(e),
                 "answer": "抱歉，处理您的问题时出现错误，请稍后重试。"
             }
             logger.error("ChatAgent failed: %s", e)
             return output

    def handle_stream(self):
        """流式生成回答：逐段 yield LLM 文本增量。

        被谁调用：service/agent_service.py 的 AgentService._stream_chat()，
                  后者在 run_agent_stream() 阶段 1（模糊澄清）与阶段 5
                  消费本生成器，把非空增量包装为 {"type":"delta","content":...}
                  帧，经 ChatService → control/chat_control.py 的 SSE 流
                  推送前端逐字渲染。
        参数：无（上游素材在 _build_chain 内部经总线订阅获得）。
        返回：生成器——每次 yield 一个文本片段（str）；最终去向：SSE 流。
        异常：不在本方法内捕获——LLM/网络异常向上抛给服务层，由编排层
              转为 error 帧或走统一降级。
        """
        chain = self._build_chain()
        yield from chain.stream({})
       

         
 

        
        
        