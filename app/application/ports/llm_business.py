"""
模块名：app.application.ports.llm_business

作用：
    业务提示词链（llm_business 各 LLM 模板类）的运行时装配点（服务定位器）。
    application/domain 层的上下文改写、标题生成、意图判定、分发分析、
    最终回答、检索汇总等提示词模板统一经本模块获取，不直接 import
    app.infrastructure.llm.llm_business（分层守卫 RULES 禁止业务层依赖
    基础设施）；具体实现由组合根 app/api/deps.py 在应用启动时注册
    （Port↔Adapter 装配）。

主要成员（register_llm_business_ops 由组合根启动时调用一次，传
    llm_business 模块对象）：
    - build_context_rewrite_prompt(context, query)：上下文改写提示词
      （ContextKey().generate(context, query)）；
    - build_analysis_prompt()：分发判定提示词（AnalysisLLM().generate()）；
    - build_chat_prompt()：最终回答提示词（ChatLLM().generate()）；
    - build_predict_prompt()：意图模糊判定提示词（PredictLLM().generate()）；
    - get_information_llm()：检索汇总链（InformationLLM 实例，
      调用方用 .generate() 取模板、.llm 取聊天模型）；
    - get_title_llm_cls()：标题生成基类（TitleLLM 类对象，
      供 application.chat.title.Title 继承）。

注意：
    get_title_llm_cls() 在 title.py 类定义时（模块导入期）调用，因此
    组合根 deps.py 必须先完成本模块注册，再 import 依赖 title 的
    chat_service（deps.py 中注册块位于 app 服务导入之前的顺序不得调整）。

被谁使用：
    - 调用方：app/application/chat/{context,title}.py、
      app/domain/agents/{analysis_agent,chat_agent,summary_agent,
      vague_agent}.py；
    - 装配方：app/api/deps.py（import 时执行 register_llm_business_ops）。
"""
from typing import Any

__all__ = [
    "register_llm_business_ops",
    "build_context_rewrite_prompt",
    "build_analysis_prompt",
    "build_chat_prompt",
    "build_predict_prompt",
    "get_information_llm",
    "get_title_llm_cls",
]


# 已注册的实现模块（组合根装配前为 None；需具备 ContextKey/AnalysisLLM/
# ChatLLM/PredictLLM/InformationLLM/TitleLLM 属性）
_llm_business_ops: Any = None


def register_llm_business_ops(ops: Any) -> None:
    """注册 llm_business 模块实现（组合根启动时调用一次，传模块对象）。"""
    global _llm_business_ops
    _llm_business_ops = ops


def _ops() -> Any:
    """取已注册的 llm_business 模块；未装配属启动期配置错误。"""
    if _llm_business_ops is None:
        raise RuntimeError(
            "业务提示词链未装配：组合根 app/api/deps.py 未注册实现"
            "（register_llm_business_ops）"
        )
    return _llm_business_ops


def build_context_rewrite_prompt(context: Any, query: Any) -> Any:
    """上下文改写提示词（语义同 llm_business.ContextKey().generate(context, query)）。"""
    return _ops().ContextKey().generate(context, query)


def build_analysis_prompt() -> Any:
    """分发判定提示词（语义同 llm_business.AnalysisLLM().generate()）。"""
    return _ops().AnalysisLLM().generate()


def build_chat_prompt() -> Any:
    """最终回答提示词（语义同 llm_business.ChatLLM().generate()）。"""
    return _ops().ChatLLM().generate()


def build_predict_prompt() -> Any:
    """意图模糊判定提示词（语义同 llm_business.PredictLLM().generate()）。"""
    return _ops().PredictLLM().generate()


def get_information_llm() -> Any:
    """检索汇总链实例（语义同 llm_business.InformationLLM()；调用方用 .generate()/.llm）。"""
    return _ops().InformationLLM()


def get_title_llm_cls() -> Any:
    """标题生成基类（语义同 llm_business.TitleLLM 类对象；供 Title 继承）。"""
    return _ops().TitleLLM
