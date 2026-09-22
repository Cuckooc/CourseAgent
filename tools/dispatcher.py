"""
模块名：tools.dispatcher

作用：
    ToolDispatcher——工具对接层（决策层与业务层之间的唯一通道）。
    LLM function calling 决策出的 tool_name/params 经状态机
    （multi_agent/state_machine.py）→ Agent 执行侧组装成
    [{"name", "args"}, ...] 后传入本模块；分发结果（ToolResult 列表）
    回灌给 LLM 或直接组装答案，最终经 SSE 推送前端。

横切职责：
1. 预算门：单次最多 TOOL_MAX_CALLS_PER_TURN 个调用（默认 5，来源 core/config.py），
   (name, 规范化参数) 指纹去重；
2. 鉴权：工具必须存在、调用方 owner_agent 必须匹配、角色必须在 require_roles 内；
3. 参数安全：剔除 LLM 伪造的身份字段（user_id/session_id/role），过 Pydantic 钳制
   （TOOL_TOP_K_MAX=10 / TOOL_QUERY_MAX_CHARS=200，均在 protocol.py 的模型内生效）；
4. 并发执行 + 超时：ThreadPoolExecutor（最多 4 线程），future.result(timeout)
   按 ToolSpec.timeout_seconds（默认 TOOL_DEFAULT_TIMEOUT_SECONDS=20s）逐项控制；
5. 降级：T0 只读工具失败/超时返回空结果（degraded=True），不阻断回答；
   T1 写类工具失败返回 success=False 交状态机处理；
6. 追踪：每次执行通过 logger 记录（chain_log 事件由 Agent 在 P2 主链路接入时追加，
   持久化目标为 dao/chain_log.py 的 ChainLogDAO）。

主要成员：
    - ToolDispatcher：无状态分发器（execute / _run_one / _failure_result）；
    - get_tool_dispatcher()：进程级单例获取函数；
    - _FORBIDDEN_IDENTITY_FIELDS：LLM 参数中禁止出现的身份字段名单。

被谁使用：
    - tools/registry.py 的 tools_for() 闭包：StructuredTool 被直接 invoke 时；
    - tools/shadow.py 的 run_shadow()：影子链路真实执行新工具链；
    - tests/tools/test_tool_layer_p1.py：鉴权/钳制/去重/降级/超时单测。
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from core.config import settings
from tools.protocol import (
    ToolContext,
    ToolResult,
    ToolSpec,
)
from tools.registry import get_spec

logger = logging.getLogger(__name__)

# LLM 参数中禁止出现的身份字段（身份只允许来自 ToolContext；命中即剔除并告警）
_FORBIDDEN_IDENTITY_FIELDS = ("user_id", "session_id", "role")


class ToolDispatcher:
    """工具分发器：决策层与业务层之间的唯一执行通道（无状态，可全局单例复用）。

    类作用：对一批 LLM 决策出的工具调用统一执行"查表 → 跨域/角色鉴权 →
    身份字段清洗 → Pydantic 参数钳制 → 指纹去重 → 线程池并发执行 →
    逐项超时 → 按风险级别降级"流水线，业务函数之间相互隔离，
    任一调用失败不影响其他调用。
    实例化位置：不直接 new，统一经 get_tool_dispatcher() 获取进程级单例；
    调用方为 tools/registry.py 的 StructuredTool 闭包、tools/shadow.py
    与 tests/tools/test_tool_layer_p1.py。无 __init__ 形参、无实例属性。
    """

    def execute(
        self,
        calls: List[Dict[str, Any]],
        ctx: ToolContext,
        owner_agent: Optional[str] = None,
    ) -> List[ToolResult]:
        """执行一批工具调用，返回与有效调用一一对应的 ToolResult 列表。

        被谁调用：
        - tools/registry.py 的 tools_for() 闭包（StructuredTool.invoke 直调）；
        - tools/shadow.py 的 run_shadow()（影子链路，owner_agent 必传）；
        - 测试 tests/tools/test_tool_layer_p1.py。
        参数：
            calls：调用批，元素形如 {"name": 工具名, "args": 参数字典}；
                   name/args 来源 LLM function calling 的 tool_calls
                   （经 state_machine → base_agent.run 传入），不可信；
            ctx：服务端注入的身份/范围上下文（唯一可信身份来源）；
            owner_agent：调用方决策层名，非空时启用跨域防护
                         （RAGAgent 不得调用 FileAgent 域工具，反之亦然）。
        返回：ToolResult 列表，按 call_id（即原始顺序）排序；
              被去重跳过的调用不占位，鉴权/校验失败的调用以降级空结果占位；
              结果去向为回灌 LLM、直接组装答案→SSE 前端，或影子模式差异对比。
        异常：本函数吞掉全部单调用异常并转为 ToolResult，自身不抛业务异常；
              工具不存在/参数非对象/跨域/越权/参数校验失败 → 降级空结果；
              执行超时/业务异常 → 按 ToolSpec.degraded 决定空结果或 success=False。
        """
        if not calls:
            return []

        results: List[ToolResult] = []
        # 已完成校验的执行项：(spec, validated_args, call_id)
        planned: List[tuple] = []
        # 指纹集合：同一批内 (工具名 + 规范化参数) 完全相同的调用只执行一次
        seen_fingerprints = set()

        # 步骤 1：预算门——截断到单轮调用上限，逐项做鉴权与参数钳制
        for idx, raw in enumerate(calls[: settings.TOOL_MAX_CALLS_PER_TURN]):
            # call_id 以 task_id 为前缀 + 批内序号，便于日志关联与最终排序
            call_id = "{}-{}".format(ctx.task_id or "tool", idx)
            name = (raw or {}).get("name")
            raw_args = (raw or {}).get("args") or {}
            if not isinstance(raw_args, dict):
                # 1.1 参数形态必须是 JSON 对象（LLM 偶发返回字符串/数组）
                results.append(ToolResult.empty(
                    str(name or "unknown"), degraded=True,
                    error="工具参数必须是对象", call_id=call_id))
                continue

            # 1.2 tool_name 查注册表（未注册 → 降级空结果，不让未知工具触达业务层）
            spec = get_spec(name) if name else None
            if spec is None:
                results.append(ToolResult.empty(
                    str(name or "unknown"), degraded=True,
                    error="未注册的工具: {}".format(name), call_id=call_id))
                continue

            # 跨域防护：只允许调用归属本决策层的工具
            if owner_agent and spec.owner_agent != owner_agent:
                logger.warning(
                    "tool cross-owner denied: %s called by %s (owner=%s), task=%s",
                    name, owner_agent, spec.owner_agent, ctx.task_id)
                results.append(ToolResult.empty(
                    name, degraded=True, error="工具不属于当前决策层", call_id=call_id))
                continue

            # 角色鉴权：ctx.role 必须命中 ToolSpec.require_roles 白名单
            if ctx.role not in spec.require_roles:
                logger.warning(
                    "tool permission denied: name=%s role=%s task=%s",
                    name, ctx.role, ctx.task_id)
                results.append(ToolResult.empty(
                    name, degraded=True, error="当前角色无权使用该工具", call_id=call_id))
                continue

            # 身份字段注入防护：LLM 若在 args 中伪造身份字段，剔除并告警
            injected = [k for k in _FORBIDDEN_IDENTITY_FIELDS if k in raw_args]
            if injected:
                logger.warning(
                    "tool args contained identity fields and were stripped: "
                    "name=%s fields=%s task=%s", name, injected, ctx.task_id)
                raw_args = {k: v for k, v in raw_args.items()
                            if k not in _FORBIDDEN_IDENTITY_FIELDS}

            # 参数校验与钳制：Pydantic 模型完成 query 截断、top_k 收敛等容错
            try:
                validated = spec.args_model(**raw_args)
            except ValidationError as e:
                logger.warning("tool args validation failed: name=%s err=%s", name, e)
                results.append(ToolResult.empty(
                    name, degraded=True, error="工具参数校验失败", call_id=call_id))
                continue

            # 批内去重：对钳制后的参数生成稳定指纹（key 排序 + JSON 序列化）
            fingerprint = "{}:{}".format(
                name, json.dumps(validated.model_dump(), sort_keys=True,
                                 ensure_ascii=False))
            if fingerprint in seen_fingerprints:
                logger.info("duplicate tool call skipped: name=%s task=%s",
                            name, ctx.task_id)
                continue
            seen_fingerprints.add(fingerprint)
            planned.append((spec, validated, call_id))

        if not planned:
            return results

        # 步骤 2：并发执行（检索类工具相互独立），线程数最多 4，逐项超时
        max_workers = min(4, len(planned))
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="tool-exec") as pool:
            future_map = {}
            for spec, validated, call_id in planned:
                future = pool.submit(self._run_one, spec, validated, ctx)
                future_map[future] = (spec, call_id)

            # 注意：超时只能阻止调用方继续等待，Python 无法强杀线程，
            # 业务函数内部仍应自行保证可返回（向量检索客户端另有请求超时）
            for future, (spec, call_id) in future_map.items():
                try:
                    result = future.result(timeout=spec.timeout_seconds)
                except FutureTimeout:
                    logger.warning(
                        "tool timeout: name=%s timeout=%ss task=%s",
                        spec.name, spec.timeout_seconds, ctx.task_id)
                    result = self._failure_result(
                        spec, call_id, "工具执行超时")
                except Exception as e:  # noqa: BLE001 工具异常统一收口
                    logger.warning("tool execution failed: name=%s err=%s",
                                   spec.name, e, exc_info=True)
                    result = self._failure_result(spec, call_id, str(e))
                result.call_id = call_id
                results.append(result)

        # 保持与调用顺序一致（去重跳过的调用不占位）
        results.sort(key=lambda r: r.call_id)
        return results

    @staticmethod
    def _run_one(spec: ToolSpec, validated, ctx: ToolContext) -> ToolResult:
        """执行单个业务函数并补齐耗时（线程池 worker 内调用）。

        被谁调用：execute() 通过 pool.submit 提交；不在外部直接调用。
        参数：spec 为工具声明，validated 为已钳制的入参模型实例，ctx 为身份上下文。
        返回：业务函数返回的 ToolResult（latency_ms 为 0 时由本方法实测填充）。
        异常：不捕获，业务异常上抛由 execute() 的 future.result 统一收口。
        """
        start = time.perf_counter()
        result = spec.business_fn(validated, ctx)
        if result.latency_ms == 0:
            result.latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "tool executed: name=%s success=%s degraded=%s latency=%sms task=%s",
            spec.name, result.success, result.degraded, result.latency_ms, ctx.task_id)
        return result

    @staticmethod
    def _failure_result(spec: ToolSpec, call_id: str,
                        error: str) -> ToolResult:
        """按工具风险级别把执行失败转换为最终 ToolResult（失败语义分流）。

        被谁调用：execute() 在 future 超时或业务抛异常时调用。
        参数：spec 提供 degraded/risk/name 信息；call_id 追踪 ID；error 失败原因。
        返回：T0（degraded=True）→ 降级空结果，回答不中断；
              T1 写类 → success=False、data=None，交状态机重试/兜底。
        """
        if spec.degraded:
            # T0 只读：降级为空结果，等价旧链路"非关键检索失败传空值"
            return ToolResult.empty(spec.name, degraded=True, error=error,
                                    call_id=call_id)
        # T1 写类：失败上抛语义，交状态机重试/兜底
        return ToolResult(name=spec.name, success=False, data=None,
                          error=error, degraded=False, call_id=call_id)


# 进程级单例：ToolDispatcher 无状态，全进程共享一个实例即可
_dispatcher: Optional[ToolDispatcher] = None


def get_tool_dispatcher() -> ToolDispatcher:
    """获取 ToolDispatcher 进程级单例（惰性创建）。

    被谁调用：tools/registry.py 的工具执行闭包、tools/shadow.py 的 run_shadow()，
    以及 tests/tools/test_tool_layer_p1.py。
    返回：全局唯一的 ToolDispatcher 实例。
    """
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = ToolDispatcher()
    return _dispatcher
