"""
模块名：app.infrastructure.llm.gateway

作用：
    LLM 网关：在 ChatOpenAI 之上叠加重试与降级，所有 LLM 调用点统一经此构建。

策略：
1. 重试：每个模型最多 gateway_max_retries 次重试（指数退避 + 抖动），
   仅对瞬时错误重试——超时/连接失败/429 限流/5xx/空响应；
2. 降级（模型级）：主模型重试耗尽后，依次尝试 fallback_model_names（如 qwen-turbo）；
3. 降级（调用方级）：全链失败抛 LLMUnavailableError，由调用方决定降级响应
   （ChatService 返回友好提示；上下文改写失败则退回原始 query，不阻断对话）；
4. 致命错误（401/403/400 等）不重试、不换模型（同一密钥与参数下换模型无意义），快速失败。

接口与 ChatOpenAI 完全一致（bind_app/domain/tools/create_react_agent/chain 无感知）：
非流式路径汇入 _generate、流式路径汇入 _stream，在此两处统一织入重试与降级。

配置来源（build_chat_model 默认值，env/qianwen_config.env → config/setting.py
的 LLMConfig/llm 单例）：
- model/api_key/base_url：env 的 model/api_key/base_url（主模型名、【密钥类字段】、
  DashScope OpenAI 兼容地址 https://dashscope.aliyuncs.com/compatible-mode/v1）；
- timeout=llm.TIMEOUT_SECONDS（env: llm_timeout_seconds，默认 30s）；
- max_tokens=llm.MAX_TOKENS（env: llm_max_tokens，默认 2000）；
- gateway_max_retries=llm.MAX_RETRIES（env: llm_max_retries，默认 2）；
- gateway_backoff_base=llm.BACKOFF_BASE_SECONDS（env: llm_backoff_base_seconds，
  默认 0.5；延迟 = base*2^attempt + 0~0.25s 随机抖动）；
- fallback_model_names=llm.FALLBACK_MODELS（env: llm_fallback_models，逗号分隔
  的降级模型链，按序尝试；空串表示不降级）。

被谁使用（Grep build_chat_model 确认）：
- model_llm/llm.py 的 LLM 基类（llm_business 全部提示词类经此间接持有网关）；
- multi_agent：base_agent.py（各 ReAct Agent 基类）、chat_agent.py、
  rag_agent.py、file_agent.py、summary_agent.py、verifier.py、failure_diagnoser.py；
- app/application/files/preference_service.py、app/domain/memory/profile_service.py、app/domain/memory/context_app.domain.memory.py、
  app/application/chat/context.py。

与 embedding 侧的关键差异：chat 允许跨模型降级（不同 chat 模型输出同为文本，
可互换）；embedding 侧只重试、禁止换模型——换 embedding 模型会改变向量维度，
导致既有 Chroma 向量与查询向量无法比较（检索静默失效），见
embedding/embedding_model.py 的 RetryingDashScopeEmbeddings。
"""
import logging
import random
import time
from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.pydantic_v1 import Field, PrivateAttr
from langchain_openai import ChatOpenAI

from config.setting import llm
from core.usage import get_current_user_id, record_usage

logger = logging.getLogger(__name__)

# openai v1 SDK 异常分类（langchain-openai 0.1.x 底层使用 openai>=1.x）
try:
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    _RETRYABLE_EXCEPTIONS = (APITimeoutError, APIConnectionError, RateLimitError)
except ImportError:  # pragma: no cover - openai 必装，防御性兜底
    _RETRYABLE_EXCEPTIONS = (TimeoutError, ConnectionError)


class LLMUnavailableError(Exception):
    """主模型与全部降级模型均不可用（重试耗尽或致命错误）。"""


class _EmptyResponseError(Exception):
    """空响应（无内容且无工具调用）：视为瞬时故障，可重试/换模型。"""


def _is_retryable(error: Exception) -> bool:
    """判断异常是否属于可重试的瞬时错误。

    被谁调用：LLMGateway._invoke_with_retry、_generate、_stream 的重试/降级分支。
    参数：error —— 调用 ChatOpenAI 时捕获的异常。
    返回：bool，True 表示瞬时故障（空响应 / APITimeoutError /
          APIConnectionError / RateLimitError / HTTP 5xx），可重试或换模型；
          False 表示认证/参数类致命错误（401/403/400 等），快速失败。
    """
    if isinstance(error, _EmptyResponseError):
        return True
    if isinstance(error, _RETRYABLE_EXCEPTIONS):
        return True
    status_code = getattr(error, "status_code", None)
    return isinstance(status_code, int) and status_code >= 500


class LLMGateway(ChatOpenAI):
    """带重试与模型降级的 ChatOpenAI，drop-in 替代原构造。

    实例化位置：业务代码不直接 new，统一由本模块 build_chat_model() 工厂创建
    （model_llm/llm.py 的 LLM 基类与 multi_agent 各 Agent、service/memory 等
    调用方均经工厂）；主模型即 ChatOpenAI 自身（self），降级模型是惰性构建的
    普通 ChatOpenAI 客户端列表（共享同一 api_key/base_url，仅 model 不同）。
    """

    # 降级模型链：主模型重试耗尽后按列表顺序尝试（来源 llm.FALLBACK_MODELS，env: llm_fallback_models）
    fallback_model_names: List[str] = Field(default_factory=list)
    # 每个模型的最大重试次数（来源 llm.MAX_RETRIES，env: llm_max_retries，默认 2）
    gateway_max_retries: int = Field(default=2)
    # 指数退避基数秒（来源 llm.BACKOFF_BASE_SECONDS，默认 0.5，另加 0~0.25s 抖动）
    gateway_backoff_base: float = Field(default=0.5)

    # 惰性构建的降级客户端缓存：首次进入降级链时按 fallback_model_names 构造，之后复用
    _fallback_clients: Optional[List[ChatOpenAI]] = PrivateAttr(default=None)
    # 请求级用户 id：agent 的 LLM 实例在生成器内创建，用实例属性可跨 next() 存活；
    # context/title 等共享实例则走线程局部（core.usage.get_current_user_id）。
    _user_id: Optional[int] = PrivateAttr(default=None)

    def __init__(self, **kwargs: Any) -> None:
        """初始化网关（参数与 ChatOpenAI 一致，另含三个 gateway_*/fallback_* 字段）。

        参数：**kwargs —— 由 build_chat_model 组装：model/api_key/base_url/
              timeout/max_tokens 来自 config.setting.llm，调用方可覆盖
              （如 temperature、failure_diagnoser 的更短 timeout）。
        """
        # 关闭 openai 客户端内部重试，统一由网关管理，避免重试叠乘
        kwargs.setdefault("max_retries", 0)
        super().__init__(**kwargs)
        self._fallback_clients = None

    def set_user_id(self, user_id: Optional[int]) -> None:
        """设置当前请求的用户 id（供 record_usage 计量用户维度）。"""
        self._user_id = user_id

    def _resolve_user_id(self) -> Optional[int]:
        """优先取实例属性（agent 链路，跨 next() 稳定），否则取线程局部（context/title）。"""
        if self._user_id is not None:
            return self._user_id
        return get_current_user_id()

    def _get_fallback_clients(self) -> List[ChatOpenAI]:
        """惰性构建并缓存降级模型客户端列表（与主模型同密钥/同 base_url，仅模型名不同）。

        被谁调用：_generate / _stream 组装候选链 [self] + 降级客户端。
        返回：List[ChatOpenAI]，顺序即 fallback_model_names 的尝试顺序；
              空列表表示不降级。model_kwargs（如 json 模式）原样透传给降级模型。
        """
        if self._fallback_clients is None:
            fallback_kwargs = {}
            if self.model_kwargs:
                fallback_kwargs["model_kwargs"] = dict(self.model_kwargs)
            self._fallback_clients = [
                ChatOpenAI(
                    model=name,
                    api_key=self.openai_api_key,
                    base_url=self.openai_api_base,
                    temperature=self.temperature,
                    timeout=self.request_timeout,
                    max_retries=0,
                    **fallback_kwargs,
                )
                for name in self.fallback_model_names
            ]
        return self._fallback_clients

    def _invoke_with_retry(
        self,
        client: ChatOpenAI,
        model_name: str,
        messages: List[BaseMessage],
        stop: Optional[List[str]],
        run_manager: Optional[CallbackManagerForLLMRun],
        **kwargs: Any,
    ) -> Any:
        """单模型重试循环；返回 ChatResult 或最后一次异常（均为 None 时不会同时出现）。"""
        last_error: Optional[Exception] = None
        for attempt in range(self.gateway_max_retries + 1):
            try:
                result = ChatOpenAI._generate(
                    client, messages, stop=stop, run_manager=run_manager, **kwargs
                )
                if self._is_empty_result(result):
                    # 空响应（无内容且无工具调用）视为失败，可重试
                    raise _EmptyResponseError(f"{model_name} 返回空响应")
                return result
            except Exception as e:
                last_error = e
                if not _is_retryable(e) or attempt >= self.gateway_max_retries:
                    break
                delay = self.gateway_backoff_base * (2**attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "LLM[%s] 第%d次调用失败(%s)，%.2fs 后重试",
                    model_name, attempt + 1, e, delay,
                )
                time.sleep(delay)
        return last_error

    @staticmethod
    def _is_empty_result(result: ChatResult) -> bool:
        """判断响应是否为空（既无文本内容也无工具调用），空响应按瞬时故障处理。

        被谁调用：_invoke_with_retry 收到 ChatResult 后的校验。
        参数：result —— ChatOpenAI._generate 返回的 ChatResult。
        返回：bool，True 表示空响应（触发 _EmptyResponseError 走重试/降级）。
        """
        if not result.generations:
            return True
        message = result.generations[0].message
        return not (getattr(message, "content", None) or getattr(message, "tool_calls", None))

    @staticmethod
    def _extract_usage(result: ChatResult):
        """从非流式响应提取 token 用量（上游未返回时为 0，不做估算）"""
        usage = {}
        if result.generations:
            metadata = getattr(result.generations[0].message, "response_metadata", None) or {}
            usage = metadata.get("token_usage", {}) or {}
        if not usage:
            usage = (result.llm_output or {}).get("token_usage", {}) or {}
        return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)

    @staticmethod
    def _extract_finish_reason(result: ChatResult):
        """提取结束原因（stop/length/tool_calls 等），用于截断可观测。"""
        if not result.generations:
            return None
        gen_info = getattr(result.generations[0], "generation_info", None) or {}
        if gen_info.get("finish_reason"):
            return gen_info.get("finish_reason")
        metadata = getattr(result.generations[0].message, "response_metadata", None) or {}
        return metadata.get("finish_reason")

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """非流式统一入口：主模型 → 降级模型链逐个尝试，单模型内先重试。

        被谁调用：LangChain 内部（chain.invoke / bind_tools / ReAct Agent 等
        所有非流式调用最终经 ChatOpenAI 接口进入本方法），业务代码不直接调用。
        参数：messages —— 对话消息列表（来源调用方组装的 prompt/历史）；
              stop —— 停止词；run_manager —— 回调管理器；**kwargs —— 透传参数。
        返回：ChatResult（首个成功模型的响应），并经 core.usage.record_usage
              记录 token 用量（用户维度）。
        异常：致命错误或全链（含重试）失败时抛 LLMUnavailableError，由调用方兜底。
        """
        # 候选链 = 主模型(self) + 降级模型。chat 输出同为文本故可跨模型降级；
        # embedding 侧禁止此操作（换模型改维度致检索失效），见 embedding/embedding_model.py
        candidates = [self] + self._get_fallback_clients()
        chain_errors: List[str] = []

        for client in candidates:
            model_name = client.model_name
            outcome = self._invoke_with_retry(
                client, model_name, messages, stop, run_manager, **kwargs
            )
            if isinstance(outcome, ChatResult):
                if candidates.index(client) > 0:
                    logger.warning("LLM 主模型不可用，已降级至 %s 成功", model_name)
                finish_reason = self._extract_finish_reason(outcome)
                if finish_reason == "length":
                    logger.warning(
                        "LLM[%s] 输出因 max_tokens 截断(finish_reason=length)，回答可能不完整",
                        model_name,
                    )
                prompt_tokens, completion_tokens = self._extract_usage(outcome)
                record_usage(model_name, prompt_tokens, completion_tokens, user_id=self._resolve_user_id())
                return outcome
            error = outcome
            if error is None:
                break
            if not _is_retryable(error):
                # 认证/参数类致命错误：换模型无意义，直接失败
                logger.error("LLM[%s] 致命错误，跳过降级链: %s", model_name, error)
                raise LLMUnavailableError(f"模型服务不可用: {error}") from error
            chain_errors.append(f"{model_name}: {error}")

        detail = "; ".join(chain_errors) or "未知错误"
        raise LLMUnavailableError(f"主模型与全部降级模型均不可用（{detail}）")

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ):
        """
        流式生成：与 _generate 同源的重试/降级策略，但受流式语义约束——
        - 首 chunk 之前失败：客户端未收到任何字节，可安全重试/换模型；
        - 首 chunk 之后失败：流已不可重放，如实中断（不静默重试造成内容重复）；
        - token 用量从最后一个 chunk 提取（上游支持时），否则仅累计请求次数。
        """
        candidates = [self] + self._get_fallback_clients()
        chain_errors: List[str] = []

        for client in candidates:
            model_name = client.model_name
            error: Optional[Exception] = None
            for attempt in range(self.gateway_max_retries + 1):
                try:
                    iterator = ChatOpenAI._stream(
                        client, messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                    first_chunk = next(iterator)
                except StopIteration:
                    error = _EmptyResponseError(f"{model_name} 返回空流")
                except Exception as e:
                    error = e
                else:
                    # 首 chunk 已产生：从此不可重试，透传剩余流
                    yield first_chunk
                    last_chunk = first_chunk
                    try:
                        for last_chunk in iterator:
                            yield last_chunk
                    except Exception as e:
                        logger.error("LLM[%s] 流式输出中断: %s", model_name, e)
                    metadata = getattr(getattr(last_chunk, "message", None), "response_metadata", None) or {}
                    token_usage = metadata.get("token_usage", {}) or {}
                    if metadata.get("finish_reason") == "length":
                        logger.warning(
                            "LLM[%s] 流式输出因 max_tokens 截断(finish_reason=length)，回答可能不完整",
                            model_name,
                        )
                    record_usage(
                        model_name,
                        int(token_usage.get("prompt_tokens", 0) or 0),
                        int(token_usage.get("completion_tokens", 0) or 0),
                        user_id=self._resolve_user_id(),
                    )
                    return

                # 首 chunk 前失败：瞬时错误退避重试，致命错误快速失败
                if not _is_retryable(error):
                    logger.error("LLM[%s] 流式致命错误，跳过降级链: %s", model_name, error)
                    raise LLMUnavailableError(f"模型服务不可用: {error}") from error
                if attempt >= self.gateway_max_retries:
                    break
                delay = self.gateway_backoff_base * (2**attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "LLM[%s] 第%d次流式调用失败(%s)，%.2fs 后重试",
                    model_name, attempt + 1, error, delay,
                )
                time.sleep(delay)
            chain_errors.append(f"{model_name}: {error}")

        detail = "; ".join(chain_errors) or "未知错误"
        raise LLMUnavailableError(f"主模型与全部降级模型均不可用（{detail}）")


def build_chat_model(**overrides: Any) -> LLMGateway:
    """全仓统一的 LLM 构造工厂：默认值来自配置，可按调用点覆盖。

    功能：把 config/setting.py 的 llm 单例（LLMConfig，值来自
    env/qianwen_config.env）映射为 LLMGateway 参数——
    model=llm.MODEL（env: model，默认 default_model）、
    api_key=llm.API_KEY（env: api_key，【密钥类字段】仅来自本地 env、禁止入库）、
    base_url=llm.BASE_URL（env: base_url，DashScope 兼容模式地址）、
    timeout=llm.TIMEOUT_SECONDS、max_tokens=llm.MAX_TOKENS、
    fallback_model_names=llm.FALLBACK_MODELS（降级模型链）、
    gateway_max_retries=llm.MAX_RETRIES（重试次数）、
    gateway_backoff_base=llm.BACKOFF_BASE_SECONDS（退避基数）。
    被谁调用：model_llm/llm.py 的 LLM 基类（llm_business 全部提示词类）；
    app/domain/agents/base_agent.py、chat_agent.py、rag_agent.py、file_agent.py、
    summary_agent.py、verifier.py、failure_diagnoser.py；
    app/application/files/preference_service.py、app/domain/memory/profile_service.py、
    app/domain/memory/context_app.domain.memory.py、app/application/chat/context.py。
    参数：**overrides —— 调用点覆盖项（如 temperature=self.temperature、
          failure_diagnoser 的更短 timeout），优先级高于配置默认值。
    返回：LLMGateway 实例（ChatOpenAI  drop-in 替代），去向各 Agent/chain
          的 invoke / stream；全链不可用时其调用抛 LLMUnavailableError。
    """
    kwargs: dict = dict(
        model=llm.MODEL,
        api_key=llm.API_KEY,
        base_url=llm.BASE_URL,
        timeout=llm.TIMEOUT_SECONDS,
        max_tokens=llm.MAX_TOKENS,
        fallback_model_names=llm.FALLBACK_MODELS,
        gateway_max_retries=llm.MAX_RETRIES,
        gateway_backoff_base=llm.BACKOFF_BASE_SECONDS,
    )
    kwargs.update(overrides)
    return LLMGateway(**kwargs)
