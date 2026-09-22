"""
模块名：core.trace（请求链路追踪：request_id 全链路透传）。

作用：
    为每个 HTTP 请求分配/透传一个 request_id，使日志、审计、降级告警与
    返回给客户端的错误响应可以通过同一 ID 串联，便于用户报障时检索服务端日志。
    - 纯 ASGI 中间件：优先透传客户端 X-Request-ID 头（合法时），否则生成
      16 位十六进制短 ID，写入 contextvar 并回写响应头（含 SSE 流式响应）；
    - contextvar 在同步端点经 anyio 线程池执行时随上下文复制，端点/DAO 内可读；
    - 所有 std logging 日志由 InterceptHandler 在输出时绑定 request_id
      （见 logging_config），错误响应体也携带 request_id。

主要成员：
    - REQUEST_ID_HEADER / _MAX_HEADER_LEN：请求头名与透传值长度上限；
    - _REQUEST_ID_VAR：保存当前请求 ID 的 contextvars.ContextVar；
    - get_request_id()：读取当前请求 ID；
    - _sanitize()：校验透传头是否合法；
    - RequestIdMiddleware：ASGI 中间件类。

被谁使用（Grep）：
    - RequestIdMiddleware：control/app.py 通过 app.add_middleware 注册；
    - get_request_id：core/audit.py（审计记录）、
      core/degradation_alert.py（降级告警）、core/logging_config.py
      （日志字段绑定）、control/app.py（三类异常响应体）。
"""
import contextvars
import uuid
from typing import Optional

# 透传/回写所使用的 HTTP 头名
REQUEST_ID_HEADER = "X-Request-ID"
_MAX_HEADER_LEN = 64  # 上限防止滥用超长头；16 位 hex 足够

# 当前请求 ID 的上下文变量：无请求上下文时取默认值 "-"
_REQUEST_ID_VAR: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")


def get_request_id() -> str:
    """获取当前请求的 request_id。

    被谁调用：core/audit.py、core/degradation_alert.py、
        core/logging_config.py 及 control/app.py 的异常处理器。
    返回：str；有请求上下文时为透传或新生成的 ID，无上下文（如启动阶段、
        后台线程）时返回 "-"。去向为日志/审计/告警字段与错误响应体。
    """
    try:
        return _REQUEST_ID_VAR.get()
    except LookupError:  # pragma: no cover - default 已兜底
        return "-"


def _sanitize(value: Optional[str]) -> str:
    """校验客户端透传的 X-Request-ID 是否合法。

    功能：去空白后要求非空、长度不超过 64、全部为可见 ASCII 字符
    （防止头注入/伪造超长或控制字符污染日志与响应头）。
    被谁调用：RequestIdMiddleware.__call__。
    参数：
        value: 来自 HTTP 请求 X-Request-ID 头的原始值（可能为 None）。
    返回：str；合法时原样返回，非法时返回空串（调用方据此重新生成 ID）。
    """
    v = (value or "").strip()
    if not v or len(v) > _MAX_HEADER_LEN:
        return ""
    return v if all(33 <= ord(c) < 127 for c in v) else ""


class RequestIdMiddleware:
    """ASGI 中间件：设置 request_id contextvar 并回写响应头。

    作用：在每个 HTTP 请求入口确定 request_id（合法透传优先，否则生成），
    写入 contextvar 供整条调用链读取，并通过包装 send 把 ID 回写到
    X-Request-ID 响应头（流式响应的 http.response.start 同样生效）。
    实例化位置：不由业务代码构造，由 FastAPI/Starlette 在
        control/app.py 的 app.add_middleware(RequestIdMiddleware) 注册后，
        按 ASGI 规范自动实例化并包裹应用。
    """

    def __init__(self, app):
        # app: ASGI 应用/下一层中间件，由 Starlette 中间件栈在构造时注入；
        # 保存到 self.app，请求到达时调用 self.app(scope, receive, send) 放行
        self.app = app

    async def __call__(self, scope, receive, send):
        # 非 HTTP 生命周期（如 lifespan）不经追踪，直接放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 从原始请求头中查找客户端透传的 X-Request-ID（头名在 ASGI 中为小写字节串）
        incoming = None
        for key, val in scope.get("headers") or []:
            if key == b"x-request-id":
                incoming = val.decode("latin-1", "ignore")
                break
        # 合法透传则沿用（跨网关/全链路追踪场景），否则生成 16 位 hex 短 ID
        rid = _sanitize(incoming) or uuid.uuid4().hex[:16]
        token = _REQUEST_ID_VAR.set(rid)

        async def send_wrapper(message):
            # 在响应起始报文上追加 x-request-id 头；后续 body 报文原样透传
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", rid.encode("latin-1")))
            await send(message)

        try:
            # 用包装后的 send 调用下游，保证异常/流式路径也能回写响应头
            await self.app(scope, receive, send_wrapper)
        finally:
            # 请求结束复位 contextvar，避免上下文复用时串号
            _REQUEST_ID_VAR.reset(token)
