"""
模块名：app.application.chat.agent_service
作用：Agent 自动化流程服务（状态机编排版），是对话主链路的核心编排层。
      对上向 app/application/chat/chat_service.py 暴露非流式 run_agent() 与流式
      run_agent_stream() 两个入口；对下协调 app/domain/agents/ 下各 Agent、
      PipelineStateMachine 状态机、失败诊断/意图校验/兜底处理器，
      并把全链路日志异步落 MySQL（dao/chain_log.py）。

核心设计：
- 引入 PipelineStateMachine 管理各 Agent 运转状态、重试计数、链路日志；
- VagueAgent / AnalysisAgent（关键）：失败先由 FailureDiagnoser 诊断，
  缺少关键信息 → Tier 1 兜底问用户，技术失败 → 重试；
- RAGAgent / FileAgent / SummaryAgent（非关键）：失败直接进入重试，不走诊断；
- SummaryAgent 额外增加相关性回退：模型评估输出与用户问题的相关性，
  低于 0.6 则回退重新检索+汇总，最多 3 轮，超限按空检索结果处理；
- 每个关键 Agent 输出后由 IntentVerifier 验证意图一致性，不一致则回退；
- 两级兜底：Tier 1 向用户澄清；Tier 2 友好错误 + 全链路日志推送开发者；
- 保持 run_agent() / run_agent_stream() 对外接口不变。

主要成员：
- AgentService：编排服务类（应用级单例，经 get_agent_service() 获取）。
- get_agent_service()：lru_cache 单例工厂。
- AgentService.run_agent()：非流式编排入口。
- AgentService.run_agent_stream()：流式编排入口（yield 状态/增量/错误事件）。
- 其余 _ 开头方法均为内部辅助：Agent 实例创建、关键/非关键执行包装器、
  相关性回退、检索编排、Chat 回滚重跑、兜底结果包装等。

被谁使用：
- app/application/chat/chat_service.py：ChatService.__init__ 中 get_agent_service() 持有单例，
  ChatService._handle_impl 调 run_agent()，_handle_stream_impl 调
  run_agent_stream()；结果经 app/api/v1/chat.py 的 /chat/send 接口返回
  前端，或经 /chat/stream 的 SSE 流式返回前端。
"""
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Dict, Optional

from app.domain.agents.message_bus import MessageBus
from app.domain.agents.summary_agent import SummaryAgent
from app.domain.agents.vague_agent import VagueAgent
from app.domain.agents.analysis_agent import AnalysisAgent
from app.domain.agents.file_agent import FileAgent
from app.domain.agents.rag_agent import RAGAgent
from app.domain.agents.chat_agent import ChatAgent
from app.domain.agents.state_machine import (
    AgentState,
    PipelineStateMachine,
    MAX_RETRIES,
    RESUME_STATES,
    RETRYING_STATES,
)
from app.domain.agents.failure_diagnoser import FailureDiagnoser
from app.domain.agents.verifier import IntentVerifier
from app.domain.agents.fallback import FallbackHandler
from app.application.ports.llm import LLMUnavailableError
from core.config import settings
from core.degradation_alert import alert_degradation
from core.metrics import record_agent_execution
from app.application.ports.persistence import get_chain_log_dao
from core.prompt_registry import get_prompt_version

# 模块级日志器：编排流程的阶段日志、重试/降级/循环检测告警均走该 logger
logger = logging.getLogger(__name__)


class AgentService:
    """多 Agent 自动化流程的编排服务（应用级单例）。

    类作用：以 PipelineStateMachine 状态机驱动 VagueAgent → AnalysisAgent →
            RAG/File 检索 → SummaryAgent → ChatAgent 的完整流水线，统一处理
            失败诊断、重试、意图校验回退、相关性回退与两级兜底，并在请求结束后
            异步把链路日志写入 MySQL。

    实例化位置：不在 control 层直接 new；唯一创建处为本模块末尾的
            get_agent_service()（@lru_cache 应用级单例），由
            app/application/chat/chat_service.py 的 ChatService.__init__ 调用并长期持有。
            tests/phase/test_all_changes.py 中另有测试目的的导入与反射检查。

    关键 self 属性：
    - _rag_db：RAGAgent 共享的持久化 Chroma 向量库实例，首次使用时经
      _get_rag_db() 懒加载（RAGAgent.build_shared_db()），之后被
      _create_agents() 中每个请求新建的 RAGAgent 复用，避免重复打开向量库。
    - _log_pool：chain_log 异步落库专用线程池（2 worker），由 run_agent /
      run_agent_stream 的 finally 块提交 _persist_chain_log 任务，
      保证落库不阻塞对话响应。
    """

    def __init__(self):
        # __init__ 无形参：单例由 get_agent_service() 无参构造
        self._rag_db = None
        # 链路日志落库线程池：与对话主链路隔离，max_workers=2 足够消化请求峰值
        self._log_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chainlog")

    @staticmethod
    def _persist_chain_log(sm, task_id):
        """异步持久化 chain_log 到 MySQL（不阻塞响应）。

        功能：把本次请求状态机累积的全链路日志（状态迁移/输出/错误/诊断/
              相关性评分等）写入 chain_log 表，供开发者排障与链路分析。
        被谁调用：run_agent() / run_agent_stream() 的 finally 块，
                  通过 self._log_pool.submit 在线程池中执行。
        参数：
        - sm：本次请求的 PipelineStateMachine 实例（请求内局部对象），
          读取其 user_id / session_id / chain_log。
        - task_id：本次请求的短任务 ID（uuid4 前 12 位，编排入口生成）。
        返回：无。数据去向：dao/chain_log.py 的 ChainLogDAO.insert → MySQL。
        异常：任何异常仅告警不抛出（后台任务，失败不得影响已返回的对话结果）。
        """
        try:
            get_chain_log_dao().insert(
                user_id=sm.user_id,
                session_id=sm.session_id,
                task_id=task_id,
                chain_log=sm.chain_log,
            )
        except Exception:
            logger.warning("chain_log persistence failed", exc_info=True)

    @staticmethod
    def _bind_user_id(user_id, *agents):
        """把 user_id 绑定到各 Agent 内部 LLM 客户端，用于按用户计量 token 用量。

        功能：遍历 Agent 列表，若其 llm 属性支持 set_user_id 则注入当前
              用户 ID；模型网关据此统计 per-user 月度用量（core/usage.py）。
        被谁调用：_create_agents()，在全部 Agent 创建后统一绑定。
        参数：
        - user_id：当前对话用户 ID，来源：app/api/v1/chat.py 从 JWT
          解析后经 ChatService.handle 透传。
        - *agents：本次请求新建的 Agent 实例（vague/analysis/summary/chat）。
        返回：无。绑定失败静默忽略（计量为辅助能力，不得阻断编排）。
        """
        for a in agents:
            llm = getattr(a, "llm", None)
            if llm is not None and hasattr(llm, "set_user_id"):
                try:
                    llm.set_user_id(user_id)
                except Exception:
                    pass

    def _get_rag_db(self):
        """懒加载并返回 RAGAgent 共享的持久化向量库（单例缓存于 self._rag_db）。

        功能：首次调用时通过 RAGAgent.build_shared_db() 构建/加载应用级共享
              Chroma 持久库（底层即 app/infrastructure/vector_store/persistent.py 的持久库实例），
              后续请求直接复用。
        被谁调用：_create_agents() 创建 RAGAgent 时传入 db 参数。
        参数：无。
        返回：共享持久化向量库对象；去向：作为 RAGAgent(db=...) 的检索库，
              数据来源/去向为 chromadb_data 持久化目录中的知识库向量。
        """
        if self._rag_db is None:
            self._rag_db = RAGAgent.build_shared_db()
        return self._rag_db

    def _create_agents(self, query, user_id, session_id, history,
                       session_keywords=None, user_profile=None,
                       history_summary=None):
        """创建一次请求所需的全部 Agent 实例（独立总线，并发隔离）。

        功能：为本次对话生成 task_id 与独立 MessageBus，并实例化 vague/
              analysis/rag/file/summary/chat 六个 Agent；RAG 复用单例共享库，
              File 不注入共享库（仅检索会话临时库）；最后绑定 user_id。
        被谁调用：run_agent() 与 run_agent_stream() 入口处各调用一次。
        参数（均由 ChatService 从 app/api/v1/chat.py 的请求上下文透传）：
        - query：改写后的最终查询文本（含上下文拼接，来源：用户输入 +
          ChatService 上下文改写）。
        - user_id：JWT 注入的当前用户 ID（LLM 用量计量/画像/检索隔离用）。
        - session_id：会话号（per-user 序列，会话临时库与记忆隔离用）。
        - history：早期摘要 + 最近 N 轮原文（来源：app/domain/memory/context_memory）。
        - session_keywords：会话累积关键词注入前缀（app/domain/memory/session_keyword_service）。
        - user_profile：用户画像注入前缀（app/domain/memory/profile_service）。
        - history_summary：早期对话摘要（供意图判定消解多轮指代）。
        返回：dict——键为 bus/vague/analysis/rag/file/summary/chat，
              各自对应 MessageBus 与 Agent 实例；去向：编排流程各阶段按名取用。
        """
        # 每次请求独立 task_id：作为 MessageBus 通道标识与 chain_log 关联键
        task_id = uuid.uuid4().hex[:12]
        message_bus = MessageBus(task_id=task_id)
        vague_agent = VagueAgent(bus=message_bus, history_summary=history_summary)
        analysis_agent = AnalysisAgent(
            bus=message_bus, user_id=user_id, session_id=session_id,
            history_summary=history_summary,
        )
        shared_db = self._get_rag_db()
        rag_agent = RAGAgent(
            message_bus=message_bus, db=shared_db,
            user_id=user_id, session_id=session_id,
        )
        file_agent = FileAgent(
            message_bus=message_bus, db=None,
            user_id=user_id, session_id=session_id,
        )
        summary_agent = SummaryAgent(message_bus=message_bus)
        chat_agent = ChatAgent(
            message_bus=message_bus, query=query, history=history,
            session_keywords=session_keywords, user_profile=user_profile,
        )
        self._bind_user_id(
            user_id, vague_agent, analysis_agent, summary_agent, chat_agent,
        )
        return {
            "bus": message_bus,
            "vague": vague_agent,
            "analysis": analysis_agent,
            "rag": rag_agent,
            "file": file_agent,
            "summary": summary_agent,
            "chat": chat_agent,
        }

    # ------------------------------------------------------------------
    # 核心辅助方法：带诊断的重试包装器
    # ------------------------------------------------------------------

    def _run_critical_agent(self, sm, diagnoser, fallback_handler,
                            agent_name, func, *args):
        """关键 Agent 执行包装器：失败 -> 诊断 -> 重试 or 兜底。

        功能：执行 VagueAgent/AnalysisAgent 等关键 Agent，每步先做循环检测；
              成功则记录指标与输出；异常时先交 FailureDiagnoser 诊断——
              缺信息（MISSING_INFO）走 Tier 1 澄清兜底，技术失败且未超重试
              上限则迁移到重试态后回到恢复态继续执行，超限走 Tier 2 最终兜底。
        被谁调用：run_agent() / run_agent_stream() 中 vague、analysis 阶段，
                  以及 _retry_chat_for_rollback() 重跑 analysis 时。
        参数：
        - sm：本次请求的 PipelineStateMachine（状态/重试计数/链路日志）。
        - diagnoser：FailureDiagnoser 实例（LLM 失败分类器）。
        - fallback_handler：FallbackHandler 实例（两级兜底响应构造）。
        - agent_name：Agent 标识（"vague"/"analysis"），用于状态机映射与指标。
        - func：被包装的 Agent 可调用（如 agents["vague"].create_agent）。
        - *args：透传给 func 的参数（如原始 query）。
        返回：Agent 正常输出 dict；或兜底处理器返回的澄清/错误结果 dict。
        异常：LLMUnavailableError（主备模型全不可用）直接上抛，由 ChatService
              统一降级；其余异常在循环内被消化为重试/兜底，不向外抛。
        """
        while True:
            # 循环检测：全局步数超限或检测到重复模式，防止重试/回退形成死循环
            sm.record_step(agent_name, "critical_execute")
            if sm.is_looping():
                logger.error("Loop detected in critical agent %s, aborting", agent_name)
                alert_degradation(
                    stage=agent_name, severity="error",
                    query=sm.query, user_id=sm.user_id,
                    session_id=sm.session_id,
                    error="loop detected: total_steps={}".format(sm.total_steps),
                )
                return fallback_handler.handle_final_fallback()

            try:
                start = time.perf_counter()
                result = func(*args)
                duration = time.perf_counter() - start
                record_agent_execution(agent_name, "success", duration)
                sm.record_output(agent_name, result)
                return result
            except LLMUnavailableError:
                raise
            except Exception as e:
                duration = time.perf_counter() - start
                record_agent_execution(agent_name, "error", duration)
                sm.record_error(agent_name, str(e))

                # 失败诊断：LLM 判定是"用户缺信息"还是"技术故障"，结果记入链路日志
                diagnosis = diagnoser.diagnose(
                    query=sm.query,
                    agent_name=agent_name,
                    error=str(e),
                    context=sm.agent_outputs,
                )
                sm.chain_log.append({
                    "type": "diagnosis",
                    "agent": agent_name,
                    "diagnosis": diagnosis,
                    "prompt_version": get_prompt_version("diagnoser"),
                })

                # 降级分支一：缺少关键信息 → Tier 1 澄清，直接向用户追问而不重试
                if diagnosis["type"] == "MISSING_INFO":
                    return fallback_handler.handle_missing_info(
                        diagnosis.get("clarification", "请提供更多信息")
                    )

                # 重试分支：技术失败且重试次数未耗尽，先进重试态（记录日志）再回恢复态
                if sm.can_retry(agent_name):
                    sm.record_retry(agent_name)
                    sm.transition(RETRYING_STATES[agent_name])
                    logger.warning(
                        "%s failed (attempt %d/%d), retrying: %s",
                        agent_name, sm.retry_counts[agent_name],
                        sm.retry_counts.get(agent_name, 0), e,
                    )
                    alert_degradation(
                        stage=agent_name, severity="warn",
                        query=sm.query, user_id=sm.user_id,
                        session_id=sm.session_id, error=str(e),
                    )
                    sm.transition(RESUME_STATES[agent_name])
                    continue
                else:
                    return fallback_handler.handle_final_fallback()

    def _run_non_critical_agent(self, sm, agent_name, func,
                                fallback_value=None):
        """非关键 Agent 执行包装器：失败直接重试，不走 FailureDiagnoser。

        功能：执行 RAG/File/Summary 等非关键 Agent：成功记录输出并返回；
              失败按状态机重试上限直接重试，超限或检测到死循环时告警并返回
              fallback_value（缺省 None），保证辅助环节故障不阻断主回答链路。
        被谁调用：_run_retrieval()（retrieval_rag/retrieval_file）、
                  _run_summary_with_relevance()（summary），以及
                  _retry_chat_for_rollback() 无 verifier 时的 summary 分支。
        参数：
        - sm：本次请求的 PipelineStateMachine。
        - agent_name：Agent 标识（"retrieval_rag"/"retrieval_file"/"summary"）。
        - func：被包装的 Agent.handle 可调用（无额外位置参数）。
        - fallback_value：重试耗尽/死循环时的降级返回值（默认 None）。
        返回：Agent 输出（dict 或 None）；失败且耗尽重试时返回 fallback_value，
              同时经 sm.record_output 记入链路日志。
        异常：LLMUnavailableError 直接上抛；其余异常在循环内消化为重试/降级。
        """
        while True:
            # 循环检测：全局步数超限或检测到重复模式，非关键 Agent 直接用降级值收场
            sm.record_step(agent_name, "non_critical_execute")
            if sm.is_looping():
                logger.error("Loop detected in non-critical agent %s, using fallback", agent_name)
                alert_degradation(
                    stage=agent_name, severity="error",
                    query=sm.query, user_id=sm.user_id,
                    session_id=sm.session_id,
                    error="loop detected: total_steps={}".format(sm.total_steps),
                )
                sm.record_output(agent_name, fallback_value)
                return fallback_value

            try:
                start = time.perf_counter()
                result = func()
                duration = time.perf_counter() - start
                record_agent_execution(agent_name, "success", duration)
                sm.record_output(agent_name, result or fallback_value)
                return result if result else fallback_value
            except LLMUnavailableError:
                raise
            except Exception as e:
                duration = time.perf_counter() - start
                record_agent_execution(agent_name, "error", duration)
                sm.record_error(agent_name, str(e))

                if sm.can_retry(agent_name):
                    sm.record_retry(agent_name)
                    logger.warning(
                        "%s failed (attempt %d/%d), retrying: %s",
                        agent_name, sm.retry_counts[agent_name],
                        MAX_RETRIES.get(agent_name, 0), e,
                    )
                    continue
                else:
                    logger.warning(
                        "%s exhausted retries, using fallback value", agent_name,
                    )
                    alert_degradation(
                        stage=agent_name, severity="warn",
                        query=sm.query, user_id=sm.user_id,
                        session_id=sm.session_id, error=str(e),
                    )
                    sm.record_output(agent_name, fallback_value)
                    return fallback_value

    def _run_summary_with_relevance(self, sm, verifier, analysis_result,
                                    agents, max_rounds=None):
        """SummaryAgent 执行 + 相关性回退：最多 max_rounds 轮检索+汇总+评分。

        功能：每轮先执行 SummaryAgent（内含重试），再用 IntentVerifier 的
              score_relevance 让模型评估汇总输出与用户问题的相关性；达到阈值
              settings.AGENT_SUMMARY_RELEVANCE_THRESHOLD 则通过并累积关键词；
              低于阈值则状态机回退到检索态重新检索 + 汇总。达到上限后以
              "未检索到相关知识库内容"的空检索兜底文案经总线发给 ChatAgent，
              不阻断后续回答流程。
        被谁调用：run_agent() / run_agent_stream() 的阶段 4，以及
                  _retry_chat_for_rollback() 带 verifier 时。
        参数：
        - sm：本次请求的 PipelineStateMachine。
        - verifier：IntentVerifier 实例（相关性打分模型）。
        - analysis_result：AnalysisAgent 的输出 dict（决定重新检索哪些源）。
        - agents：_create_agents() 返回的 Agent/总线字典。
        - max_rounds：相关性回退最大轮数；None 时取
          settings.AGENT_SUMMARY_MAX_ROUNDS（配置默认 3）。
        返回：dict——SummaryAgent 汇总结果（含 answer/keywords），或兜底
              {"success": True, "answer": 空检索提示文案}；结果同时经
              MessageBus 发布给 ChatAgent 作为回答素材。
        """
        if max_rounds is None:
            max_rounds = settings.AGENT_SUMMARY_MAX_ROUNDS

        fallback_output = {
            "success": True,
            "answer": "未检索到相关知识库内容，请基于通用知识简要回答用户问题，并注明为一般性建议。",
        }

        for round_idx in range(max_rounds):
            sm.transition(AgentState.SUMMARIZING)
            summary_result = self._run_non_critical_agent(
                sm, "summary", agents["summary"].handle,
                fallback_value=None,
            )

            if summary_result is None:
                logger.warning("summary returned None, treating as empty")
                sm.record_output("summary", fallback_output)
                agents["bus"].publish("SummaryAgent", "ChatAgent", fallback_output)
                return fallback_output

            summary_text = ""
            if isinstance(summary_result, dict):
                summary_text = summary_result.get("answer", "")

            # 无效重试短路：summary 为空或为"未检索到"类占位文案时，说明上游
            # 没有任何检索结果可汇总，回滚重新检索也不会改变结果，
            # 直接按空检索返回，避免 3 轮无效循环（假检索+假评分）。
            if not summary_text or "未检索到" in summary_text[:30]:
                logger.warning(
                    "summary is empty/fallback (no retrieval input), skipping relevance retry"
                )
                sm.record_output("summary", fallback_output)
                agents["bus"].publish("SummaryAgent", "ChatAgent", fallback_output)
                return fallback_output

            score = verifier.score_relevance(sm.query, summary_text)
            sm.chain_log.append({
                "type": "relevance_check",
                "round": round_idx + 1,
                "score": score,
                "prompt_version": get_prompt_version("relevance"),
            })

            if score >= settings.AGENT_SUMMARY_RELEVANCE_THRESHOLD:
                logger.info("summary relevance passed (score=%.2f)", score)
                sm.record_output("summary", summary_result)
                summary_kws = summary_result.get("keywords", [])
                # 相关性通过：把汇总抽取出的关键词累积进会话关键词（Redis），供后续轮次注入
                if summary_kws and sm.user_id and sm.session_id:
                    try:
                        from app.domain.memory.session_keyword_service import get_session_keyword_service
                        # 数据去向：app/domain/memory/session_keyword_service 的会话关键词缓存
                        get_session_keyword_service().accumulate(sm.user_id, sm.session_id, summary_kws)
                    except Exception:
                        pass
                return summary_result

            logger.warning(
                "summary relevance too low (score=%.2f, round %d/%d)",
                score, round_idx + 1, max_rounds,
            )

            if round_idx < max_rounds - 1:
                sm.transition(AgentState.ROLLING_BACK)
                sm.chain_log.append({
                    "type": "summary_relevance_rollback",
                    "round": round_idx + 1,
                })
                sm.transition(AgentState.RETRIEVING)
                self._run_retrieval(sm, analysis_result, agents)

        logger.warning(
            "summary relevance failed after %d rounds, treating as empty",
            max_rounds,
        )
        alert_degradation(
            stage="summary", severity="warn",
            query=sm.query, user_id=sm.user_id,
            session_id=sm.session_id,
            error="relevance below threshold after {} rounds".format(max_rounds),
        )
        sm.record_output("summary", fallback_output)
        agents["bus"].publish("SummaryAgent", "ChatAgent", fallback_output)
        return fallback_output

    def _is_fallback_state(self, sm):
        """检查状态机是否已进入兜底状态。

        功能：判定当前状态是否为 FALLBACK_CLARIFY（Tier 1 澄清）或
              FALLBACK_ERROR（Tier 2 错误兜底），供编排主流程在每个关键
              Agent 执行后决定是否提前结束流水线。
        被谁调用：run_agent() / run_agent_stream() 各阶段后的兜底检查，
                  以及 _retry_chat_for_rollback()。
        参数：sm——本次请求的 PipelineStateMachine。
        返回：bool——True 表示已进入兜底态，调用方应立即包装/流式输出兜底结果。
        """
        return sm.state in (AgentState.FALLBACK_CLARIFY, AgentState.FALLBACK_ERROR)

    # ------------------------------------------------------------------
    # 非流式编排
    # ------------------------------------------------------------------

    def run_agent(self, query, user_id=None, session_id=None,
                  history=None, session_keywords=None, user_profile=None,
                  history_summary=None):
        # type: (str, Optional[int], Optional[int], Optional[str], Optional[str], Optional[str], Optional[str]) -> Dict[str, Any]
        """启动非流式自动化流程，使用状态机驱动各 Agent 的执行、重试与兜底。

        功能：依次执行五个阶段——①VagueAgent 意图判定（模糊问题直接闲聊回答）；
              ②AnalysisAgent 需求分析（输出是否需要 RAG/File），输出经
              IntentVerifier 校验，不一致且可回滚时重跑一次；③RAG/File 检索
              （非关键，失败传空）；④SummaryAgent 汇总（带相关性回退）；
              ⑤ChatAgent 生成最终回答，校验失败时回滚重跑整条上游链。
              无论成功失败，finally 均异步提交 chain_log 落库。
        被谁调用：app/application/chat/chat_service.py 的 ChatService._handle_impl。
        参数：
        - query (str)：改写后的最终查询，来源：app/api/v1/chat.py /chat/send
          的用户输入经 ChatService 上下文改写。
        - user_id (int|None)：JWT 注入的用户 ID。
        - session_id (int|None)：当前会话 ID（per-user 序列）。
        - history (str|None)：上下文记忆注入文本（app/domain/memory/context_memory）。
        - session_keywords (str|None)：会话关键词前缀。
        - user_profile (str|None)：用户画像前缀。
        - history_summary (str|None)：早期对话摘要。
        返回：Dict[str, Any]——成功时含 success/user_input/vague_result/
              analysis_result/rag_result/chat_result；兜底时含 fallback 字段
              （clarify/error）；异常时 {"success": False, "error": ...}。
              去向：经 util/result_handle.handle_result 归一化后由
              app/api/v1/chat.py 的 /chat/send 接口返回前端。
        异常：LLMUnavailableError 直接上抛交 ChatService 统一友好降级；
              其余异常捕获后返回 success=False 结果（不向 control 抛错）。
        """
        sm = None
        # 入口生成 task_id：即使状态机构造失败，finally 也能按该 id 关联日志
        task_id = uuid.uuid4().hex[:12]
        try:
            logger.info("Starting agent service for query: %s", query)

            sm = PipelineStateMachine(query, user_id, session_id)
            diagnoser = FailureDiagnoser()
            fallback_handler = FallbackHandler(sm, query, user_id, session_id)
            verifier = IntentVerifier()
            agents = self._create_agents(query, user_id, session_id, history,
                                         session_keywords, user_profile,
                                         history_summary=history_summary)

            # === 阶段 1: VagueAgent（关键）===
            sm.transition(AgentState.VAGUE_DETECTING)
            vague_result = self._run_critical_agent(
                sm, diagnoser, fallback_handler,
                "vague", agents["vague"].create_agent, query,
            )
            if self._is_fallback_state(sm):
                return self._wrap_fallback_result(sm, fallback_handler, query, vague_result)

            # 意图判定为单次 LLM 直判、结果稳定，不再做 verifier 回滚重跑
            # （修复：旧实现 verifier 偶判"不一致"触发 VagueAgent 重跑，
            #   每条消息多耗约 10s 且重跑结果与首次几乎一致）

            is_vague = vague_result.get("success", False)
            if is_vague:
                chat_result = agents["chat"].handle()
                sm.transition(AgentState.COMPLETED)
                return {
                    "success": True,
                    "user_input": query,
                    "vague_result": vague_result,
                    "chat_result": chat_result,
                }

            # === 阶段 2: AnalysisAgent（关键）===
            sm.transition(AgentState.ANALYZING)
            analysis_result = self._run_critical_agent(
                sm, diagnoser, fallback_handler,
                "analysis", agents["analysis"].create_agent, query,
            )
            if self._is_fallback_state(sm):
                return self._wrap_fallback_result(sm, fallback_handler, query, analysis_result)

            if not analysis_result.get("success", True):
                analysis_result = {"success": True, "need_RAGAgent": True, "need_FileAgent": False}
                alert_degradation(
                    stage="analysis_agent", severity="warn",
                    query=query, user_id=user_id, session_id=session_id,
                    error="analysis returned success=False",
                )

            if not verifier.verify(query, "analysis", analysis_result):
                if sm.can_rollback():
                    sm.record_rollback("analysis")
                    sm.transition(AgentState.ROLLING_BACK)
                    sm.transition(AgentState.ANALYZING)
                    analysis_result = self._run_critical_agent(
                        sm, diagnoser, fallback_handler,
                        "analysis", agents["analysis"].create_agent, query,
                    )
                    if self._is_fallback_state(sm):
                        return self._wrap_fallback_result(sm, fallback_handler, query, analysis_result)
                    if not analysis_result.get("success", True):
                        analysis_result = {"success": True, "need_RAGAgent": True, "need_FileAgent": False}
                else:
                    return self._wrap_fallback_result(
                        sm, fallback_handler, query, analysis_result,
                    )

            # === 阶段 3: RAG/File（非关键，失败传空值）===
            sm.transition(AgentState.RETRIEVING)
            rag_result = self._run_retrieval(
                sm, analysis_result, agents,
            )

            # === 阶段 4: SummaryAgent（非关键 + 相关性回退）===
            self._run_summary_with_relevance(
                sm, verifier, analysis_result, agents,
            )

            # === 阶段 5: ChatAgent ===
            sm.transition(AgentState.GENERATING)
            chat_result = agents["chat"].handle()

            if not chat_result.get("success", False):
                chat_result = {
                    "success": False,
                    "answer": "抱歉，处理您的问题时遇到问题，请稍后重试。",
                }

            if not verifier.verify(query, "chat", chat_result):
                # 保留回滚前已生成的成功回答：回滚重跑失败时不再作废它
                previous_chat = chat_result if chat_result.get("success", False) else None
                if sm.can_rollback():
                    sm.record_rollback("chat")
                    sm.transition(AgentState.ROLLING_BACK)
                    chat_result = self._retry_chat_for_rollback(
                        sm, diagnoser, fallback_handler, agents, query,
                        verifier=verifier, analysis_result=analysis_result,
                        previous_chat_result=previous_chat,
                    )
                    if self._is_fallback_state(sm):
                        if chat_result.get("success", False):
                            # 回滚失败但已有成功回答：直接返回，不再走兜底报错
                            sm.transition(AgentState.COMPLETED)
                            logger.info(
                                "Chat rollback failed but previous answer kept for query: %s", query,
                            )
                            return {
                                "success": True,
                                "user_input": query,
                                "vague_result": vague_result,
                                "analysis_result": analysis_result,
                                "rag_result": rag_result,
                                "chat_result": chat_result,
                            }
                        return self._wrap_fallback_result(sm, fallback_handler, query, chat_result)
                else:
                    return self._wrap_fallback_result(
                        sm, fallback_handler, query, chat_result,
                    )

            sm.transition(AgentState.COMPLETED)
            logger.info("Agent service completed successfully for query: %s", query)
            return {
                "success": True,
                "user_input": query,
                "vague_result": vague_result,
                "analysis_result": analysis_result,
                "rag_result": rag_result,
                "chat_result": chat_result,
            }

        except LLMUnavailableError:
            raise
        except Exception as e:
            logger.exception("Agent service failed for query: %s", query)
            return {
                "success": False,
                "user_input": query,
                "error": str(e),
            }
        finally:
            if sm is not None:
                self._log_pool.submit(self._persist_chain_log, sm, task_id)

    def _run_retrieval(self, sm, analysis_result, agents):
        """运行 RAG/File 检索（非关键，各自独立重试，失败传空值）。

        功能：按 AnalysisAgent 的 need_RAGAgent/need_FileAgent 标志选择执行
              RAGAgent（公共/私有持久知识库）与 FileAgent（会话临时知识库），
              各自经非关键包装器重试；检索结果通过 MessageBus 供 SummaryAgent
              消费，本方法仅回传"该工具是否被调用"的标记。
        被谁调用：run_agent() / run_agent_stream() 的阶段 3，以及
                  _retry_chat_for_rollback() 回滚重跑时。
        参数：
        - sm：本次请求的 PipelineStateMachine。
        - analysis_result：AnalysisAgent 输出 dict（来源：LLM 分析结果，
          其中 need_RAGAgent/need_FileAgent 决定检索开关）。
        - agents：_create_agents() 返回的 Agent 字典。
        返回：dict——{"rag": "rag_agent_called"|None,
              "file": "file_agent_called"|None}，作为 rag_result 回传编排入口。
        """
        rag_tool = {"rag": None, "file": None}
        jobs = []
        if analysis_result.get("need_RAGAgent", False):
            jobs.append(("rag", agents["rag"]))
        if analysis_result.get("need_FileAgent", False):
            jobs.append(("file", agents["file"]))
        if not jobs:
            return rag_tool

        for key, agent in jobs:
            agent_name = "retrieval_{}".format(key)
            self._run_non_critical_agent(
                sm, agent_name, agent.handle,
                fallback_value=None,
            )
            rag_tool[key] = "{}_agent_called".format(key)

        return rag_tool

    def _retry_chat_for_rollback(self, sm, diagnoser, fallback_handler,
                                 agents, query, verifier=None,
                                 analysis_result=None, previous_chat_result=None):
        """ChatAgent 验证失败后回退：重新运行上游链 + ChatAgent。

        功能：按 分析 → 检索 → 汇总（有 verifier 走相关性回退，否则走普通
              非关键重试）→ 生成 的顺序重跑整条上游链，得到新的 ChatAgent
              回答。任一步骤进入兜底态或抛异常时，优先返回回滚前已生成的
              成功回答，避免把已经生成好的回答作废成兜底报错。
        被谁调用：run_agent() 阶段 5 ChatAgent 意图校验失败且状态机允许
                  回滚（can_rollback）时。
        参数：
        - sm：本次请求的 PipelineStateMachine。
        - diagnoser / fallback_handler：关键 Agent 包装器所需的诊断器与兜底器。
        - agents：本次请求的 Agent 字典（重跑复用同一批实例与总线）。
        - query：原始用户查询（重跑 AnalysisAgent 的输入）。
        - verifier：意图校验器；None 时汇总阶段不做相关性回退。
        - analysis_result：重跑前的分析结果（当前实现中被重跑结果覆盖）。
        - previous_chat_result：回滚前已生成的成功回答 dict；重跑失败时
          作为保底回答返回（来源：首次 ChatAgent.handle 的 LLM 输出）。
        返回：dict——重跑得到的 chat_result；失败且无保底时
              {"success": False, "answer": ""}；有保底时返回 previous_chat_result。
        异常：LLMUnavailableError 上抛；其余异常捕获后返回保底/失败结果。
        """
        try:
            analysis_result = self._run_critical_agent(
                sm, diagnoser, fallback_handler,
                "analysis", agents["analysis"].create_agent, query,
            )
            if self._is_fallback_state(sm):
                if previous_chat_result and previous_chat_result.get("success", False):
                    return previous_chat_result
                return {"success": False, "answer": ""}

            if not analysis_result.get("success", True):
                analysis_result = {"success": True, "need_RAGAgent": True, "need_FileAgent": False}

            self._run_retrieval(sm, analysis_result, agents)
            if verifier is not None:
                self._run_summary_with_relevance(
                    sm, verifier, analysis_result, agents,
                )
            else:
                self._run_non_critical_agent(
                    sm, "summary", agents["summary"].handle,
                    fallback_value=None,
                )

            sm.transition(AgentState.GENERATING)
            chat_result = agents["chat"].handle()
            return chat_result
        except LLMUnavailableError:
            raise
        except Exception as e:
            logger.warning("Chat rollback failed: %s", e)
            if previous_chat_result and previous_chat_result.get("success", False):
                return previous_chat_result
            return {"success": False, "answer": ""}

    @staticmethod
    def _wrap_fallback_result(sm, fallback_handler, query, last_result):
        """将兜底处理器的返回包装为与正常返回兼容的格式。

        功能：根据状态机当前兜底态，把 Tier 1 澄清/Tier 2 错误统一包装成
              与 run_agent 正常返回同构的 dict（均带 chat_result.answer），
              使上层 handle_result/前端无需区分正常与兜底分支。
        被谁调用：run_agent() 各阶段检测到 _is_fallback_state 时。
        参数：
        - sm：本次请求的 PipelineStateMachine（读取 state 判定兜底类型）。
        - fallback_handler：FallbackHandler（提供澄清/最终错误文案）。
        - query：原始用户查询，回填 user_input 字段。
        - last_result：进入兜底前最后一个 Agent 的输出（保留字段兼容性）。
        返回：dict——澄清态 {"success": True, "fallback": "clarify", ...}；
              错误态 {"success": False, "fallback": "error", "error",
              "chat_result": {"success": False, "answer": 友好错误文案}}。
        """
        if sm.state == AgentState.FALLBACK_CLARIFY:
            return {
                "success": True,
                "fallback": "clarify",
                "user_input": query,
                "chat_result": {
                    "success": True,
                    "answer": fallback_handler.handle_fallback().get(
                        "message", "请问您能否提供更详细的信息？"
                    ),
                },
            }
        fb = fallback_handler.handle_final_fallback()
        return {
            "success": False,
            "fallback": "error",
            "user_input": query,
            "error": fb.get("message", "系统暂时遇到问题"),
            "chat_result": {
                "success": False,
                "answer": fb.get("message", "抱歉，系统暂时遇到问题，请稍后重新输入您的问题。"),
            },
        }

    # ------------------------------------------------------------------
    # 流式编排
    # ------------------------------------------------------------------

    def run_agent_stream(self, query, user_id=None, session_id=None,
                         history=None, session_keywords=None, user_profile=None,
                         history_summary=None):
        """流式对话编排：状态机驱动 + yield status/delta/error 事件。

        功能：与 run_agent 相同的五阶段流水线，但在意图/分析/检索/汇总前
              yield {"type": "status", ...} 阶段提示帧，最终回答由
              ChatAgent.handle_stream 的文本增量包装为 delta 帧逐块 yield；
              兜底时经 _stream_fallback 输出澄清 delta 或错误帧。
              finally 同样异步提交 chain_log 落库。
        被谁调用：app/application/chat/chat_service.py 的 ChatService._handle_stream_impl，
                  事件帧向上经 app/api/v1/chat.py 的 /chat/stream 接口
                  序列化为 SSE（data: {...}\\n\\n）流式返回前端。
        参数：同 run_agent（query/user_id/session_id/history/
              session_keywords/user_profile/history_summary，来源一致）。
        返回：generator——逐帧 yield dict：
              - {"type": "status", "stage", "message"}：前置阶段提示；
              - {"type": "delta", "content"}：回答文本增量；
              - {"type": "error", "message"}：兜底错误。
        """
        sm = None
        # 入口生成 task_id：chain_log 异步落库的关联键
        task_id = uuid.uuid4().hex[:12]
        try:
            logger.info("Starting streaming agent service for query: %s", query)

            sm = PipelineStateMachine(query, user_id, session_id)
            diagnoser = FailureDiagnoser()
            fallback_handler = FallbackHandler(sm, query, user_id, session_id)
            verifier = IntentVerifier()
            agents = self._create_agents(query, user_id, session_id, history,
                                         session_keywords, user_profile,
                                         history_summary=history_summary)

            # === 阶段 1: VagueAgent ===
            yield {"type": "status", "stage": "intent", "message": "正在理解您的问题..."}
            sm.transition(AgentState.VAGUE_DETECTING)
            vague_result = self._run_critical_agent(
                sm, diagnoser, fallback_handler,
                "vague", agents["vague"].create_agent, query,
            )
            if self._is_fallback_state(sm):
                yield from self._stream_fallback(fallback_handler)
                return

            # 意图判定为单次 LLM 直判、结果稳定，不再做 verifier 回滚重跑
            # （与 run_agent 保持一致，避免 VagueAgent 双重执行）

            is_vague = vague_result.get("success", False)
            if is_vague:
                yield from self._stream_chat(agents["chat"])
                return

            # === 阶段 2: AnalysisAgent ===
            yield {"type": "status", "stage": "analysis", "message": "正在分析您的需求..."}
            sm.transition(AgentState.ANALYZING)
            analysis_result = self._run_critical_agent(
                sm, diagnoser, fallback_handler,
                "analysis", agents["analysis"].create_agent, query,
            )
            if self._is_fallback_state(sm):
                yield from self._stream_fallback(fallback_handler)
                return

            if not analysis_result.get("success", True):
                analysis_result = {"success": True, "need_RAGAgent": True, "need_FileAgent": False}
                alert_degradation(
                    stage="analysis_agent", severity="warn",
                    query=query, user_id=user_id, session_id=session_id,
                    error="analysis returned success=False",
                )

            if not verifier.verify(query, "analysis", analysis_result):
                if sm.can_rollback():
                    sm.record_rollback("analysis")
                    sm.transition(AgentState.ROLLING_BACK)
                    sm.transition(AgentState.ANALYZING)
                    analysis_result = self._run_critical_agent(
                        sm, diagnoser, fallback_handler,
                        "analysis", agents["analysis"].create_agent, query,
                    )
                    if self._is_fallback_state(sm):
                        yield from self._stream_fallback(fallback_handler)
                        return
                    if not analysis_result.get("success", True):
                        analysis_result = {"success": True, "need_RAGAgent": True, "need_FileAgent": False}
                else:
                    yield from self._stream_fallback(fallback_handler)
                    return

            # === 阶段 3: RAG/File ===
            if analysis_result.get("need_RAGAgent", False) or analysis_result.get("need_FileAgent", False):
                yield {"type": "status", "stage": "retrieval", "message": "正在检索知识库..."}
            sm.transition(AgentState.RETRIEVING)
            self._run_retrieval(sm, analysis_result, agents)

            # === 阶段 4: SummaryAgent（相关性回退）===
            yield {"type": "status", "stage": "summary", "message": "正在组织回答..."}
            self._run_summary_with_relevance(
                sm, verifier, analysis_result, agents,
            )

            # === 阶段 5: ChatAgent ===
            sm.transition(AgentState.GENERATING)
            yield from self._stream_chat(agents["chat"])
        finally:
            if sm is not None:
                self._log_pool.submit(self._persist_chain_log, sm, task_id)

    @staticmethod
    def _stream_chat(chat_agent):
        """把 ChatAgent 的文本增量包装为统一 delta 事件。

        功能：消费 ChatAgent.handle_stream() 的 LLM 流式 token，非空增量
              包装为 {"type": "delta", "content": ...} 帧向上 yield。
        被谁调用：run_agent_stream() 阶段 5（模糊闲聊分支与正常回答分支）。
        参数：chat_agent——本次请求的 ChatAgent 实例（其 handle_stream
              数据来源：model_llm 网关的流式 LLM 返回）。
        返回：generator——delta 事件帧；去向：ChatService →
              app/api/v1/chat.py 的 SSE 流式返回前端逐字渲染。
        """
        for delta in chat_agent.handle_stream():
            if delta:
                yield {"type": "delta", "content": delta}

    @staticmethod
    def _stream_fallback(fallback_handler):
        """兜底时 yield 相应的事件帧。

        功能：取 FallbackHandler.handle_fallback() 的兜底结果——澄清类
              作为 delta 帧（像正常回答一样展示追问文案），其余作为
              error 帧返回。
        被谁调用：run_agent_stream() 各关键阶段检测到兜底态时。
        参数：fallback_handler——本次请求的 FallbackHandler。
        返回：generator——单帧 {"type": "delta", "content"} 或
              {"type": "error", "message"}，经 SSE 返回前端。
        """
        result = fallback_handler.handle_fallback()
        if result.get("fallback") == "clarify":
            yield {"type": "delta", "content": result.get("message", "")}
        else:
            yield {"type": "error", "message": result.get("message", "系统暂时遇到问题，请稍后重试。")}


@lru_cache(maxsize=1)
def get_agent_service():
    """AgentService 应用级单例工厂。

    功能：以 lru_cache 保证全进程仅构建一个 AgentService（共享 RAG 向量库
          句柄与 chain_log 线程池）。
    被谁调用：app/application/chat/chat_service.py 的 ChatService.__init__（每个
              ChatService 单例初始化时调用一次，实际命中同一缓存实例）。
    返回：AgentService 唯一实例。
    """
    return AgentService()
