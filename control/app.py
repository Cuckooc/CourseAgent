"""
模块：app.py
作用：FastAPI 应用入口，负责生命周期钩子、中间件、全局异常处理、全部路由注册与前端 SPA 静态托管。
主要成员：
- lifespan：应用生命周期异步上下文管理器（启动后台守护任务，关闭时落盘向量索引并释放数据库连接池）；
- app：全局 FastAPI 实例（装配中间件、异常处理器、路由、静态托管）；
- biz_exception_handler / http_exception_handler / validation_exception_handler /
  unhandled_exception_handler：四类全局异常处理器，统一 {status,code,message,request_id} 响应；
- healthz / readyz / metrics：K8s 存活探针、就绪探针、Prometheus 指标端点；
- spa_index / spa_fallback / api_fallback_other_methods：前端构建产物存在时的 SPA 托管与兜底路由；
- root：无前端产物时的纯 API 根路径探活响应。
被谁使用：由 Dockerfile / docker-compose 以 `uvicorn control.app:app` 启动（docs 文档亦多处引用）；
          本文件 import 并注册 chat/login/file/history/admin/knowledge/profile/review 八个子路由模块。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from control.chat_control import chat_router
from control.login_control import login_router
from control.file_control import file_router
from control.history_control import history_router
from control.admin_control import admin_router
from control.knowledge_control import knowledge_router
from control.profile_control import profile_router
from control.review_control import review_router
from core.config import settings
from core.logging_config import setup_logging
from core.metrics import METRICS_CONTENT_TYPE, MetricsMiddleware, render_metrics
from core.responses import BizException
from core.trace import RequestIdMiddleware, get_request_id

# 统一日志（loguru 渲染 + 标准 logging 桥接）；在 app 构建前完成，
# 使模块导入期产生的日志也走同一格式
setup_logging(level=settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期钩子：启动时拉起各后台守护线程，yield 期间对外提供服务，关闭时释放资源。

    被谁调用：非内部调用，由 FastAPI 框架在应用启动/关闭时自动执行（lifespan 协议）。
    参数：
    - app：FastAPI 框架注入的当前应用实例（本函数内未使用）。
    返回：异步上下文管理器，无显式返回值。
    异常：各启动/关闭步骤均以 try/except 包裹并降级为告警日志，单点失败不阻断启动与关闭流程。
    """
    logger.info(
        "Application startup: env=%s, json_log=%s, metrics=%s",
        settings.APP_ENV,
        settings.LOG_JSON,
        settings.METRICS_ENABLED,
    )
    if settings.IS_PROD and settings.JWT_SECRET == "dev-only-secret-change-me":
        logger.warning("JWT_SECRET 使用默认值，生产环境必须通过环境变量注入随机密钥！")
    # 临时知识库已持久化到 uploads/temp/<uid>_<sid>/chroma/（会话级生命周期，
    # 删除会话时清理），启动时不再清空遗留目录，get_db/has_session 按需从磁盘恢复。
    # 仅清理确认为残留的空会话目录（无物理文件也无向量数据的目录没有恢复价值）。
    try:
        from pathlib import Path as _Path
        temp_root = _Path(settings.UPLOAD_DIR) / "temp"
        if temp_root.is_dir():
            removed = 0
            for child in temp_root.iterdir():
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
                    removed += 1
            if removed:
                logger.info("startup cleanup removed %d empty temp session dirs", removed)
    except Exception as e:
        logger.warning("startup temp cleanup failed: %s", e)
    # 用户画像：启动后台落库守护（启动时先补扫进程停机期间已满 7 天的暂存）
    try:
        from memory.profile_service import get_profile_service
        get_profile_service().start_background_flusher()
    except Exception as e:
        logger.warning("startup profile flusher failed: %s", e)
    # 长期记忆落库守护：短期记忆静默临近过期时批量转存 MySQL 后删除缓存
    try:
        from memory.long_term import get_long_term_flusher
        get_long_term_flusher().start_background_flusher()
    except Exception as e:
        logger.warning("startup long-term flusher failed: %s", e)
    # 会话关键词落库守护：Redis 累积关键词定期 UPSERT 到 MySQL 持久化
    try:
        from memory.session_keyword_service import get_session_keyword_service
        get_session_keyword_service().start_background_flusher()
    except Exception as e:
        logger.warning("startup keyword flusher failed: %s", e)
    # 定时清理守护：7 天注销到期硬删除 + 3 年软删除过期清理
    try:
        from core.purge_scheduler import get_purge_scheduler
        get_purge_scheduler().start_background_scheduler()
    except Exception as e:
        logger.warning("startup purge scheduler failed: %s", e)
    yield
    # 关闭：先把向量库 HNSW 不足 sync_threshold 的尾部索引强制落盘，
    # 避免重启后最近上传的文件在近似检索中“消失”（详见 service.vector_store）
    try:
        from service.vector_store import flush_persistent_index

        flush_persistent_index()
    except Exception as e:
        logger.warning("shutdown vector index flush failed: %s", e)
    # 关闭：释放数据库连接池
    from db.session import engine

    engine.dispose()
    logger.info("Application shutdown: db engine disposed")


# 全局 FastAPI 应用实例：uvicorn 启动目标（control.app:app），下方所有中间件、
# 异常处理器、业务路由与 SPA 兜底路由均注册到该对象
app = FastAPI(title="智能课程咨询服务", version="2.0", lifespan=lifespan)

# 请求链路追踪（P1）：request_id 写入 contextvar + 回写 X-Request-ID 响应头
app.add_middleware(RequestIdMiddleware)

# Prometheus 指标中间件（HTTP RED + LLM 用量）
if settings.METRICS_ENABLED:
    app.add_middleware(MetricsMiddleware)

# CORS：开发环境默认 *，生产环境通过 cors_origins 配置白名单
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS_LIST,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- 全局异常处理（统一响应格式） ----------------
# 以下四个处理器由 FastAPI 在对应异常抛出时自动回调，非内部调用；
# 统一返回 {status, code, message, request_id} 的 JSON 响应给前端
@app.exception_handler(BizException)
async def biz_exception_handler(request: Request, exc: BizException):
    """业务异常处理器：把主动抛出的 BizException 转成带业务 code 的统一失败响应。

    参数：
    - request：FastAPI 注入的当前请求对象（本函数内未使用）；
    - exc：业务逻辑中抛出的 BizException（含 http_status/code/message）。
    返回：JSONResponse，HTTP 状态码取 exc.http_status，响应体为统一失败结构。
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={"status": "fail", "code": exc.code, "message": exc.message, "request_id": get_request_id()},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP 异常处理器：把 FastAPI/路由抛出的 HTTPException 转成统一失败响应。

    参数：
    - request：FastAPI 注入的当前请求对象（本函数内未使用）；
    - exc：框架或路由抛出的 HTTPException（含 status_code/detail）。
    返回：JSONResponse，HTTP 状态码取 exc.status_code，code 与状态码相同。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "fail", "code": exc.status_code, "message": str(exc.detail), "request_id": get_request_id()},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """请求参数校验异常处理器：Pydantic/FastAPI 入参校验失败时返回 422 与首个错误的定位信息。

    参数：
    - request：FastAPI 注入的当前请求对象（本函数内未使用）；
    - exc：请求校验异常，exc.errors() 含出错字段位置 loc 与原因 msg。
    返回：JSONResponse（422），message 拼接首个错误的字段路径与原因，便于前端定位。
    """
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse(
        status_code=422,
        content={
            "status": "fail",
            "code": 422,
            "message": f"请求参数校验失败: {loc} {first.get('msg', '')}".strip(),
            "request_id": get_request_id(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常处理器：未被前述处理器捕获的异常一律返回 500，并记录完整异常栈。

    参数：
    - request：FastAPI 注入的当前请求对象，用于记录出错路径；
    - exc：未捕获的原始异常（仅写日志，不向前端泄露细节）。
    返回：JSONResponse（500），message 固定为“服务器内部错误”。
    """
    logger.exception("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"status": "fail", "code": 500, "message": "服务器内部错误", "request_id": get_request_id()},
    )


# 业务路由统一注册：八个 APIRouter 分别来自 control 下各 *_control 模块，
# 各路由的 prefix 在其模块内声明，此处仅挂载到全局 app
app.include_router(chat_router)
app.include_router(login_router)
app.include_router(file_router)
app.include_router(history_router)
app.include_router(admin_router)
app.include_router(knowledge_router)
app.include_router(profile_router)
app.include_router(review_router)


@app.get("/healthz")
def healthz():
    """进程存活探针（K8s liveness）。

    HTTP 方法+路径：GET /healthz；无鉴权、无限流依赖。
    被谁调用：由 HTTP 客户端（K8s kubelet / 监控探针）调用，非内部调用。
    返回：固定 JSON {status:"success", message:"healthy"}，HTTP 200 表示进程存活。
    """
    return {"status": "success", "message": "healthy"}


@app.get("/metrics")
def metrics():
    """Prometheus 指标抓取端点。

    HTTP 方法+路径：GET /metrics；无鉴权依赖，可通过配置 metrics_enabled=false 关闭。
    被谁调用：由 HTTP 客户端（Prometheus scraper）调用，非内部调用。
    返回：指标开启时返回 text/plain（Prometheus exposition 格式）；关闭时返回 404 JSON。
    """
    if not settings.METRICS_ENABLED:
        return JSONResponse(status_code=404, content={"status": "fail", "message": "metrics disabled"})
    return Response(content=render_metrics(), media_type=METRICS_CONTENT_TYPE)


@app.get("/readyz")
def readyz():
    """依赖就绪探针（K8s readiness）：检查 MySQL 连通性。

    HTTP 方法+路径：GET /readyz；无鉴权、无限流依赖。
    被谁调用：由 HTTP 客户端（K8s kubelet）调用，非内部调用。
    返回：MySQL 可连通时返回 {status:"success", message:"ready"}；
          连接失败时返回 JSONResponse（503）告知依赖不可用，K8s 据此摘除流量。
    异常：数据库异常被捕获并转换为 503，不向上抛出。
    """
    from sqlalchemy import text

    from db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "success", "message": "ready"}
    except Exception as e:
        logger.error("readyz dependency check failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={"status": "fail", "message": "依赖服务不可用"},
        )


# ---------------- 前端 SPA 静态托管（M4） ----------------
# 必须在所有显式路由（含探针）注册之后追加：FastAPI 按注册顺序匹配，
# catch-all 若先注册会抢占 /healthz 等探针。
# 构建产物目录存在时由 FastAPI 单端口托管：/ 返回 index.html，/assets 走静态资源，
# 其余 GET 路径 SPA fallback 回 index.html（React Router 前端路由接管）；
# API/探针前缀不回退，保持 JSON 404。目录不存在则退化为纯 API 服务（/ 返回探活 JSON）。
# 前端构建产物目录（来自配置 FRONTEND_DIST_DIR）：存在且含 index.html 时启用单端口 SPA 托管，
# 否则走文件末尾 else 分支退化为纯 API 服务
_dist_dir = settings.FRONTEND_DIST_DIR
if _dist_dir.is_dir() and (_dist_dir / "index.html").is_file():
    logger.info("Serving frontend SPA from %s", _dist_dir)
    assets_dir = _dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # 已被后端占用的路径前缀元组：SPA fallback 不接管这些前缀，交回 API 404/405 语义；
    # 在 spa_fallback 内用于识别 API/探针路径
    _API_PREFIXES = (
        "login/", "chat/", "history/", "file/", "knowledge/", "admin/", "profile/",
        "review/", "healthz", "readyz", "metrics", "docs", "openapi.json", "redoc",
    )

    @app.get("/", include_in_schema=False)
    async def spa_index():
        """SPA 首页路由：返回前端入口 index.html。

        HTTP 方法+路径：GET /（仅在前端构建产物存在时注册）；无鉴权依赖。
        被谁调用：由 HTTP 客户端（浏览器）调用，非内部调用。
        返回：FileResponse，文件为 dist 目录下的 index.html。
        """
        return FileResponse(_dist_dir / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request):
        """GET 路径兜底：API 前缀返回 JSON 错误，真实文件直出，其余路径回退 index.html。

        HTTP 方法+路径：GET /{full_path:path}（catch-all，注册于所有显式路由之后）。
        被谁调用：由 HTTP 客户端（浏览器导航 / XHR）调用，非内部调用。
        参数：
        - full_path：路径参数，catch-all 捕获的完整请求路径；
        - request：FastAPI 注入的请求对象，用于读取 Accept 头。
        返回：API 前缀未命中 → JSONResponse（404/405）；非 HTML 客户端的未知路径 →
              JSONResponse（404）；dist 内真实文件 → FileResponse；其余 → index.html。
        """
        # 1) API/探针路径：不返回 HTML，避免前端误解析。
        #    同路径存在其他方法的显式路由（如 POST /chat/send 的 GET 请求）→ 保留 405 语义；
        #    完全未注册的 API 路径 → JSON 404。
        if full_path.startswith(_API_PREFIXES):
            target = f"/{full_path}"
            for route in app.router.routes:
                if getattr(route, "path", None) == target and "GET" not in (
                    getattr(route, "methods", None) or set()
                ):
                    return JSONResponse(
                        status_code=405,
                        content={"status": "fail", "code": 405, "message": "请求方法不被允许"},
                    )
            return JSONResponse(
                status_code=404,
                content={"status": "fail", "code": 404, "message": "请求的资源不存在"},
            )
        # 2) 未知路径按 Accept 区分：浏览器导航（Accept 含 text/html）→ SPA index.html
        #    交由 React Router 处理；API 客户端/XHR（如 Accept: application/json）→ JSON 404
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(
                status_code=404,
                content={"status": "fail", "code": 404, "message": "请求的资源不存在"},
            )
        # 3) dist 内真实文件（favicon 等）
        candidate = (_dist_dir / full_path).resolve()
        try:
            candidate.relative_to(_dist_dir.resolve())
        except ValueError:
            candidate = None  # 路径穿越防护
        if candidate and candidate.is_file():
            return FileResponse(candidate)
        # 4) 其余路径回退 index.html，交由 React Router 处理
        return FileResponse(_dist_dir / "index.html")

    # 非 GET 方法无 SPA 语义：未知路径一律 JSON 404（显式路由已先行匹配，
    # 此兜底使 metrics 的 unmatched 归并语义保持不变）
    @app.api_route(
        "/{full_path:path}",
        methods=["POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def api_fallback_other_methods(full_path: str):
        """非 GET 方法的 catch-all 兜底：未知写操作路径一律返回 JSON 404。

        HTTP 方法+路径：POST/PUT/PATCH/DELETE /{full_path:path}（显式路由已先行匹配）。
        被谁调用：由 HTTP 客户端（web/frontend）调用，非内部调用。
        参数：
        - full_path：路径参数，catch-all 捕获的完整请求路径（本函数内未使用）。
        返回：固定 JSONResponse（404），保持 unmatched 指标归并语义不变。
        """
        return JSONResponse(
            status_code=404,
            content={"status": "fail", "code": 404, "message": "请求的资源不存在"},
        )
else:
    logger.info("Frontend dist not found (%s), running as pure API service", _dist_dir)

    @app.get("/")
    def root():
        """纯 API 模式根路径探活响应（前端构建产物不存在时注册）。

        HTTP 方法+路径：GET /；无鉴权依赖。
        被谁调用：由 HTTP 客户端（web/frontend/探针）调用，非内部调用。
        返回：固定 JSON {status:"success", message:"服务器已启动"}。
        """
        return {"status": "success", "message": "服务器已启动"}
