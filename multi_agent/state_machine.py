"""
模块名：multi_agent.state_machine

作用：
多 Agent 流水线状态机。

管理各 Agent 的运转状态、重试计数、全链路日志与回退计数。
编排层（AgentService）通过状态机驱动流水线，替代旧的顺序 try/except。

主要成员：
- AgentState：流水线全部状态枚举（正常节点态 / 重试态 / 回退态 / 兜底态 / 终态）；
- MAX_RETRIES：各 Agent 及回退的最大重试次数（来源 core.config.settings）；
- RETRYING_STATES / RESUME_STATES：Agent 名 -> 重试态 / 恢复态的映射表；
- CRITICAL_AGENTS / NON_CRITICAL_AGENTS：关键/非关键 Agent 名集合；
- PipelineStateMachine：状态机主体，维护状态、计数、链路日志与循环检测。

被谁使用（Grep 模块名结果）：
- service/agent_service.py：run_agent() / run_agent_stream() 入口各
  PipelineStateMachine(query, user_id, session_id) 创建一次；
  _run_critical_agent / _run_non_critical_agent /
  _run_summary_with_relevance / _run_retrieval / _retry_chat_for_rollback
  等编排方法全程调用其 transition/record_*/can_retry/is_looping；
- multi_agent/fallback.py：import AgentState 用于切换 FALLBACK_* 兜底态；
- tests/phase/test_phase8_agent_eval.py：测试导入 PipelineStateMachine、MAX_RETRIES。

状态转移图（文字版；每步输入来源与输出去向）：
    IDLE
      │  入口 run_agent()/run_agent_stream()，输入：用户 query + user_id/session_id
      ▼
    VAGUE_DETECTING ──成功且 success=True(意图模糊)──► ChatAgent.handle ──► COMPLETED
      │ （VagueAgent 经 MessageBus 把消息直接发给 ChatAgent）
      │  成功且意图明确
      ▼
    ANALYZING ──verifier 判"不一致"且 can_rollback──► ROLLING_BACK ──► ANALYZING（重跑一次）
      │  （AnalysisAgent 输出 need_RAGAgent/need_FileAgent）
      ▼
    RETRIEVING（RAGAgent / FileAgent 并行可选；非关键，失败传空结果）
      │  检索结果经总线 -> SummaryAgent
      ▼
    SUMMARIZING ──相关性评分 < 阈值且轮次未满──► ROLLING_BACK ──► RETRIEVING（重新检索后再汇总）
      │  汇总 answer 经总线 -> ChatAgent
      ▼
    GENERATING（ChatAgent 生成最终回答）
      │  verifier 判"不一致"且 can_rollback ──► ROLLING_BACK
      │       └─► 重跑 分析→检索→汇总→生成（_retry_chat_for_rollback）
      ▼
    COMPLETED

失败/重试/兜底支路：
    任一关键 Agent(vague/analysis) 异常
      ├─ FailureDiagnoser 判 MISSING_INFO ──► FALLBACK_CLARIFY（Tier1：向用户澄清，跳过重试）
      ├─ 判 TECHNICAL_ERROR 且 can_retry ──► RETRYING_*（记日志）──► 恢复态 RESUME_STATES 重跑
      └─ 重试耗尽 ──► FALLBACK_ERROR（Tier2：友好报错 + 推送开发者链路日志）──► FAILED
    非关键 Agent(retrieval_rag/retrieval_file/summary) 异常：仅按上限重试，
      耗尽后用降级值继续，不阻断主链路。
    循环检测：record_step 累计 total_steps 并维护动作指纹滑动窗口；
      is_looping() 命中步数上限或重复模式时，立即终止并走最终兜底。
"""
import datetime
import logging
from enum import Enum
from typing import Any, Dict, Optional

from core.config import settings
from core.prompt_registry import get_prompt_version

logger = logging.getLogger(__name__)


class AgentState(Enum):
    """流水线状态枚举：状态机所有合法节点。

    分组：
    - 正常节点态：IDLE 及 VAGUE_DETECTING/ANALYZING/RETRIEVING/
      SUMMARIZING/GENERATING/COMPLETED，对应五个 Agent 执行阶段；
    - 重试态：RETRYING_*，关键 Agent 技术故障后、重跑前的过渡记录态；
    - 回退态：ROLLING_BACK，验证不一致/相关性不足时回滚重跑上游链；
    - 兜底态：FALLBACK_CLARIFY（Tier1 缺信息追问）/FALLBACK_ERROR（Tier2 终态报错）；
    - FAILED：失败终态（枚举保留，当前由 FALLBACK_ERROR 承载）。
    """
    IDLE = "idle"                                   # 初始空闲态（状态机创建后）
    VAGUE_DETECTING = "vague_detecting"             # 阶段1：VagueAgent 意图模糊判定
    ANALYZING = "analyzing"                         # 阶段2：AnalysisAgent 下游分发判定
    RETRIEVING = "retrieving"                       # 阶段3：RAGAgent/FileAgent 知识检索
    SUMMARIZING = "summarizing"                     # 阶段4：SummaryAgent 检索结果汇总
    GENERATING = "generating"                       # 阶段5：ChatAgent 最终回答生成
    VERIFYING = "verifying"                         # 校验态（枚举保留；一致性校验现由编排层内联完成）
    COMPLETED = "completed"                         # 成功终态

    RETRYING_VAGUE = "retrying_vague"               # vague 重试过渡态（仅写链路日志）
    RETRYING_ANALYSIS = "retrying_analysis"         # analysis 重试过渡态
    RETRYING_RETRIEVAL = "retrying_retrieval"       # retrieval_rag/retrieval_file 共用重试态
    RETRYING_SUMMARY = "retrying_summary"           # summary 重试过渡态
    ROLLING_BACK = "rolling_back"                   # 回退中：即将重跑上游节点

    FALLBACK_CLARIFY = "fallback_clarify"           # Tier1 兜底：向用户澄清追问
    FALLBACK_ERROR = "fallback_error"               # Tier2 兜底：友好错误 + 开发者告警
    FAILED = "failed"                               # 失败终态（保留）


# 各 Agent（及回退）的最大重试次数表：键为 agent_name，值来自 core.config.settings
MAX_RETRIES = {
    "vague": settings.AGENT_MAX_RETRIES_VAGUE,
    "analysis": settings.AGENT_MAX_RETRIES_ANALYSIS,
    "retrieval_rag": settings.AGENT_MAX_RETRIES_RETRIEVAL,
    "retrieval_file": settings.AGENT_MAX_RETRIES_RETRIEVAL,
    "summary": settings.AGENT_MAX_RETRIES_SUMMARY,
    "rollback": settings.AGENT_MAX_RETRIES_ROLLBACK,
}

# agent_name -> 重试过渡态：can_retry 通过后先 transition 到此处留痕
RETRYING_STATES = {
    "vague": AgentState.RETRYING_VAGUE,
    "analysis": AgentState.RETRYING_ANALYSIS,
    "retrieval_rag": AgentState.RETRYING_RETRIEVAL,
    "retrieval_file": AgentState.RETRYING_RETRIEVAL,
    "summary": AgentState.RETRYING_SUMMARY,
}

# agent_name -> 恢复态（重试留痕后立即迁回的实际执行节点态）
RESUME_STATES = {
    "vague": AgentState.VAGUE_DETECTING,
    "analysis": AgentState.ANALYZING,
    "retrieval_rag": AgentState.RETRIEVING,
    "retrieval_file": AgentState.RETRIEVING,
    "summary": AgentState.SUMMARIZING,
}

# 关键 Agent：失败需经 FailureDiagnoser 诊断，区分澄清/重试/最终兜底
CRITICAL_AGENTS = {"vague", "analysis"}
# 非关键 Agent：失败仅重试，耗尽后用降级值继续，不阻断回答主链路
NON_CRITICAL_AGENTS = {"retrieval_rag", "retrieval_file", "summary"}


class PipelineStateMachine:
    """Agent 流水线状态机：跟踪状态、重试计数、链路日志、循环检测。

    类作用：承载单次对话请求的全部编排运行时数据——当前所处 AgentState、
    每个 Agent 的重试计数、Agent 产物缓存、回退计数/目标、全链路日志
    chain_log，以及基于全局步数与滑动窗口指纹的死循环检测。
    实例化位置：service/agent_service.py 的 run_agent() 与
    run_agent_stream() 入口（每请求一个实例）；FallbackHandler 也持有
    同一实例以切换兜底态。
    关键属性去向：
    - state：编排层每个阶段开始/回退/兜底时 transition 改写；
    - retry_counts / rollback_count：can_retry / can_rollback 的判定依据；
    - agent_outputs：缓存各 Agent 最近产物（FailureDiagnoser 诊断时作为上下文）；
    - chain_log：请求结束后由 AgentService._persist_chain_log 异步落库，
      Tier2 兜底时经 alert_chain_failure 推送给开发者；
    - total_steps / _recent_actions：is_looping() 死循环检测输入。
    """

    # 单请求全局最大执行步数（含重试/回退）：来源 settings.AGENT_MAX_TOTAL_STEPS，超限判定死循环
    MAX_TOTAL_STEPS = settings.AGENT_MAX_TOTAL_STEPS
    # 循环检测滑动窗口大小（动作指纹条数）：来源 settings.AGENT_LOOP_WINDOW
    LOOP_WINDOW = settings.AGENT_LOOP_WINDOW

    def __init__(self, query, user_id, session_id):
        # type: (str, Optional[int], Optional[int]) -> None
        """初始化一次请求的状态机。

        被谁调用：AgentService.run_agent() / run_agent_stream() 入口。
        参数：
        - query：本次用户查询（来源：ChatService 改写后的最终查询；
          供 verifier/diagnoser 与告警使用）；
        - user_id / session_id：JWT 用户 ID / 会话 ID（来源：请求上下文；
          供告警与链路日志关联）。
        """
        self.state = AgentState.IDLE
        self.query = query
        self.user_id = user_id
        self.session_id = session_id

        # 各 Agent 重试计数，键与 MAX_RETRIES 对齐，初值 0
        self.retry_counts = {k: 0 for k in MAX_RETRIES}
        self.chain_log = []  # type: list  # 全链路事件日志（转移/输出/错误/回退/诊断/评分）
        self.agent_outputs = {}  # type: Dict[str, Any]  # agent_name -> 最近一次产物
        self.rollback_count = 0
        self.rollback_target = None  # type: Optional[str]  # 最近一次回退目标 Agent 名

        # 循环检测：全局步数计数 + 最近动作指纹
        self.total_steps = 0
        self._recent_actions = []  # type: list  # 形如 "vague:critical_execute" 的指纹滑动窗口

    def transition(self, new_state):
        # type: (AgentState) -> None
        """状态迁移：切换当前状态并向 chain_log 追加一条 transition 记录。

        被谁调用：agent_service.py 编排流程的每个阶段边界
        （正常推进、RETRYING/RESUME 重试留痕、ROLLING_BACK 回退、
        FallbackHandler 的 FALLBACK_* 兜底切换）。
        参数：new_state——目标 AgentState（来源：编排层按 Agent 执行结果选择）。
        返回：None。输出去向：chain_log 中的 from/to/timestamp 记录，
        最终随链路日志异步落库。
        """
        old = self.state
        self.state = new_state
        self.chain_log.append({
            "type": "transition",
            "from": old.value,
            "to": new_state.value,
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def can_retry(self, agent_name):
        # type: (str) -> bool
        """判定指定 Agent 是否还能重试（当前计数 < MAX_RETRIES 配置上限）。

        被谁调用：_run_critical_agent / _run_non_critical_agent 的异常分支。
        参数：agent_name——Agent 标识（vague/analysis/retrieval_rag/
        retrieval_file/summary）。
        返回：bool——True 去向：调用方 record_retry 后重跑；
        False 去向：关键 Agent 走 Tier2 兜底，非关键 Agent 用降级值收场。
        """
        return self.retry_counts.get(agent_name, 0) < MAX_RETRIES.get(agent_name, 0)

    def record_retry(self, agent_name):
        # type: (str) -> None
        """记录一次重试：递增该 Agent 的重试计数。

        被谁调用：can_retry 返回 True 之后、transition 到重试态之前。
        参数：agent_name——同上。
        返回：None。输出去向：更新 retry_counts，影响后续 can_retry 判定。
        """
        self.retry_counts[agent_name] = self.retry_counts.get(agent_name, 0) + 1

    def record_output(self, agent_name, output):
        # type: (str, Any) -> None
        """记录 Agent 成功产物：缓存本体并向 chain_log 追加摘要事件。

        被谁调用：_run_critical_agent / _run_non_critical_agent 成功分支，
        以及 _run_summary_with_relevance 记录汇总结果/兜底文案。
        参数：
        - agent_name：产出 Agent 标识；
        - output：Agent 返回值（通常为 dict，取 answer/error 前 200 字做摘要）。
        返回：None。输出去向：agent_outputs[agent_name]（供诊断器作上下文）
        与 chain_log 的 output 事件（携带 prompt 版本号，便于回溯）。
        """
        self.agent_outputs[agent_name] = output
        summary = ""
        if isinstance(output, dict):
            # 优先取 answer，其次 error，截断 200 字防止日志膨胀
            summary = str(output.get("answer", output.get("error", "")))[:200]
        elif output is not None:
            summary = str(output)[:200]
        self.chain_log.append({
            "type": "output",
            "agent": agent_name,
            "output_summary": summary,
            "prompt_version": get_prompt_version(agent_name),
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def record_error(self, agent_name, error):
        # type: (str, str) -> None
        """记录 Agent 执行错误：向 chain_log 追加 error 事件（截断 500 字）。

        被谁调用：_run_critical_agent / _run_non_critical_agent 的 except 分支。
        参数：agent_name——失败 Agent 标识；error——异常文本（来源：捕获的 Exception）。
        返回：None。输出去向：chain_log，最终落库并在 Tier2 时推送开发者。
        """
        self.chain_log.append({
            "type": "error",
            "agent": agent_name,
            "error": str(error)[:500],
            "prompt_version": get_prompt_version(agent_name),
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def can_rollback(self):
        # type: () -> bool
        """判定是否还能执行验证不一致后的回滚重跑（回退计数 < rollback 上限）。

        被谁调用：run_agent/run_agent_stream 中 analysis 与 chat 阶段
        verifier.verify 判"不一致"之后，以及汇总相关性回退的轮次控制外层。
        返回：bool——True 去向：record_rollback 后重跑上游链；
        False 去向：包装兜底结果返回前端。
        """
        return self.rollback_count < MAX_RETRIES["rollback"]

    def record_rollback(self, target_agent):
        # type: (str) -> None
        """记录一次回退：递增回退计数、记住目标并追加 rollback 日志。

        被谁调用：can_rollback 通过后（目标如 "analysis"/"chat"）。
        参数：target_agent——回退重跑的目标 Agent 名。
        返回：None。输出去向：rollback_count/rollback_target 与 chain_log。
        """
        self.rollback_count += 1
        self.rollback_target = target_agent
        self.chain_log.append({
            "type": "rollback",
            "target": target_agent,
            "rollback_count": self.rollback_count,
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def record_step(self, agent_name, action_type="execute"):
        # type: (str, str) -> None
        """记录一步执行：更新全局计数 + 添加动作指纹到滑动窗口。

        被谁调用：_run_critical_agent（action_type="critical_execute"）
        与 _run_non_critical_agent（"non_critical_execute"）每轮循环开头。
        参数：agent_name——执行的 Agent；action_type——动作类型（构成指纹）。
        返回：None。输出去向：total_steps 递增、_recent_actions 追加
        "agent_name:action_type" 指纹，并追加 step 事件到 chain_log。
        """
        self.total_steps += 1
        fingerprint = "{}:{}".format(agent_name, action_type)
        self._recent_actions.append(fingerprint)
        # 保持窗口大小：超过 2 倍窗口时裁回 1 倍窗口，避免列表无限增长
        if len(self._recent_actions) > self.LOOP_WINDOW * 2:
            self._recent_actions = self._recent_actions[-self.LOOP_WINDOW:]
        self.chain_log.append({
            "type": "step",
            "agent": agent_name,
            "action": action_type,
            "total_steps": self.total_steps,
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def is_looping(self):
        # type: () -> bool
        """检测是否陷入循环：全局步数超限 或 滑动窗口内出现重复模式。

        被谁调用：关键/非关键 Agent 执行包装器每轮 record_step 之后；
        返回 True 时关键 Agent 直接走 Tier2 兜底，非关键 Agent 用降级值收场。
        返回：bool。
        判定规则：
        - total_steps >= MAX_TOTAL_STEPS：硬上限，无条件判死循环；
        - 最近 LOOP_WINDOW 条指纹中去重后数量不足窗口一半：认为在重复
          同样的 agent+action 组合（重试/回退卡死）。
        """
        if self.total_steps >= self.MAX_TOTAL_STEPS:
            logger.warning(
                "Loop detected: total_steps=%d >= MAX_TOTAL_STEPS=%d",
                self.total_steps, self.MAX_TOTAL_STEPS,
            )
            return True
        # 检查滑动窗口内是否有重复的 agent+action 模式
        if len(self._recent_actions) >= self.LOOP_WINDOW:
            window = self._recent_actions[-self.LOOP_WINDOW:]
            # 唯一指纹数不到窗口一半：重复模式占比过高，判定循环
            if len(set(window)) < len(window) // 2:
                logger.warning(
                    "Loop detected: repeated pattern in recent actions: %s",
                    window,
                )
                return True
        return False
