"""
模块名：core.metrics

作用：
    Prometheus 指标埋点与暴露：HTTP RED 指标（请求数/错误数/延迟）+ LLM 用量 + Agent 执行指标。

    - MetricsMiddleware 为纯 ASGI 中间件，覆盖流式响应的完整耗时；
    - path 标签使用路由模板（如 /history/{session_id} 归并），避免高基数；
    - LLM 指标由 core.usage.record_usage 调用 record_llm_usage 同步递增，
      多副本下每个进程只统计本进程调用，由 Prometheus 服务端聚合。

主要成员：
    - MetricsMiddleware：纯 ASGI 中间件类（请求数/延迟/在途请求埋点）；
    - record_llm_usage(model, prompt_tokens, completion_tokens)：递增 LLM 计数器；
    - record_agent_execution(agent_name, status, duration_seconds)：记录 Agent 耗时与次数；
    - render_metrics()：生成 Prometheus 文本 exposition；
    - METRICS_CONTENT_TYPE：模块级常量，/metrics 响应的 Content-Type；
    - REQUESTS / REQUEST_LATENCY / REQUESTS_IN_PROGRESS / LLM_* / AGENT_*：
      模块级全局单例，prometheus_client 指标对象，导入期注册到全局 Registry。

被谁使用：
    - control/app.py：app.add_middleware(MetricsMiddleware) 注册中间件，
      /metrics 端点调用 render_metrics() 并以 METRICS_CONTENT_TYPE 返回（受 settings.METRICS_ENABLED 开关控制）；
    - core/usage.py：record_usage 记账后调用 record_llm_usage；
    - app/application/chat/agent_service.py：Agent 执行成功/失败处调用 record_agent_execution。
"""
import time
from typing import Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# 下列指标对象均为模块级全局单例：导入本模块时构造并注册到 prometheus_client 默认 Registry，
# 进程生命周期内复用；多副本部署时各进程独立计数，由 Prometheus 服务端 scrape 后聚合。

# ---------------- HTTP 指标 ----------------
# HTTP 请求总数计数器，标签：方法/路由模板/状态码（MetricsMiddleware 埋点）
REQUESTS = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    labelnames=("method", "path", "status"),
)

# HTTP 请求处理延迟直方图，标签：方法/路由模板
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求处理延迟（秒）",
    labelnames=("method", "path"),
    # 覆盖快接口与 LLM 长请求（最长 60s 桶）
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# 在途请求数仪表盘（请求开始 +1、结束 -1，含流式响应的完整生命周期）
REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "处理中的 HTTP 请求数",
    labelnames=("method", "path"),
)

# ---------------- LLM 业务指标 ----------------
# 以下三个计数器标签均为 model；由 core.usage 经 record_llm_usage 递增
LLM_REQUESTS = Counter(
    "llm_requests_total",
    "LLM 成功调用次数",
    labelnames=("model",),
)
LLM_PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "LLM 输入 token 累计",
    labelnames=("model",),
)
LLM_COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "LLM 输出 token 累计",
    labelnames=("model",),
)

# ---------------- Agent 执行指标 ----------------
# Agent 执行耗时直方图，标签：agent 名/执行状态(success/error/retry)
AGENT_EXECUTION_DURATION = Histogram(
    "agent_execution_duration_seconds",
    "Agent 执行耗时（秒）",
    labelnames=("agent_name", "status"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
)

# Agent 执行次数计数器（含成功/失败/重试）
AGENT_EXECUTIONS = Counter(
    "agent_executions_total",
    "Agent 执行次数（含成功/失败/重试）",
    labelnames=("agent_name", "status"),
)

# 模块级常量：不进入指标统计的路径（存活探针与指标端点自身，避免噪声）
_SKIP_PATHS = {"/metrics", "/healthz"}

# 模块级常量：SPA catch-all 兜底路由的模板（control/app.py 注册）：
# 命中它时按状态细分——404 归并 unmatched（未知 API），200 归并 spa_fallback（前端路由回退），
# 避免污染真实路由模板与 unmatched 404 统计
_CATCHALL_TEMPLATE = "/{full_path:path}"


def _route_template(route, status_code: int) -> str:
    """把 Starlette 路由对象归并为低基数的路径模板字符串。

    功能：未匹配路由返回 "unmatched"；命中 SPA catch-all 时按 404→unmatched、
    非 404→spa_fallback 细分；正常路由返回其注册模板（如 /history/{session_id}）。
    被谁调用：MetricsMiddleware 在响应开始与请求结束时各调用一次。
    参数：
        route: scope["route"] 中的路由对象（404 时可能为 None），来源为 ASGI scope；
        status_code: 本次响应状态码，用于 catch-all 细分。
    返回：str，路径标签值。
    """
    path = getattr(route, "path", None)
    if not path:
        return "unmatched"
    if path == _CATCHALL_TEMPLATE:
        return "unmatched" if status_code == 404 else "spa_fallback"
    return path


def record_llm_usage(model: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """供 core.usage 调用：同步递增本进程 LLM 计数器（任何异常都不影响主链路）。

    被谁调用：core/usage.py 的 record_usage（每次 LLM 调用记账后）。
    参数：
        model: 模型名标签，来源为上游 service/LLM 网关返回或配置的模型标识；
        prompt_tokens: 本次输入 token 数，来源为 LLM 响应 usage 字段，默认 0 不累加；
        completion_tokens: 本次输出 token 数，来源同上，默认 0 不累加。
    返回：None；指标异常被吞掉（可观测性失败永不影响业务）。
    """
    try:
        LLM_REQUESTS.labels(model=model).inc()
        if prompt_tokens:
            LLM_PROMPT_TOKENS.labels(model=model).inc(prompt_tokens)
        if completion_tokens:
            LLM_COMPLETION_TOKENS.labels(model=model).inc(completion_tokens)
    except Exception:  # noqa: BLE001 - 指标失败永不影响业务
        pass


def record_agent_execution(agent_name: str, status: str, duration_seconds: float) -> None:
    """记录 Agent 执行指标：耗时 + 计数。status 为 success/error/retry。

    被谁调用：app/application/chat/agent_service.py 的 Agent 执行封装处（成功与失败分支各一次）。
    参数：
        agent_name: Agent 名称标签（如 vague/analysis/retrieval/summary），来源为编排层；
        status: 执行结果标签，"success" / "error" / "retry"；
        duration_seconds: 本次执行耗时（秒），来源为编排层计时。
    返回：None；指标异常被吞掉，不影响 agent 主链路。
    """
    try:
        AGENT_EXECUTION_DURATION.labels(agent_name=agent_name, status=status).observe(duration_seconds)
        AGENT_EXECUTIONS.labels(agent_name=agent_name, status=status).inc()
    except Exception:  # noqa: BLE001 - 指标失败永不影响业务
        pass


def render_metrics() -> bytes:
    """生成 Prometheus 文本 exposition 格式。

    功能：导出默认 Registry 中全部指标的文本快照。
    被谁调用：control/app.py 的 GET /metrics 端点（仅 settings.METRICS_ENABLED 开启时注册暴露）。
    参数：无。
    返回：bytes，Prometheus 文本格式指标数据（HTTP 响应体）。
    """
    return generate_latest()


# 模块级常量：/metrics 端点响应的 Content-Type（prometheus_client 提供，含版本与 charset）
METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


class MetricsMiddleware:
    """纯 ASGI 中间件：统计状态码、耗时、在途请求（含流式响应完整生命周期）。

    类作用：包裹下游 ASGI app，在 http.response.start 捕获真实状态码并使在途计数 +1，
    在请求结束（含流式响应发送完毕/异常）的 finally 中记录延迟、请求总数并归还在途计数；
    非 HTTP 协议与 _SKIP_PATHS 路径直接透传不埋点。
    实例化位置：control/app.py 中 ``app.add_middleware(MetricsMiddleware)``，
    由 Starlette 在应用构建时实例化（受 settings.METRICS_ENABLED 控制是否注册），非 FastAPI 按请求注入。
    """

    def __init__(self, app: Callable):
        """初始化中间件。

        参数：
            app: 下游 ASGI 可调用对象，由 Starlette 中间件栈注入；保存为 self.app 后续转发请求。
        关键属性去向：self.app 在 __call__ 中被调用以继续请求链。
        """
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        """ASGI 入口：对单个 HTTP 请求做指标埋点后透传给下游 app。

        被谁调用：ASGI 服务器（uvicorn）按请求调用。
        参数：
            scope: ASGI 连接/请求上下文（取 type/path/method，响应时 Starlette 写入 route）；
            receive: ASGI 接收信道回调，原样透传给下游；
            send: ASGI 发送信道回调，被 send_wrapper 包装以截获响应状态码。
        返回：None（通过 send/receive 与客户端交互）。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path", "")
        if raw_path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        status_code = 500
        start = time.perf_counter()
        in_progress_label = None

        async def send_wrapper(message):
            nonlocal status_code, in_progress_label
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
                # 此刻路由已匹配，scope["route"] 可用
                route = scope.get("route")
                in_progress_label = _route_template(route, status_code)
                REQUESTS_IN_PROGRESS.labels(method=method, path=in_progress_label).inc()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 路由匹配后 Starlette 向 scope 写入 route；未匹配（404）归并为 "unmatched"
            route = scope.get("route")
            path_template = _route_template(route, status_code)
            REQUEST_LATENCY.labels(method=method, path=path_template).observe(
                time.perf_counter() - start
            )
            REQUESTS.labels(method=method, path=path_template, status=str(status_code)).inc()
            if in_progress_label is not None:
                REQUESTS_IN_PROGRESS.labels(method=method, path=in_progress_label).dec()
