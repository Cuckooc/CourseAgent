"""
模块名：tools.registry

作用：
    工具注册中心（对接层）。采用"业务包导入即自注册"机制：业务模块在导入时
    显式调用 register_tool(ToolSpec(...)) 把工具登记进进程级全局字典 _REGISTRY；
    本模块再按决策层（owner_agent）生成 bind_tools 可用的 LangChain 工具。

注册机制：
    - 显式注册（非装饰器）：tools/business/knowledge_business.py 在模块尾部
      直接调用 register_tool(ToolSpec(...))；
    - 惰性加载：首次查询时由 _ensure_business_loaded() 导入 tools.business 包
      （tools/business/__init__.py 再导入各业务模块），导入动作即触发注册，
      以此规避 registry ↔ business ↔ protocol 的循环导入；
    - 重复注册同名工具直接抛 ValueError，防止静默覆盖。

当前工具名清单（以 name 为键）：
    - knowledge_search       （owner_agent="RAGAgent"，持久知识库检索）
    - session_file_search    （owner_agent="FileAgent"，当前会话临时库检索）

一份 ToolSpec 同时服务两件事：
1. args_model 自动转 JSON Schema，经 StructuredTool 提供给 llm.bind_tools；
2. name -> business_fn 供 ToolDispatcher 执行分发（dispatcher 通过 get_spec 查表）。

工具按请求（ToolContext）生成：用闭包把 ctx 注入执行通道，
LLM 无法通过参数伪造 user_id / session_id。

被谁使用：
    - tools/dispatcher.py：get_spec() 按 tool_name 查 ToolSpec；
    - tools/shadow.py：tools_for() 取本决策层工具做 bind_tools 影子决策；
    - tests/tools/test_tool_layer_p1.py：注册表隔离与工具层单测。
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List

from langchain_core.tools import StructuredTool

from tools.protocol import ToolContext, ToolSpec

logger = logging.getLogger(__name__)

# 进程级工具注册表：键为工具名（ToolSpec.name），值为完整 ToolSpec 声明
_REGISTRY: Dict[str, ToolSpec] = {}
# 业务包是否已完成首次导入（自注册只触发一次）
_business_loaded = False


def _ensure_business_loaded() -> None:
    """惰性导入业务包，触发 register_tool 显式自注册（避免循环导入）。

    被谁调用：get_spec/all_specs/specs_for/tools_for 四个查询函数的入口处，
    保证任何查询之前 tools.business 已导入、全部 ToolSpec 已登记。
    异常：业务模块导入失败会原样上抛（导入期错误属于配置/代码错误，不吞）。
    """
    global _business_loaded
    if _business_loaded:
        return
    from tools import business  # noqa: F401  导入即注册

    _business_loaded = True


def register_tool(spec: ToolSpec) -> ToolSpec:
    """注册一个工具（业务模块导入时调用）。重复注册直接报错，防止静默覆盖。

    被谁调用：tools/business/knowledge_business.py 模块尾部两次
    （knowledge_search、session_file_search）；未来新业务模块同法调用。
    参数：spec 为待登记的工具声明，先经 ToolSpec.validate() 强校验。
    返回：原 spec（便于链式/模块级表达式使用）。
    异常：spec 非法或同名工具已存在时抛 ValueError。
    """
    spec.validate()
    if spec.name in _REGISTRY:
        raise ValueError("工具重复注册: %s" % spec.name)
    _REGISTRY[spec.name] = spec
    logger.debug("tool registered: %s (owner=%s, risk=%s)",
                 spec.name, spec.owner_agent, spec.risk)
    return spec


def get_spec(name: str) -> ToolSpec:
    """按工具名查询 ToolSpec；未注册时返回 None（不抛错，由 dispatcher 兜底）。

    被谁调用：tools/dispatcher.py 的 ToolDispatcher.execute() 分发第一步查表。
    参数：name 为 LLM function calling 决策出的 tool_name。
    返回：命中的 ToolSpec；查无此工具时返回 None。
    """
    _ensure_business_loaded()
    return _REGISTRY.get(name)


def all_specs() -> List[ToolSpec]:
    """返回注册表中全部 ToolSpec（列表副本，调用方修改不影响注册表）。

    被谁调用：目前供管理/调试与测试使用；主链路按 owner_agent 过滤请用 specs_for。
    """
    _ensure_business_loaded()
    return list(_REGISTRY.values())


def specs_for(owner_agent: str) -> List[ToolSpec]:
    """返回归属某决策层的全部工具。

    被谁调用：tools_for() 生成 StructuredTool 列表前调用；测试中也直接断言。
    参数：owner_agent 为决策层名（如 "RAGAgent"/"FileAgent"），
          来源发起 function calling 决策的 Agent 身份。
    返回：owner_agent 字段精确匹配的 ToolSpec 列表；无匹配返回空列表。
    """
    _ensure_business_loaded()
    return [s for s in _REGISTRY.values() if s.owner_agent == owner_agent]


def tools_for(owner_agent: str, ctx: ToolContext) -> List[StructuredTool]:
    """生成某决策层在本次请求中可绑定的 LangChain 工具列表。

    被谁调用：
    - tools/shadow.py 的 run_shadow()：取工具喂给 llm.bind_tools 做影子决策；
    - P2 主链路接入后将由 RAGAgent/FileAgent 在每轮决策前调用；
    - tests/tools/test_tool_layer_p1.py：验证按决策层隔离。
    参数：
        owner_agent：决策层名，决定可见工具集合（跨域工具不出现在 Schema 中）；
        ctx：本次请求的服务端上下文，被闭包捕获注入执行通道，
             LLM 无法经工具参数伪造 user_id/session_id/role。
    返回：StructuredTool 列表；该决策层无工具时返回空列表并告警。

    注意：当前架构为单轮 function calling，Agent 只读取 AIMessage.tool_calls
    做结构化决策，真正的执行由 ToolDispatcher 完成；此处的 func 闭包保留
    标准工具调用语义（直接 invoke 工具时也会走同一套鉴权/钳制/降级通道）。
    """
    specs = specs_for(owner_agent)
    if not specs:
        logger.warning("no tools registered for owner_agent=%s", owner_agent)

    tools: List[StructuredTool] = []
    for spec in specs:
        def _make_func(s: ToolSpec):
            def _run(**kwargs) -> str:
                # StructuredTool 的标准执行入口：LLM 经 invoke 直调时走此闭包
                # 惰性导入避免 registry <-> dispatcher 循环依赖
                from tools.dispatcher import get_tool_dispatcher

                # 单工具调用包装成统一的一批调用格式，仍走完整鉴权/钳制/超时/降级通道
                results = get_tool_dispatcher().execute(
                    [{"name": s.name, "args": kwargs}], ctx, owner_agent=owner_agent,
                )
                if not results:
                    return json.dumps({"results": []}, ensure_ascii=False)
                r = results[0]
                # 返回 JSON 字符串：成功/空结果/错误三态平铺，供 LLM 或上游解析
                return json.dumps(
                    {"success": r.success, "results": r.data or [], "error": r.error},
                    ensure_ascii=False,
                )

            return _run

        # args_schema 即工具入参模型，LangChain 据此自动生成 bind_tools 的 JSON Schema
        tools.append(
            StructuredTool.from_function(
                make_function(_make_func(spec), spec.name),
                name=spec.name,
                description=spec.description,
                args_schema=spec.args_model,
            )
        )
    return tools


def make_function(func, name: str):
    """赋予闭包稳定的函数名（StructuredTool 会取 __name__ 作为工具名兜底）。

    被谁调用：tools_for() 为每个 spec 生成执行闭包后调用。
    参数：func 为捕获了 spec/ctx/owner_agent 的 _run 闭包；name 为工具名。
    返回：设置好 __name__ 的同一函数对象（原地修改后返回）。
    """
    func.__name__ = name
    return func
