"""
模块名：core.logging_config

作用：
    日志统一配置：标准 logging → loguru 桥接。

    全仓模块均使用 logging.getLogger(__name__)，无需逐个改造：
    - InterceptHandler 把标准库日志记录转发给 loguru 统一渲染；
    - dev 环境彩色文本，prod 环境 JSON 序列化（便于 ELK/Loki 采集）；
    - uvicorn / fastapi / sqlalchemy 等第三方 logger 一并接管；
    - 每条日志自动绑定 core.trace 的 request_id，文件 sink 为 logs/app.log
      （50MB 轮转、保留 14 天、zip 压缩）。

主要成员：
    - InterceptHandler：logging.Handler 实现类，把标准日志记录转发给 loguru；
    - setup_logging(level, json_logs)：初始化全局 sink 与桥接（幂等可重复调用）；
    - _INTERCEPTED_LOGGERS：模块级常量，需接管 handler 的第三方 logger 名单；
    - _TEXT_FORMAT / _FILE_ROTATION / _FILE_RETENTION：模块级常量，文本格式与文件轮转策略。

被谁使用：
    - control/app.py：应用启动入口调用 setup_logging(level=settings.LOG_LEVEL,
      json_logs=settings.LOG_JSON)。InterceptHandler 不被业务代码直接实例化，
      仅在 setup_logging 内创建（root logger 与各第三方 logger 各挂一个）。
"""
import logging
import sys
from pathlib import Path

from loguru import logger as loguru_logger

from core.trace import get_request_id


class InterceptHandler(logging.Handler):
    """标准 logging.Handler：把日志记录桥接到 loguru。

    类作用：拦截所有走标准 logging 链路（含 uvicorn/sqlalchemy 等第三方库）的日志记录，
    转换级别、回溯真实调用帧后交由 loguru 渲染，并绑定当前请求 request_id。
    实例化位置：不通过 FastAPI 注入，由 setup_logging() 创建：
    一个挂到 logging.root（basicConfig force=True），另替换 _INTERCEPTED_LOGGERS
    名单内每个第三方 logger 的 handler。
    """

    def emit(self, record: logging.LogRecord) -> None:
        """处理单条标准日志记录并转发给 loguru。

        被谁调用：Python logging 框架在每条记录到达本 handler 时回调（业务代码不直接调用）。
        参数：
            record: logging.LogRecord，待转发的日志记录（含级别名/消息/异常栈）。
        返回：None。
        """
        try:
            level: object = loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 回溯到真正发起日志的调用帧，使 loguru 输出正确的文件名/行号
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # 每条日志绑定当前请求的 request_id（无请求上下文时为 "-"）
        loguru_logger.opt(depth=depth, exception=record.exc_info).bind(
            request_id=get_request_id()
        ).log(level, record.getMessage())


# 模块级常量：需接管的第三方 logger 名单。
# uvicorn 自带 handler，必须替换并设 propagate=False，否则同一日志重复输出两行。
_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "sqlalchemy.engine",
)

# 模块级常量：dev 环境控制台/文本文件的 loguru 格式（含 request_id 列）。
_TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[request_id]}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

# 模块级常量：文件轮转策略（P1 数据保留制度化）：单文件 50MB、保留 14 天、zip 压缩归档
_FILE_ROTATION = "50 MB"
_FILE_RETENTION = "14 days"


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """初始化全局日志：loguru sink + 标准 logging 桥接（幂等）。

    功能：先 loguru.remove() 清空默认 sink，再按 json_logs 选择 JSON（prod）或彩色文本
    （dev）分别注册 stderr 与 logs/app.log 两个 sink；随后用 InterceptHandler
    接管 root logger 与 _INTERCEPTED_LOGGERS 中的第三方 logger。
    被谁调用：control/app.py 模块导入期（应用启动）调用一次，
              入参来自 settings.LOG_LEVEL / settings.LOG_JSON。
    参数：
        level: 日志级别字符串（如 "INFO"），来源为 settings.LOG_LEVEL（env: log_level）；
        json_logs: 是否 JSON 序列化输出，来源为 settings.LOG_JSON（prod=True）。
    返回：None。
    """
    # remove() 保证重复调用/uvicorn reload 下不会叠加重复 sink
    loguru_logger.remove()
    loguru_logger.configure(extra={"request_id": "-"})
    if json_logs:
        loguru_logger.add(
            sys.stderr,
            level=level,
            serialize=True,
            backtrace=False,
            diagnose=False,  # prod 不打印变量值，避免敏感信息泄露
            enqueue=True,   # 多进程/线程安全
        )
        # 运行时数据目录 logs/app.log：容器内由 compose 挂载 ./logs 卷持久化
        loguru_logger.add(
            Path("logs") / "app.log",
            level=level,
            serialize=True,
            backtrace=False,
            diagnose=False,
            enqueue=True,
            rotation=_FILE_ROTATION,
            retention=_FILE_RETENTION,
            compression="zip",
        )
    else:
        loguru_logger.add(
            sys.stderr,
            level=level,
            format=_TEXT_FORMAT,
            backtrace=True,
            diagnose=False,
            enqueue=True,
        )
        loguru_logger.add(
            Path("logs") / "app.log",
            level=level,
            format=_TEXT_FORMAT,
            backtrace=True,
            diagnose=False,
            enqueue=True,
            rotation=_FILE_ROTATION,
            retention=_FILE_RETENTION,
            compression="zip",
        )

    # force=True 清空 root 上已有 handler（uvicorn 配置过的），避免重复行
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    for name in _INTERCEPTED_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False
