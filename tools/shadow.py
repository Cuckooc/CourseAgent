"""
模块名：tools.shadow

作用：
    P1 阶段 function calling 改造的影子模式（Shadow Mode）运行器。
    影子模式由 core/config.py 的 Settings.TOOL_SHADOW_MODE（环境变量
    tool_shadow_mode，默认 false 关闭）控制；开启后在不改变线上实际回答
    的前提下，旁路验证新的工具链，用于灰度比对、收集差异：
      ①取本决策层注册表工具（registry.tools_for）→ llm.bind_tools 单轮
        决策出 AIMessage.tool_calls；
      ②经 ToolDispatcher 真实执行业务函数（完整鉴权/钳制/超时/降级通道）；
      ③把新链路结果与 Agent 旧检索链路（retrieve_scoped）结果做内容指纹
        对比，只输出结构化对比报告与日志，不回灌 LLM、不经 SSE 推送，
        因而对用户答案零影响。
    影子链路任何异常都在调用方（Agent._run_shadow）与本模块内双层收口，
    永不影响主流程。P2 主链路切换后本文件删除（决策逻辑内联到
    RAGAgent/FileAgent）。

差异日志去向：仅写入本模块 logger（logger 名 "tools.shadow"），
    正常一致为 info 级；零交集/降级/错误为 warning 级，供灰度期间人工关注；
    报告 dict 同时返回给调用方（Agent 不消费，测试 tests/tools/
    test_tool_layer_p1.py 做断言）。

主要成员：
    - run_shadow：影子模式主入口（决策→执行→指纹对比→报告）；
    - _normalize_calls：把 AIMessage.tool_calls 规范化为 dispatcher 入参，
      模型未决策时按 _DEFAULT_TOOL 保守兜底；
    - _extract_query / _extract_summary：从 Agent 消息体提取问题与历史摘要；
    - _content_signatures / _shadow_signatures：旧链路/新链路结果的内容指纹；
    - _DEFAULT_TOOL：各决策层的兜底工具名；_COMPARE_PREFIX：指纹截取长度。

被谁使用：
    - multi_agent/rag_agent.py 的 RAGAgent._run_shadow()（旧链路完成后，
      settings.TOOL_SHADOW_MODE 开启且有消息时调用，owner_agent="RAGAgent"）；
    - multi_agent/file_agent.py 的 FileAgent._run_shadow()（额外要求本会话
      has_uploaded_files 时才调用，owner_agent="FileAgent"）；
    - tests/tools/test_tool_layer_p1.py：影子决策与兜底/对比逻辑单测。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from tools.dispatcher import get_tool_dispatcher
from tools.protocol import ToolContext
from tools.registry import tools_for

logger = logging.getLogger(__name__)

# 各决策层在模型未产出 tool_calls 时的保守兜底工具：
# 键为决策层名（ToolSpec.owner_agent），值为注册表中的工具名，
# 必须与 tools/business/knowledge_business.py 注册的工具名一致
_DEFAULT_TOOL = {
    "RAGAgent": "knowledge_search",       # RAGAgent 域兜底：持久知识库检索
    "FileAgent": "session_file_search",   # FileAgent 域兜底：当前会话临时库检索
}

_COMPARE_PREFIX = 80  # 内容指纹取片段前 N 个字符做新旧链路重合度对比（N=80）


def _extract_query(payload: Dict[str, Any]) -> str:
    """从 Agent 消息体提取用户问题（影子决策 prompt 的核心输入）。

    被谁调用：run_shadow() 开头，用于空 query 短路与拼影子决策 prompt。
    参数：payload 为 Agent 收到的消息体（MessageBus 消息的 message 字段，
          形如 {"query": ..., "history_summary": ...}），来源用户 query。
    返回：query 字符串；缺失/为 None 时返回空串（调用方据此跳过影子执行）。
    """
    return (payload or {}).get("query", "") or ""


def _extract_summary(payload: Dict[str, Any]) -> str:
    """从 Agent 消息体提取早期对话摘要（影子决策 prompt 的辅助上下文）。

    被谁调用：run_shadow() 拼装决策 prompt 时调用。
    参数：payload 同 _extract_query；摘要可能位于顶层 history_summary
          或嵌套在 context.history_summary（兼容两种消息结构）。
    返回：摘要文本；任一层级都缺失时返回 "无"。
    """
    if not payload:
        return "无"
    summary = payload.get("history_summary")
    if not summary:
        summary = (payload.get("context") or {}).get("history_summary")
    return summary or "无"


def _normalize_calls(decision: Any, owner_agent: str,
                     query: str) -> List[Dict[str, Any]]:
    """把 LLM 决策消息规范化为 dispatcher 入参；模型未给调用时保守兜底。

    被谁调用：run_shadow() 在 llm.bind_tools(...).invoke() 得到决策消息后调用。
    参数：
        decision：LangChain AIMessage，其 tool_calls 为 LLM function calling
                  决策结果（形如 [{"name", "args", "id"}, ...]），不可信；
        owner_agent：当前决策层名，用于选择兜底工具；
        query：用户问题，兜底调用以 {"query": query, "top_k": 3} 为参数。
    返回：[{"name": 工具名, "args": 参数字典}, ...]，直接作为
          ToolDispatcher.execute() 的 calls 入参；非 dict 的异常条目被丢弃；
          一个有效调用都没有时补一条保守兜底调用，保证影子链路总能对比。
    """
    calls = getattr(decision, "tool_calls", None) or []
    normalized = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        args = c.get("args") or {}
        if name:
            normalized.append({"name": name, "args": args})
    if not normalized:
        normalized.append({
            "name": _DEFAULT_TOOL.get(owner_agent, "knowledge_search"),
            "args": {"query": query, "top_k": 3},
        })
    return normalized


def _content_signatures(results: List[Dict[str, Any]]) -> set:
    """旧检索链路结果的内容指纹集合。

    被谁调用：run_shadow() 对 legacy_payload["results"] 提取指纹。
    参数：results 为 Agent 旧链路（rag_agent/file_agent 调 retrieve_scoped）
          产出的结果列表，元素形如 {"content": str, "metadata": dict}。
    返回：非空 content 前 _COMPARE_PREFIX 字符组成的 set，供交集比对；
          集合天然去重，只比较"是否召回过同一内容片段"，与顺序无关。
    """
    sigs = set()
    for item in results or []:
        content = (item or {}).get("content", "") or ""
        if content:
            sigs.add(content[:_COMPARE_PREFIX])
    return sigs


def _shadow_signatures(tool_results) -> set:
    """新工具链路（ToolResult 列表）的内容指纹集合。

    被谁调用：run_shadow() 对 dispatcher.execute() 返回的 ToolResult 提取指纹。
    参数：tool_results 为 ToolResult 列表，业务数据在各自的 data 字段
          （检索类为 [{"content", "metadata"}, ...]）。
    返回：与 _content_signatures 同口径的指纹 set；
          降级/失败时 data 为空集，自然贡献空指纹。
    """
    sigs = set()
    for tr in tool_results:
        for item in tr.data or []:
            content = (item or {}).get("content", "") or ""
            if content:
                sigs.add(content[:_COMPARE_PREFIX])
    return sigs


def run_shadow(owner_agent: str, llm, payload: Dict[str, Any],
               legacy_payload: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
    """执行一次影子决策链并与旧链路结果对比（影子模式主入口）。

    流程：提取 query → 取本决策层工具 → bind_tools 单轮决策 → 规范化调用
          → ToolDispatcher 真实执行 → 新旧结果指纹交集 → 组装报告并按差异
          级别写日志。全程旁路：报告不回灌 LLM、不推送 SSE、不改变实际回答。
    被谁调用：multi_agent/rag_agent.py、multi_agent/file_agent.py 各自的
              _run_shadow()（外层 try/except 已兜底）；tests/tools 单测直调。
    参数：
        owner_agent：决策层名（"RAGAgent"/"FileAgent"），决定可见工具与兜底工具；
        llm：影子决策专用模型实例（Agent._get_shadow_llm()，与主回答模型隔离）；
        payload：本轮 Agent 消息体（用户 query/history_summary）；
        legacy_payload：旧检索链路产物，取其 results（[{content, metadata}]）
                        作为对比基线，来自 Agent 已发布给 SummaryAgent 的消息；
        ctx：服务端注入的身份上下文（user_id/session_id/role/task_id）。
    返回：对比报告 dict（仅用于日志与测试断言，Agent 不消费），结构见下方
          report；query 为空或本决策层无工具时返回 None（跳过影子执行）。
    异常：本函数不主动捕获（异常由调用方 Agent._run_shadow 统一吞掉并降级为
          debug 日志），保证影子链路任何失败都不影响线上回答。
    """
    query = _extract_query(payload)
    if not query:
        # 无问题则无检索可比，直接跳过
        return None

    # 取本决策层已注册工具（含 ctx 闭包注入），跨域工具不在其中
    tools = tools_for(owner_agent, ctx)
    if not tools:
        logger.warning("shadow skipped, no tools for %s", owner_agent)
        return None

    # 影子决策 prompt：只让模型做"调哪个工具、填什么参数"的单轮决策，禁止作答
    prompt = (
        "你是工具调用决策器，只决定是否调用工具及填写参数，不要直接回答问题。\n"
        "早期对话摘要：{summary}\n"
        "用户问题：{query}"
    ).format(summary=_extract_summary(payload), query=query)

    # bind_tools 注入 JSON Schema，单轮 invoke 拿到 AIMessage.tool_calls
    decision = llm.bind_tools(tools).invoke([HumanMessage(content=prompt)])
    calls = _normalize_calls(decision, owner_agent, query)

    # 真实执行新工具链：与主链路同一 dispatcher，鉴权/钳制/超时/降级完全一致
    tool_results = get_tool_dispatcher().execute(
        calls, ctx, owner_agent=owner_agent)

    # 新旧链路内容指纹交集：量化"新链路是否召回了旧链路的同款片段"
    legacy_sigs = _content_signatures((legacy_payload or {}).get("results"))
    shadow_sigs = _shadow_signatures(tool_results)
    overlap = legacy_sigs & shadow_sigs

    # 结构化对比报告（差异日志的载体）：
    report = {
        "task_id": ctx.task_id,                 # 链路追踪 ID（MessageBus.task_id）
        "owner_agent": owner_agent,             # 决策层名
        "query": query[:100],                   # 问题前 100 字（日志防过长）
        "tool_calls": [{"name": c["name"], "args": c["args"]} for c in calls],  # 实际决策+兜底调用
        "legacy_count": len(legacy_sigs),       # 旧链路指纹数（去重后召回片段数）
        "shadow_count": len(shadow_sigs),       # 新链路指纹数
        "overlap_count": len(overlap),          # 新旧交集指纹数（0 需重点关注）
        "degraded": [r.name for r in tool_results if r.degraded],               # 发生降级的工具名
        "errors": [{"name": r.name, "error": r.error}
                   for r in tool_results if not r.success],                     # 失败工具及原因
    }

    # 差异判定：两路都有结果但零交集，或影子链路出现降级/错误 → warning 级人工关注
    if report["degraded"] or report["errors"]:
        logger.warning("[tool-shadow] %s report=%s", owner_agent, report)
    elif legacy_sigs and shadow_sigs and not overlap:
        logger.warning("[tool-shadow] %s zero-overlap report=%s",
                       owner_agent, report)
    else:
        logger.info("[tool-shadow] %s report=%s", owner_agent, report)
    return report
