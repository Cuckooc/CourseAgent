"""
模块名：app.domain.agents.base_agent

作用：
    定义多 Agent 流水线中"判定类 Agent"的抽象基类 BaseAgent，统一封装
    LLM 模型构建、Agent 元信息（名称/记忆/工具/总线）、通用执行模板
    （run：按决策结果选择调工具或调 LLM）与跨 Agent 发消息便捷方法。

主要成员：
    - BaseAgent：抽象基类，声明抽象方法 create_agent，并提供 run /
      send_message 两个通用模板方法。

被谁使用（Grep 结果）：
    - app/domain/agents/vague_agent.py：class VagueAgent(BaseAgent)
    - app/domain/agents/analysis_agent.py：class AnalysisAgent(BaseAgent)
    （ChatAgent / RAGAgent / FileAgent / SummaryAgent 为独立实现，
      不继承本基类；它们在 service/agent_service.py 中由
      AgentService._create_agents() 直接实例化。）
"""
from abc import ABC, abstractmethod
from config.setting import llm,agent
from app.infrastructure.llm.gateway import build_chat_model
from typing import Dict, Any,Optional
from .message_bus import MessageBus



class BaseAgent(ABC):
    """所有"判定类 Agent"的抽象基类（接口约定 + 通用能力复用）。

    类作用：
        统一各 Agent 的 LLM 构建方式与公共属性，并约定子类必须实现
        create_agent() 作为状态机调度入口；run() / send_message() 为
        可选复用的通用模板方法。

    接口契约（子类实现情况，Grep `(BaseAgent)` 结果）：
        - VagueAgent（app/domain/agents/vague_agent.py）：意图模糊判定，
          输出经 MessageBus 分流到 ChatAgent（模糊）或 AnalysisAgent（明确）；
        - AnalysisAgent（app/domain/agents/analysis_agent.py）：下游工具分发判定，
          输出 need_RAGAgent/need_FileAgent 标志并向 RAGAgent/FileAgent 发消息。

    实例化位置：
        本类为抽象类不可直接实例化；两个子类均在
        service/agent_service.py 的 AgentService._create_agents() 中
        按"每请求一次"创建（随本次请求的独立 MessageBus 一起构造）。

    关键属性去向：
        - self.llm：供子类 create_agent() 中 prompt | llm 链调用；
        - self.bus：供子类 publish 决策结果给下游 Agent；
        - self.memory / self.tools：预留给记忆与工具管理器注入
          （当前 VagueAgent/AnalysisAgent 均以 None 注入，未实际使用）。
    """

    def __init__(self,agent_name:str,bus:MessageBus,memory:Optional[Any]=None,tools:Optional[Any]=None):
        """初始化 Agent 公共组件。

        被谁调用：由子类（VagueAgent/AnalysisAgent）的 __init__ 经
                  super().__init__(...) 调用，service/agent_service.py
                  的 _create_agents() 是这些子类的最终实例化位置。
        参数：
        - agent_name：Agent 名称（如 "VagueAgent"/"AnalysisAgent"），
          由子类硬编码传入；用作 MessageBus 发消息时的 sender 标识与日志名。
        - bus：本次请求专属的 MessageBus 实例，由 AgentService 在
          _create_agents() 中创建（每对话请求独立，避免并发串话）后注入。
        - memory：记忆管理器实例（预留，当前调用方传 None）。
        - tools：工具管理器实例（预留，需提供 run(tool_name, **params)
          接口；当前调用方传 None）。
        """
        # LLM 连接配置：来源 config/setting.py 的 llm 配置段（模型名/密钥/网关地址/温度）
        self.model_name = llm.MODEL
        self.api_key = llm.API_KEY
        self.base_url = llm.BASE_URL
        self.temperature = llm.TEMPERATURE
        # 统一经 model_llm.gateway 构建聊天模型（内含主备网关与不可用异常 LLMUnavailableError）
        self.llm = build_chat_model(temperature=self.temperature)
        self.agent_name = agent_name
        self.memory = memory
        self.tools = tools 
        # Agent 行为配置：VERBOSE 日志开关、MAX_ITERATIONS ReAct 迭代上限（来源 config/setting.py 的 agent 段）
        self.agent_verbose = agent.VERBOSE
        self.max_iterations = agent.MAX_ITERATIONS   
        self.bus=bus 
    @abstractmethod
    def create_agent(self,query:str,context:Dict[str, Any]={})->Dict[str, Any]:
        """
        创建代理（抽象方法）：子类的状态机调度入口，执行本 Agent 的核心判定。

        被谁调用：service/agent_service.py 的 _run_critical_agent()
                  （以 agents["vague"].create_agent / agents["analysis"].create_agent
                  形式被 func(*args) 包装调用）。
        参数：
        - query：用户原始问题（来源：state_machine 调度链传入，
          实际为 ChatService 上下文改写后的最终查询）。
        - context：上游产物字典（来源：state_machine 调度链；
          可携带 history_summary 等早期对话摘要），默认空 dict。
        返回：dict 决策结果——典型字段 success/query/answer/tool_results/
              error，以及子类特有分流字段（VagueAgent 用 success 表示是否模糊；
              AnalysisAgent 用 need_RAGAgent/need_FileAgent）；
              去向：state_machine（经 _run_critical_agent 的 record_output
              记入链路日志并据此决定下一节点），同时子类内部会经
              MessageBus.publish 把结果发给下游 Agent。
        异常：子类实现中 LLMUnavailableError 上抛交 ChatService 统一降级；
              其他异常在子类内部捕获并转为 success=False 结果。
        """
        raise NotImplementedError(f"{self.agent_name}未实现")
    def run(self,result:Dict[str, Any])->Any:
        """
        运行代理（通用执行模板）：按上游决策结果选择"调工具"或"调 LLM"。

        被谁调用：当前 state_machine 编排链未直接调用本方法（各 Agent 以
                  create_agent/handle 为实际入口）；保留为工具型 Agent 的
                  复用模板。
        参数：
        - result：决策 dict（来源：create_agent 风格的 LLM 决策输出），
          关键字段：
          * need_tool：是否需要调用外部工具（去向：tools 执行分支）；
          * tool_name：工具名（给 self.tools.run 选择具体工具）；
          * params：工具参数 dict（以 **params 展开传给工具）；
          * prompt：无需工具时直接送 LLM 的提示词。
        返回：工具执行结果或 LLM 生成结果；工具管理器/LLM 未初始化时返回
              {"error": ...} 错误 dict。
        """
        # 分支一：决策要求调用工具——把 tool_name/params 交给工具管理器执行
        if result.get("need_tool",False):
            tool_name = result.get("tool_name")
            params= result.get("params",{})
            if self.tools and hasattr(self.tools,"run"):
                return self.tools.run(tool_name,**params)
            else:
                return{"error":"工具管理器未初始化"}
        # 分支二：无需工具——直接用 prompt 请求 LLM 生成
        prompt=result.get("prompt","")
        if self.llm and hasattr(self.llm,"generate"):
            return self.llm.generate(prompt)
        else:
            return {"error":"LLM模型未初始化"}
    def send_message(self,target_agent:str,message:Dict[str, Any],message_bus:Any)->None:
        """
        发送消息给代理（MessageBus.publish 的便捷封装）。

        被谁调用：当前各子类直接使用 self.bus.publish(...)，本方法为保留的
                  统一封装入口，无外部调用方。
        参数：
        - target_agent：接收方 Agent 名称（如 "ChatAgent"/"AnalysisAgent"）。
        - message：业务消息 dict（来源：发送方 Agent 的决策/产物），
          去向：进入接收方在 MessageBus 中的邮箱。
        - message_bus：消息总线实例（需提供 publish 方法）。
        返回：None。
        """
        if hasattr(message_bus,"publish"):
            message_bus.publish(self.agent_name,target_agent,message)