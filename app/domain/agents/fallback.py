"""
模块名：app.domain.agents.fallback

作用：
    两级兜底处理器：当关键 Agent 失败且 FailureDiagnoser 判定为缺信息，
    或技术故障重试/回退全部耗尽、循环检测命中时，由编排层调用本模块
    构造最终返回给用户的结果，并驱动状态机切换到 FALLBACK_* 兜底态。

两级降级策略（降级触发条件与最终回答去向）：
- Tier 1（handle_missing_info，缺少关键信息）：诊断器判 MISSING_INFO
  时触发——状态机转 FALLBACK_CLARIFY，跳过重试，直接把澄清提问文案
  作为 success 回答返回（流式场景包装为 delta 帧像正常回答一样展示）；
- Tier 2（handle_final_fallback，最终兜底）：技术故障重试耗尽、回退
  耗尽或 is_looping 命中时触发——状态机转 FALLBACK_ERROR，向用户返回
  友好错误文案（success=False），并经 core.degradation_alert 把全链路
  chain_log 推送开发者告警。

主要成员：
- FallbackHandler：兜底处理器类，持有状态机实例与请求标识，
  handle_missing_info / handle_final_fallback / handle_fallback 为
  编排层调用入口，_notify_developer 为内部开发者告警方法。

被谁使用（Grep 模块名结果）：
- app/application/chat/agent_service.py：run_agent()/run_agent_stream() 入口各
  FallbackHandler(sm, query, user_id, session_id) 实例化一次；
  _run_critical_agent 调 handle_missing_info/handle_final_fallback；
  run_agent 包装兜底结果调 handle_fallback；_stream_fallback 调
  handle_fallback 生成 SSE delta/error 帧。
- 依赖 app.domain.agents.state_machine.AgentState 的 FALLBACK_CLARIFY /
  FALLBACK_ERROR 两个状态。
"""
import logging
from typing import Any, Dict, Optional

from app.domain.agents.state_machine import AgentState

logger = logging.getLogger(__name__)


class FallbackHandler:
    """两级兜底处理器：构造澄清/错误响应并切换状态机兜底态。

    类作用：
        承载一次请求的兜底出口——把 FailureDiagnoser 的澄清文案或固定
        友好错误文案包装成统一 dict 返回编排层，同时把 PipelineStateMachine
        迁到 FALLBACK_CLARIFY/FALLBACK_ERROR，并在 Tier2 时把 chain_log
        推送开发者。
    继承关系：无基类（独立辅助类）。
    实例化位置：app/application/chat/agent_service.py 的 run_agent() 与
        run_agent_stream() 入口（每请求一个实例，与本次请求的 sm 绑定）。
    关键 self 属性含义与去向：
        - self.sm：本次请求 PipelineStateMachine（来源：编排层创建），
          用于 transition 兜底态、读取 chain_log 推送告警；
        - self.query/self.user_id/self.session_id：请求标识（来源：
          ChatService 请求上下文），随开发者告警上报便于复现定位。
    """

    def __init__(self, state_machine, query, user_id, session_id):
        """初始化兜底处理器。

        被谁调用：AgentService.run_agent() / run_agent_stream() 入口。
        参数：
        - state_machine：本次请求的 PipelineStateMachine（用于切换
          FALLBACK_* 态与读取 chain_log）；
        - query：用户原始问题（来源：ChatService 改写查询，告警用）；
        - user_id / session_id：JWT 用户 ID / 会话 ID（来源：请求上下文，
          告警关联用）。
        """
        self.sm = state_machine
        self.query = query
        self.user_id = user_id
        self.session_id = session_id

    def handle_missing_info(self, clarification):
        # type: (str) -> Dict[str, Any]
        """Tier 1 兜底：诊断器判定缺少关键信息，直接向用户提问澄清。

        触发条件：_run_critical_agent 中 FailureDiagnoser.diagnose 返回
                  type="MISSING_INFO"（用户问题信息不足，重试无意义）。
        被谁调用：app/application/chat/agent_service.py 的 _run_critical_agent()。
        参数：clarification——诊断器生成的一句向用户澄清的提问
              （来源：diagnose 返回的 clarification 字段，缺省"请提供更多信息"）。
        返回：{"success": True, "fallback": "clarify",
              "message": 澄清文案}；状态机先转 FALLBACK_CLARIFY；
              去向：run_agent 同步返回前端 / run_agent_stream 经
              _stream_fallback 作为 delta 帧经 SSE 展示给用户。
        """
        self.sm.transition(AgentState.FALLBACK_CLARIFY)
        logger.info("Tier 1 fallback: asking user for clarification")
        return {
            "success": True,
            "fallback": "clarify",
            "message": clarification,
        }

    def handle_final_fallback(self):
        # type: () -> Dict[str, Any]
        """Tier 2 最终兜底：技术性失败重试耗尽/回退耗尽/命中循环检测。

        触发条件（任一）：关键 Agent 判 TECHNICAL_ERROR 且 can_retry 为
                  False；循环检测 is_looping() 命中；编排层各回退支路
                  重跑仍失败且无保底回答。
        被谁调用：_run_critical_agent()、run_agent()/run_agent_stream()
                  的各兜底分支，以及 handle_fallback() 的默认分支。
        参数：无。
        返回：{"success": False, "fallback": "error", "message":
              固定友好错误文案}；状态机先转 FALLBACK_ERROR；
              去向：run_agent 包装进失败结果返回前端；流式场景经
              _stream_fallback 作为 error 帧经 SSE 返回。
        副作用：调 _notify_developer() 把全链路 chain_log 推送开发者告警。
        """
        self.sm.transition(AgentState.FALLBACK_ERROR)
        self._notify_developer()
        logger.info("Tier 2 fallback: final error with developer notification")
        return {
            "success": False,
            "fallback": "error",
            "message": "抱歉，系统暂时遇到问题，请稍后重新输入您的问题。",
        }

    def handle_fallback(self):
        # type: () -> Dict[str, Any]
        """按当前状态机状态返回对应兜底结果（不重新诊断的统一出口）。

        被谁调用：run_agent() 包装兜底结果（_wrap_fallback_result 附近）、
                  _retry_chat_for_rollback 保底文案拼装，以及
                  run_agent_stream() 的 _stream_fallback()。
        参数：无（依据 self.sm.state 决策）。
        返回：当前已是 FALLBACK_CLARIFY 态时返回默认澄清提问 dict
              （success=True）；其余情况转交 handle_final_fallback()
              走 Tier2 错误兜底（含状态切换与开发者告警）。
        """
        if self.sm.state == AgentState.FALLBACK_CLARIFY:
            return {
                "success": True,
                "fallback": "clarify",
                "message": "请问您能否提供更详细的信息？",
            }
        return self.handle_final_fallback()

    def _notify_developer(self):
        # type: () -> None
        """Tier2 兜底时把全链路日志推送给开发者（告警失败静默，不影响兜底）。

        被谁调用：handle_final_fallback()。
        参数：无（query/user_id/session_id/chain_log 均取自 self）。
        返回：None。实际推送由 core.degradation_alert.alert_chain_failure
              完成（chain_log 含 transition/output/error/rollback/
              diagnosis/relevance_check 等全部事件）；推送通道异常时仅
              记 warning，保证用户侧错误响应不受影响。
        """
        try:
            from core.degradation_alert import alert_chain_failure
            alert_chain_failure(
                query=self.query,
                user_id=self.user_id,
                session_id=self.session_id,
                chain_log=self.sm.chain_log,
            )
        except Exception:
            logger.warning("Failed to notify developer of chain failure")
