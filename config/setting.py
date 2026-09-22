"""
模块名：config.setting

作用：
    通义千问（DashScope OpenAI 兼容模式）与 Agent 行为的配置中心。
    模块导入期从本地 env/qianwen_config.env 加载环境变量（load_dotenv，
    不覆盖已存在的真实环境变量），随后 LLMConfig / AgentConfig 在类定义
    执行期间逐字段 os.getenv 读取并固化为类属性，最后导出单例 llm、agent。

主要成员：
    - LLMConfig：模型名 / 密钥 / base_url / 温度 / 相似度阈值 / 网关
      （超时、重试、退避、降级模型链、max_tokens）/ OCR 模型；
    - AgentConfig：ReAct 最大迭代数、详细日志开关、意图判定是否带历史摘要；
    - llm = LLMConfig()、agent = AgentConfig()：供全仓 import 的配置单例。

被谁使用（Grep "from config.setting import" 确认）：
    - model_llm/gateway.py（llm：build_chat_model 全部默认值）、
      model_llm/llm.py（llm：MODEL/API_KEY/BASE_URL）；
    - embedding/embedding_model.py（LLMConfig.API_KEY / MAX_RETRIES /
      BACKOFF_BASE_SECONDS）；
    - multi_agent/base_agent.py、chat_agent.py、summary_agent.py
      （llm.MODEL/API_KEY/BASE_URL/TEMPERATURE，agent.VERBOSE/MAX_ITERATIONS）；
    - service/chat_service.py（LLMConfig.SIMILARITY_THRESHOLD、
      agent.INTENT_WITH_HISTORY）。

安全约定：env/qianwen_config.env 为本地密钥文件，禁止提交版本库；
    API_KEY 为【密钥类字段】，仅允许来自本地 env/真实环境变量，禁止入库、
    禁止打日志、禁止在响应体回传。
"""
import os
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional,Final

# 项目根目录（本文件上两级），用于定位 env 文件，避免依赖运行时 cwd
BASE_DIR:Final[Path] = Path(__file__).parent.parent
# 通义千问配置文件路径：<根>/env/qianwen_config.env（本地文件，禁止入库）
ENV_PATH:Final[Path]=BASE_DIR / "env"/"qianwen_config.env"
# 导入期一次性把 qianwen_config.env 注入 os.environ；类属性随后统一 os.getenv 读取
load_dotenv(dotenv_path=ENV_PATH)


class LLMConfig:
 """
 LLM 配置类：字段在类定义期（模块导入时）从环境变量读取一次并固化。

 实例化位置：模块末尾 llm=LLMConfig() 单例；同时 embedding/embedding_model.py
 等以类属性方式（LLMConfig.xxx）直接读取——两种方式拿到的是同一份固化值。
 """
 # 主模型名，环境变量 model（qianwen_config.env，如 qwen-plus）；
 # 默认 default_model 仅为未配置时的占位，实际必须由 env 提供。
 # 读取方：model_llm/llm.py、gateway.build_chat_model、multi_agent 各 Agent
 MODEL:Final[Optional[str]] = os.getenv("model","default_model")
 # 【密钥类字段】DashScope api_key，环境变量 api_key：仅来自本地
 # env/qianwen_config.env 或真实环境变量，禁止入库/打日志/回传前端。
 # 读取方：model_llm/llm.py、gateway.build_chat_model、embedding/embedding_model.py
 API_KEY:Final[Optional[str]] = os.getenv("api_key")
 # OpenAI 兼容 API 地址，环境变量 base_url
 # （https://dashscope.aliyuncs.com/compatible-mode/v1）；
 # 读取方：model_llm/llm.py、gateway.build_chat_model、multi_agent 各 Agent
 BASE_URL:Final[Optional[str]] = os.getenv("base_url")
 # 采样温度，环境变量 Temperature（注意大写 T），默认 0.9（偏高、回答更发散）；
 # 读取方：multi_agent/base_agent.py、chat_agent.py、summary_agent.py
 TEMPERATURE:Final[Optional[float]] = float(os.getenv("Temperature","0.9"))
 # 上下文相关性余弦相似度阈值，环境变量 similarity_threshold，默认 0.8：
 # service/chat_service.py 仅当 query 与上下文相似度 ≥ 该值才拼接 RAG 上下文
 SIMILARITY_THRESHOLD:Final[Optional[float]] = float(os.getenv("similarity_threshold","0.8"))

 # LLM 网关：超时/重试/退避/降级模型链（逗号分隔，按序尝试；空串表示不降级）
 # 单次请求超时秒数，环境变量 llm_timeout_seconds，默认 30；
 # 读取方：gateway.build_chat_model(timeout=...)
 TIMEOUT_SECONDS:Final[float] = float(os.getenv("llm_timeout_seconds","30"))
 # 每个模型的最大重试次数，环境变量 llm_max_retries，默认 2；
 # 读取方：gateway.build_chat_model、embedding/embedding_model.py
 MAX_RETRIES:Final[int] = int(os.getenv("llm_max_retries","2"))
 # 指数退避基数秒，环境变量 llm_backoff_base_seconds，默认 0.5
 # （实际延迟 base*2^attempt + 0~0.25s 抖动）；读取方：gateway、embedding_model
 BACKOFF_BASE_SECONDS:Final[float] = float(os.getenv("llm_backoff_base_seconds","0.5"))
 # 降级模型链：环境变量 llm_fallback_models，逗号分隔按序尝试，默认空（不降级，
 # 示例 qwen-turbo）；读取方：gateway.build_chat_model(fallback_model_names=...)
 FALLBACK_MODELS:Final[list] = [m.strip() for m in os.getenv("llm_fallback_models","").split(",") if m.strip()]
 # 单次回答最大生成 token：默认 2000，避免详细计划/逐条作答被中途截断
 # 环境变量 llm_max_tokens；读取方：gateway.build_chat_model(max_tokens=...)
 MAX_TOKENS:Final[int] = int(os.getenv("llm_max_tokens","2000"))

 # OCR 模型（扫描件 PDF 文字识别，使用 DashScope 多模态 API），
 # 环境变量 OCR_MODEL，默认 qwen-vl-plus；注意 file_analysis/ocr_service.py
 # 当前直接 os.getenv("OCR_MODEL") 读取，本字段为同值的配置化登记/预留
 OCR_MODEL:Final[str] = os.getenv("OCR_MODEL", "qwen-vl-plus")

class AgentConfig:
 """
 Agent 行为配置类：字段在类定义期从环境变量读取，模块末尾导出 agent 单例。

 实例化位置：模块末尾 agent=AgentConfig()；读取方为 multi_agent 各 Agent
 与 service/chat_service.py。
 """
 # ReAct Agent 单轮最大工具迭代次数，环境变量 MAX_ITERATIONS，默认 10
 # （防止工具调用死循环）；读取方：multi_agent/base_agent.py、chat_agent.py、summary_agent.py
 MAX_ITERATIONS:int = int(os.getenv("MAX_ITERATIONS","10"))
 # Agent 详细日志开关，环境变量 VERBOSE（"true"/"false" 字符串），默认 True；
 # 读取方：multi_agent/base_agent.py 等赋给 agent_verbose 控制过程日志
 VERBOSE:bool = os.getenv("VERBOSE","True").lower() == "true"
 # 意图判定（Vague/Analysis）是否携带早期对话摘要：多轮承接式提问（"那这个呢"）
 # 依赖摘要消解指代；关闭后意图模型只看当前轮原文。
 # 环境变量 intent_with_history，默认 true；读取方：service/chat_service.py
 INTENT_WITH_HISTORY:bool = os.getenv("intent_with_history","true").lower() == "true"




# 全仓共享的配置单例：import 后以 llm.xxx / agent.xxx 读取（值在导入期已固化）
llm=LLMConfig()
agent=AgentConfig()

 