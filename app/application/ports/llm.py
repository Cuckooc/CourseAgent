"""
模块名：app.application.ports.llm

作用：
    LLM 网关的运行时装配点（服务定位器）。application/domain 层的 Agent 与
    业务服务统一经本模块获取聊天模型与契约异常，不直接 import
    app.infrastructure.llm.gateway（分层守卫 RULES 禁止业务层依赖基础设施）；
    具体实现由组合根 app/api/deps.py 在应用启动时注册（Port↔Adapter 装配）。

主要成员：
    - LLMUnavailableError：主模型与全部降级模型均不可用时由实现抛出的契约
      异常，定义于 core.exceptions（分层守卫禁止 infrastructure→application，
      契约类型只能放横切层 core），本模块 re-export 供业务层单点导入；
    - register_chat_model_builder(builder)：组合根注册实现工厂（启动时一次）；
      测试可注入假实现（fake builder）免调真实 LLM；
    - build_chat_model(**overrides)：业务层统一调用入口，签名与
      infrastructure.llm.gateway.build_chat_model 完全一致，返回
      LangChain ChatOpenAI 兼容模型（带重试 + 模型降级）。

被谁使用：
    - 调用方：app/domain/agents/*（base_agent/chat_agent/rag_agent/file_agent/
      summary_agent/verifier/failure_diagnoser/analysis_agent/vague_agent）、
      app/application/chat/{agent_service,chat_service,context}.py、
      app/application/files/preference_service.py、
      app/domain/memory/{context_memory,profile_service}.py；
    - 装配方：app/api/deps.py（import 时执行 register_chat_model_builder）。
"""
from typing import Any, Callable, Optional

from core.exceptions import LLMUnavailableError

__all__ = ["LLMUnavailableError", "build_chat_model", "register_chat_model_builder"]


# 已注册的实现工厂（组合根装配前为 None）；签名同 gateway.build_chat_model
_builder: Optional[Callable[..., Any]] = None


def register_chat_model_builder(builder: Callable[..., Any]) -> None:
    """注册 LLM 网关实现工厂（组合根在启动时调用一次；测试可注入假实现）。

    参数：builder —— 与 infrastructure.llm.gateway.build_chat_model 同签名的
          工厂函数（**overrides -> ChatOpenAI 兼容模型）。
    """
    global _builder
    _builder = builder


def build_chat_model(**overrides: Any) -> Any:
    """构建带重试/降级的聊天模型（签名与行为同 infrastructure 实现）。

    参数：**overrides —— 调用点覆盖项（temperature/timeout 等），原样透传实现。
    返回：LangChain ChatOpenAI 兼容模型（LLMGateway 实例）。
    异常：RuntimeError —— 组合根尚未装配（属启动期配置错误，不应在运行期出现）；
          LLMUnavailableError —— 主备模型全链不可用（由实现抛出）。
    """
    if _builder is None:
        raise RuntimeError(
            "LLM 网关未装配：组合根 app/api/deps.py 未注册实现"
            "（register_chat_model_builder）"
        )
    return _builder(**overrides)
