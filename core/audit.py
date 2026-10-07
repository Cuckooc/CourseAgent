"""
模块名：core.audit

作用：
    审计日志。安全事件与管理员操作的独立追加式记录（logs/audit.log，JSON Lines）。

    - append-only：审计流只写不查（查询/告警由日志采集侧承担），独立文件与业务
      日志隔离，便于单独采集、归档与留存（保留 90 天，严于业务日志的 14 天）；
    - 每条记录含 actor（谁）/action（做什么）/target（对谁）/result/request_id，
      request_id 可与 app.log 双向关联还原完整请求上下文；
    - 写入失败不阻塞业务（降级进 stderr 主日志）。

主要成员：
    - audit(action, actor, target, result, **detail)：写入一条审计事件；
    - _ensure_sink()：内部函数，惰性注册 loguru 审计文件 sink；
    - _SINK_ID：模块级全局单例，保存已注册 sink 的 id（None 表示尚未注册/注册失败）。

被谁使用：
    - control/login_control.py：记录注册、登录成功/失败事件；
    - control/chat_control.py：记录对话相关的安全事件；
    - control/admin_control.py：记录管理员操作（如用户降权/停用等）。
"""
import logging
from typing import Optional

from loguru import logger as loguru_logger

from core.config import BASE_DIR
from core.trace import get_request_id

# 模块级全局单例：loguru 审计 sink 的编号。
# 初始为 None（未注册）；首次调用 audit() 时由 _ensure_sink() 惰性赋值。
# 注册失败保持 None，后续每次 audit() 都会重试注册。
_SINK_ID: Optional[int] = None


def _ensure_sink() -> None:
    """惰性注册审计文件 sink（幂等；失败时静默降级为主日志）。

    功能：首次调用时在 logs/audit.log 上注册一个只接收 extra.audit=True 记录的
    loguru sink（JSON Lines、50MB 轮转、保留 90 天、zip 压缩、enqueue 异步落盘）；
    已注册则直接返回。
    被谁调用：本模块 audit() 每次写事件前调用。
    参数：无。
    返回：None。sink 创建失败时不抛异常，仅向标准 logging 告警并保持 _SINK_ID=None
    （审计降级：事件仍可能进入主日志，业务不被阻塞）。
    """
    global _SINK_ID
    if _SINK_ID is not None:
        return
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # 审计写入：独立 sink 与业务 app.log 物理隔离；filter 只放行 bind(audit=True) 的记录
        _SINK_ID = loguru_logger.add(
            log_dir / "audit.log",
            level="INFO",
            filter=lambda record: bool(record["extra"].get("audit")),
            serialize=True,  # JSON Lines，采集侧直接解析
            rotation="50 MB",
            retention="90 days",
            compression="zip",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
    except Exception as e:  # noqa: BLE001 - 审计不可用不阻塞业务
        logging.getLogger(__name__).warning("audit sink init failed: %s", e)
        _SINK_ID = None


def audit(
    action: str,
    actor: Optional[dict] = None,
    target=None,
    result: str = "success",
    **detail,
) -> None:
    """记录一条审计事件。

    功能：组装 actor/action/target/result/request_id 字段，以 bind(audit=True) 写入审计 sink。
    被谁调用（Grep audit( 结果）：
        - control/login_control.py：register（user_registered）、login_by_account
          （登录成功 / login_failed）；
        - control/chat_control.py：对话端点的安全事件；
        - control/admin_control.py：管理员操作（如 deactivate_user 等）。

    参数：
        action: 事件名（字符串常量，调用方约定，如 "login_failed"）；
        actor: 操作者信息 dict，通常来自 app.auth.guards.get_current_user 的返回值
               {"user_id","user_name","role"}；登录/匿名事件可只传 {"user_name": ...}；
        target: 操作对象（用户 id、用户名、文件名等，可为 dict 或标量），来源为 HTTP 请求参数；
        result: 事件结果，默认 "success"，失败场景调用方传 "fail"；
        **detail: 额外上下文字段平铺进记录（如 changed_role、reason）。
    返回：
        None。仅追加写入 logs/audit.log；写入失败只告警不抛出，不影响业务主流程。
    """
    _ensure_sink()
    fields = {
        "actor": actor or {},
        "action": action,
        "target": target,
        "result": result,
        "request_id": get_request_id(),
    }
    fields.update(detail)
    try:
        loguru_logger.bind(audit=True, **fields).info(action)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("audit write failed(%s): %s", e, fields)
