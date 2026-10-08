"""
模块名：app.infrastructure.llm.llm

作用：
    定义 LLM 抽象基类，统一三件事：
    1. 导入期加载 env/qianwen_config.env（通义千问 DashScope 兼容模式配置）；
    2. 从 config/setting.py 的 llm 单例读取模型名 / api_key / base_url；
    3. 构造时经 model_llm/gateway.py 的 build_chat_model() 得到带「重试 + 模型降级」
       的 LLMGateway 实例（接口与 ChatOpenAI 一致）。
    子类只需实现 generate() 返回各业务场景的提示词模板。

主要成员：
    - LLM（ABC）：抽象基类，持有 model_name/api_key/base_url 与 self.llm 网关客户端；
    - LLM.generate：抽象方法，由子类返回提示词模板字符串。

被谁使用（Grep "model_llm.llm" / 子类名确认）：
    - model_llm/llm_business.py 的全部提示词类（RagLLM/FileLLM/ChatLLM/
      OptimizeLLM/InformationLLM/PredictLLM/AnalysisLLM/ContextKey/TitleLLM）
      均继承本类，并在 multi_agent 各 Agent（chat_agent、vague_agent、
      analysis_agent、summary_agent）、app/application/chat/title.py、app/application/chat/context.py 中被实例化。
"""
from abc import ABC, abstractmethod

from config.setting import llm
from app.infrastructure.llm.gateway import build_chat_model
# 环境变量由 config/setting.py 在导入期统一 load_dotenv(env/qianwen_config.env) 加载，此处不再重复加载
class LLM(ABC):
    """LLM 抽象基类：封装模型配置读取与网关客户端构造，子类只负责提供提示词模板。

    实例化位置：本类不直接实例化；由 model_llm/llm_business.py 各提示词子类经
    super().__init__() 间接构造（如 InformationLLM 传 json_mode=True），子类实例
    随后在 multi_agent 各 Agent 与 app/application/chat/title.py、app/application/chat/context.py 中创建。
    """

    def __init__(self, json_mode=False, **extra_kwargs):
        # type: (bool, **Any) -> None
        """初始化模型配置并构建网关客户端。

        参数：
            json_mode: 是否强制 JSON 对象输出。True 时透传
                model_kwargs={"response_format": {"type": "json_object"}}，
                供 InformationLLM（RAG 结果汇总需返回严格 JSON）使用；来源子类构造调用。
            **extra_kwargs: 额外透传给 build_chat_model 的 ChatOpenAI 参数
                （如 temperature/timeout），由各 Agent 按需覆盖默认值。
        关键属性去向：
            model_name/api_key/base_url 取自 config.setting.llm
            （env/qianwen_config.env 的 model/api_key/base_url），同时在
            app/domain/agents/base_agent.py 等 Agent 基类中被记录用于日志展示；
            self.llm 为 LLMGateway 实例，去向子类组装 LangChain chain
            （prompt | self.llm）后的 invoke / stream 调用。
        """
        self.model_name = llm.MODEL
        self.api_key = llm.API_KEY
        self.base_url = llm.BASE_URL
        kwargs = {}  # type: Dict[str, Any]
        if json_mode:
            # JSON 模式：要求模型响应体必须是合法 JSON 对象（InformationLLM 汇总场景）
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        kwargs.update(extra_kwargs)
        # 统一经网关构造：内置指数退避重试与降级模型链，详见 model_llm/gateway.py
        self.llm = build_chat_model(**kwargs)

    @abstractmethod
    def generate(self):
        """抽象方法：子类返回业务提示词模板字符串（含 {input} 等 LangChain 占位符）。

        返回：str 提示词模板，去向 ChatPromptTemplate/PromptTemplate.from_template
              渲染后交 self.llm 调用；无返回值实现（pass），具体内容由子类定义。
        """
        pass
