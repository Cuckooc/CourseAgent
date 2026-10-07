"""
模块名：app.infrastructure.embeddings.embedding_model

作用：
    为 DashScope text-embedding-v2 提供「重试 + 查询缓存」包装。
    RetryingDashScopeEmbeddings 以组合方式持有 langchain DashScopeEmbeddings，
    对 embed_documents / embed_query 的远程调用做指数退避重试，并在进程内
    维护查询向量 LRU 缓存（相同问题免重复调用，单次约 300-500ms）。

主要成员：
    - RetryingDashScopeEmbeddings：带重试的 Embeddings 实现（接口与 langchain 一致）；
    - get_embedding：lru_cache 单例工厂，全进程返回同一包装实例；
    - _MAX_RETRIES / _BACKOFF_BASE / _FATAL_MARKERS / _QUERY_CACHE_SIZE：重试与缓存常量。

被谁使用（Grep "embedding.embedding_model" 确认）：
    - service/chat_service.py：get_embedding() 用于上下文相关性判定
      （embed_query 计算 query 与上下文的余弦相似度）。
    注意入库/检索主链路（service/vector_store.py、multi_agent/retrieval.py）使用
    embedding/text_embedding.py 的 get_embedding（裸客户端）；两者均锁定
    text-embedding-v2（1536 维），向量维度保持一致。

向量维度一致性约束：
    只重试、绝不换模型降级——换 embedding 模型会改变向量维度，导致既有 Chroma
    向量与查询向量无法比较（检索静默失效）。重试耗尽后直接抛原异常，由上层兜底。

密钥来源：
    api_key 取自 config/setting.py 的 LLMConfig.API_KEY（env/qianwen_config.env
    的 api_key，本地密钥文件禁止入库）。
"""
import logging
import random
import time
from collections import OrderedDict
from functools import lru_cache
from threading import Lock

import dashscope
from config.setting import LLMConfig
from langchain_community.embeddings import DashScopeEmbeddings

logger = logging.getLogger(__name__)
# 模块导入期把配置中的 api_key 写入 dashscope 全局（LLMConfig.API_KEY 来自 env/qianwen_config.env，禁止入库）
dashscope.api_key = LLMConfig.API_KEY

# 与 LLM 网关一致的重试参数
# 最大重试次数：取 LLMConfig.MAX_RETRIES（env: llm_max_retries，默认 2），max(0,...) 兜底防负值
_MAX_RETRIES = max(0, getattr(LLMConfig, "MAX_RETRIES", 2))
# 指数退避基数秒：取 LLMConfig.BACKOFF_BASE_SECONDS（默认 0.5），实际延迟 = base*2^attempt + 随机抖动
_BACKOFF_BASE = getattr(LLMConfig, "BACKOFF_BASE_SECONDS", 0.5)
# 致命错误（鉴权/参数问题）不重试，快速失败
_FATAL_MARKERS = ("401", "403", "invalid api key", "unauthorized", "access denied")

# 查询 embedding LRU 缓存：重复/相似问题免远程调用（单次约 300-500ms）
# 容量上限 256 条，超出后淘汰最久未使用项（OrderedDict + Lock 保证线程安全）
_QUERY_CACHE_SIZE = 256
_query_cache: "OrderedDict[str, list]" = OrderedDict()
_query_cache_lock = Lock()


def _is_fatal(exc: Exception) -> bool:
    """根据异常文本特征判断是否为致命错误（401/403/非法密钥等）。

    被谁调用：RetryingDashScopeEmbeddings._with_retry。
    参数：exc —— embedding 调用捕获的异常。
    返回：bool，True 表示不重试立即上抛（换模型/重试均无意义）。
    """
    text = str(exc).lower()
    return any(m in text for m in _FATAL_MARKERS)


class RetryingDashScopeEmbeddings:
    """
    DashScopeEmbeddings 的重试包装（组合模式，接口与 langchain Embeddings 一致）。

    与 chat 侧 LLMGateway 的差异：**只重试、不换模型降级**——
    换 embedding 模型会改变向量维度，导致既有 Chroma 向量与查询向量
    无法比较（检索静默失效），因此失败耗尽后直接抛出原异常。

    embedding 调用幂等，所有异常默认可重试（无法可靠区分 SDK 错误类型时，
    以字符串特征排除 401/403 类致命错误）。

    实例化位置：仅由本模块 get_embedding() 工厂创建（lru_cache 保证进程级单例），
    被 service/chat_service.py 经 get_embedding() 持有使用；业务代码不直接 new。
    内嵌的 DashScopeEmbeddings 在 __init__ 中以 text-embedding-v2 + 全局
    dashscope.api_key 构造；关键产出（1536 维 float 向量）去向 chat_service 的
    余弦相似度计算，入库侧向量则写入 service/vector_store.py 管理的 Chroma。
    """

    def __init__(self):
        """无参构造：模型名固定 text-embedding-v2，密钥取模块导入期设置的 dashscope.api_key。"""
        self._inner = DashScopeEmbeddings(
            model="text-embedding-v2",
            dashscope_api_key=dashscope.api_key,
        )

    def _with_retry(self, func, *args, **kwargs):
        """对单次 embedding 远程调用执行「指数退避 + 抖动」重试循环。

        被谁调用：本类 embed_documents / embed_query。
        参数：func —— 内嵌客户端的可重试方法（embed_documents/embed_query）；
              args/kwargs —— 透传给该方法的文本参数。
        返回：成功时返回 func 的原始结果（向量或向量批次）。
        异常：致命错误（_is_fatal）立即抛出；其余异常在 _MAX_RETRIES 次退避
              重试耗尽后抛出最后一次异常——无降级模型（维度约束），由调用方兜底。
        """
        last_exc: Exception = RuntimeError("unreachable")
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if _is_fatal(e):
                    # 鉴权/密钥类致命错误：重试无意义，快速失败
                    logger.error("embedding 致命错误，快速失败: %s", e)
                    raise
                if attempt < _MAX_RETRIES:
                    # 指数退避：0.5*2^attempt 秒基础延迟 + 0~0.25s 随机抖动，避免请求尖峰同步重试
                    delay = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.25)
                    logger.warning(
                        "embedding 调用失败(第 %d 次)，%.2fs 后重试: %s",
                        attempt + 1,
                        delay,
                        e,
                    )
                    time.sleep(delay)
        # 重试耗尽：记录错误并上抛最后一次异常（不换模型，保证向量维度一致）
        logger.error("embedding 重试耗尽(%d 次): %s", _MAX_RETRIES + 1, last_exc)
        raise last_exc

    def embed_documents(self, texts):
        """批量文本向量化（接口与 DashScopeEmbeddings 一致）。

        被谁调用：Chroma 建库/写入路径以 embedding_function 身份间接调用
        （service/vector_store.py 的批量入库）。
        参数：texts —— str 列表（分块文本）。
        返回：List[List[float]]，每个 1536 维，顺序与 texts 对齐，去向 Chroma 写入。
        异常：见 _with_retry（致命错误即抛；其余重试耗尽后抛最后异常）。
        """
        return self._with_retry(self._inner.embed_documents, texts)

    def embed_query(self, text):
        """单条查询向量化（接口与 DashScopeEmbeddings 一致）。

        带 LRU 缓存：相同文本直接命中缓存，避免重复远程调用。

        被谁调用：service/chat_service.py 上下文相关性判定（query 与候选上下文
        各向量化后算余弦相似度）；Chroma 检索时亦以 embedding_function 身份调用。
        参数：text —— 查询字符串（用户问题/上下文文本）。
        返回：List[float]，1536 维向量；命中 _query_cache 时直接返回缓存引用。
        """
        # 缓存键：去除首尾空白；非 str 时退化为 str()，保证键可哈希且稳定
        key = text.strip() if isinstance(text, str) else str(text)
        with _query_cache_lock:
            # 命中：移到队尾（标记最近使用）后直接返回，零远程调用
            cached = _query_cache.get(key)
            if cached is not None:
                _query_cache.move_to_end(key)
                return cached
        # 未命中：远程向量化（含重试）
        emb = self._with_retry(self._inner.embed_query, text)
        with _query_cache_lock:
            # 回填缓存并淘汰最久未使用项，控制内存上限
            _query_cache[key] = emb
            _query_cache.move_to_end(key)
            if len(_query_cache) > _QUERY_CACHE_SIZE:
                _query_cache.popitem(last=False)
        return emb


@lru_cache(maxsize=1)
def get_embedding():
    """获取带重试的 DashScopeEmbeddings（调用方接口零改动）。

    功能：返回 RetryingDashScopeEmbeddings 进程级单例（lru_cache 保证唯一实例）。
    被谁调用：service/chat_service.py（ChatService 初始化时持有，用于上下文相似度）。
    返回：RetryingDashScopeEmbeddings 实例；其 embed_query/embed_documents 产出的
          1536 维向量须与 Chroma 存量向量（text-embedding-v2）维度一致。
    """
    logger.info("get_embedding start")
    return RetryingDashScopeEmbeddings()
