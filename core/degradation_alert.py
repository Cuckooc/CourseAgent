"""
模块名：core.degradation_alert

作用：
    降级告警。agent 流水线兜底事件的结构化记录与可选推送。

    - 独立 sink（logs/degradation.log，JSON Lines），便于单独采集与告警；
    - severity=warn 仅写日志（中间环节降级，用户仍获得回答）；
    - severity=critical 额外 POST 到 webhook（LLM 整体不可用，需立即介入）；
    - 写入/推送失败不阻塞业务。

主要成员：
    - alert_degradation(...)：记录单环节降级事件（warn/critical）；
    - alert_chain_failure(...)：记录全链路失败事件（critical，必推 webhook）；
    - _post_webhook(url, payload)：内部函数，守护线程异步 POST webhook；
    - _ensure_sink()：内部函数，惰性注册 loguru 降级文件 sink；
    - _SINK_ID：模块级全局单例，已注册 sink 的 id（None=未注册/注册失败）。

被谁使用：
    - multi_agent/summary_agent.py、service/agent_service.py、service/chat_service.py
      调用 alert_degradation 记录各 agent 环节降级；
    - multi_agent/fallback.py 在全链路兜底失败时调用 alert_chain_failure。
"""
import json
import logging
import threading
import urllib.request
from typing import Optional

from loguru import logger as loguru_logger

from core.config import BASE_DIR, settings
from core.trace import get_request_id

# 模块级全局单例：降级日志 sink 编号。初始 None（未注册），
# 首次告警时由 _ensure_sink() 惰性赋值；注册失败保持 None，后续告警会重试注册。
_SINK_ID: Optional[int] = None


def _ensure_sink() -> None:
    """惰性注册降级告警文件 sink（幂等；失败时静默降级为主日志）。

    功能：首次调用时在 logs/degradation.log 注册只接收 extra.degradation=True 记录的
    sink（WARNING 起、JSON Lines、50MB 轮转、保留 30 天、zip 压缩、enqueue 异步）。
    被谁调用：本模块 alert_degradation() / alert_chain_failure() 每次写事件前调用。
    参数：无。
    返回：None；创建异常不抛出，仅向标准 logging 告警并保持 _SINK_ID=None。
    """
    global _SINK_ID
    if _SINK_ID is not None:
        return
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # 降级写入：独立 sink 与 app.log/audit.log 隔离，filter 只放行 bind(degradation=True)
        _SINK_ID = loguru_logger.add(
            log_dir / "degradation.log",
            level="WARNING",
            filter=lambda record: bool(record["extra"].get("degradation")),
            serialize=True,
            rotation="50 MB",
            retention="30 days",
            compression="zip",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("degradation sink init failed: %s", e)
        _SINK_ID = None


def _post_webhook(url: str, payload: dict) -> None:
    """后台线程 POST 告警到 webhook（不阻塞业务，5s 超时）。

    功能：起一个 daemon 线程，用 urllib 发送 JSON POST；线程随主进程退出，
    发送失败（网络/非 2xx/超时）在线程内静默吞掉，不影响业务也不重试。
    被谁调用：本模块 alert_degradation()（critical 且配置了 webhook）与
              alert_chain_failure()（配置了 webhook 即发）。
    参数：
        url: webhook 地址，来源为 settings.DEGRADE_WEBHOOK_URL（env/config.env）；
        payload: 告警字段 dict（stage/severity/query/user_id/session_id/request_id 等）。
    返回：None。
    """
    def _send():
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def alert_degradation(
    stage: str,
    severity: str,
    query: str = "",
    user_id: Optional[int] = None,
    session_id: Optional[int] = None,
    error: str = "",
    flow_context: Optional[dict] = None,
) -> None:
    """记录一条降级告警事件。

    功能：写一条结构化降级日志（logs/degradation.log），并对 query/error 截断 500 字；
    severity=critical 且配置了 DEGRADE_WEBHOOK_URL 时额外异步推送 webhook。
    被谁调用：
        - multi_agent/summary_agent.py（summary 环节降级）；
        - service/agent_service.py（各 agent 执行失败/重试耗尽）；
        - service/chat_service.py（对话链路降级，如 retrieval 失败）。

    参数：
        stage: 降级环节，"vague_agent" | "analysis_agent" | "retrieval" | "summary" | "llm_unavailable"，
               来源为上游 service/agent 传入的环节名；
        severity: 严重级别，"warn"（中间环节降级，用户仍有回答）| "critical"（LLM 整体不可用）；
        query: 用户提问原文，来源为 HTTP 对话请求（截断 500 字），默认空串；
        user_id: 当前用户 id，来源为 core.deps.get_current_user 的解析结果，可为 None；
        session_id: 当前会话 id，来源为上游 service 的会话上下文，可为 None；
        error: 异常/错误描述字符串（截断 500 字），默认空串；
        flow_context: 各 agent 输出快照 dict，用于事后排查，可为 None。
    返回：None。DEGRADE_NOTIFY_ENABLED 关闭时直接返回；日志/推送失败只告警不抛出。
    """
    # 降级开关关闭（settings.degradation_notify_enabled=false）时完全不记录
    if not settings.DEGRADE_NOTIFY_ENABLED:
        return

    _ensure_sink()

    fields = {
        "stage": stage,
        "severity": severity,
        "query": query[:500],
        "user_id": user_id,
        "session_id": session_id,
        "error": error[:500],
        "request_id": get_request_id(),
    }
    if flow_context:
        fields["flow_context"] = flow_context

    try:
        loguru_logger.bind(degradation=True, **fields).warning(
            "degradation: %s (%s)", stage, severity
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("degradation write failed: %s", e)

    # critical（LLM 整体不可用）需值班人员立即介入：异步推送 webhook，不阻塞当前请求
    if severity == "critical" and settings.DEGRADE_WEBHOOK_URL:
        _post_webhook(settings.DEGRADE_WEBHOOK_URL, fields)


def alert_chain_failure(
    query: str,
    user_id: Optional[int] = None,
    session_id: Optional[int] = None,
    chain_log: Optional[list] = None,
) -> None:
    """全链路失败告警：severity=critical，始终推送 webhook。

    功能：所有 agent 环节与兜底均失败时记录一条 chain_failure 事件（含完整链路日志），
    并在配置 webhook 时无条件异步推送。
    被谁调用：multi_agent/fallback.py（全链路兜底的最终失败分支）。

    与 alert_degradation 的区别：
    - 包含完整 chain_log（各 agent 状态转换 + 输出/错误快照）
    - 始终触发 webhook（不受 severity 判断）

    参数：
        query: 用户提问原文，来源为 HTTP 对话请求（日志内截断 500 字、消息文本截断 100 字）；
        user_id: 当前用户 id，来源为 core.deps.get_current_user，可为 None；
        session_id: 当前会话 id，来源为上游 service 会话上下文，可为 None；
        chain_log: 各 agent 状态转换与输出/错误快照的列表，来源为 multi_agent 状态机流转记录。
    返回：None。DEGRADE_NOTIFY_ENABLED 关闭时直接返回；写入/推送失败只告警不抛出。
    """
    if not settings.DEGRADE_NOTIFY_ENABLED:
        return

    _ensure_sink()

    fields = {
        "stage": "chain_failure",
        "severity": "critical",
        "query": query[:500],
        "user_id": user_id,
        "session_id": session_id,
        "request_id": get_request_id(),
        "chain_log": chain_log or [],
    }

    try:
        loguru_logger.bind(degradation=True, **fields).critical(
            "chain_failure: full pipeline failed for query=%s", query[:100]
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("chain_failure write failed: %s", e)

    if settings.DEGRADE_WEBHOOK_URL:
        _post_webhook(settings.DEGRADE_WEBHOOK_URL, fields)
