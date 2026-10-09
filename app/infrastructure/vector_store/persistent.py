"""
向量库统一网关：持久化 Chroma 单例 + 进程内写锁 + 父子块入库。

并发模型（多文件并行上传）：
- 锁外（可并行）：文件解析/脱敏/切分、DashScope embedding 远程调用（每个文件独立批次）；
- 锁内（串行）：collection.add 本地写入。Chroma 底层 SQLite + hnswlib 不支持多客户端/多线程
  并发写同一目录，串行化写入杜绝 "database is locked" 与向量/元数据错配；
- 文件隔离：每次写入的 ids/documents/metadatas 均由单个文件的父子块独立构建，
  锁只改变写入时序，不混合不同文件的数据。

父块向量不额外调用 embedding API（长文本还可能超模型单次长度上限）：
父向量 = 其子块向量的均值并 L2 归一化，语义上代表父块；父块正常只按 id 回查，
该向量仅用于旧链路/兜底相似度检索。

主要成员：
- PersistentVectorStore：持久化 Chroma 单例持有者与进程写锁（模块级 _store）。
- get_persistent_db()/persistent_lock()：取共享持久库与其写锁。
- flush_persistent_index()/_flush_collection_index()：HNSW 索引强制并盘落盘。
- add_parent_child()：单文件父子块入库（embedding 锁外、写入锁内）。
- find_max_similarity()：去重扫描（新块对库内已有块的最大 cosine 相似度）。
- replace_document()/add_new_version()：内容变更的两种替换策略。
- _l2_normalize()/_cosine_from_l2()/_embed_in_batches()：归一化/距离换算/
  批量并发 embedding 等工具函数。

被谁使用：
- app/application/files/file_service.py：上传持久库入库、去重扫描与替换策略；
- app/application/review/review_service.py：审核通过后入库与 flush；
- app/infrastructure/vector_store/temp_store.py：临时库复用 add_parent_child 写入逻辑；
- dao/knowledge.py：删除文档向量块时取共享库与写锁；
- app/domain/agents/rag_agent.py：RAG 检索共享持久库；
- core/purge_scheduler.py：旧版本清理；control/app.py 与
  app/api/v1/files.py：服务关闭/批次结束时 flush 索引；
- app/domain/tools/business/knowledge_business.py：知识库检索工具。
"""
from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from langchain_chroma import Chroma

from app.infrastructure.embeddings.parent_child import (
    build_parent_child_documents,
    make_file_id,
    split_parent_child,
)
from app.infrastructure.embeddings.text_embedding import get_embedding
from core.config import settings

# 模块级日志器：向量写入/替换/flush 落盘与异常告警日志走该 logger
logger = logging.getLogger(__name__)

# 持久化 Chroma 落盘目录：统一取配置 settings.CHROMA_DIR（默认 storage/chromadb，
# 可用环境变量 chroma_dir 覆盖），取代历史上硬编码的 <项目根>/chromadb_data。
_PERSIST_PATH = str(settings.CHROMA_DIR)

# 单次发给 embedding API 的子块上限（DashScope text-embedding-v2 批量约束，保守值）
_EMBED_BATCH = 25

# embedding 批内并发线程数（单大文件的多个批次并行发请求，受 DashScope QPS 限制取 4）
_EMBED_CONCURRENCY = 4
_embed_executor = ThreadPoolExecutor(max_workers=_EMBED_CONCURRENCY, thread_name_prefix="embed-batch")

# HNSW 索引参数：chromadb 0.5 默认 search_ef=10，在万级点库上近似搜索探索不足，
# 后增量插入的点群会形成不可达“孤岛”（k 较小时零召回，k 很大才能搜到）。
# M/construction_ef 提升图连通性，search_ef 提升查询时的探索宽度（须 >= 粗检 k）。
# 默认 sync_threshold=1000：不足 1000 条的尾部索引更新只驻留内存，进程退出即丢失
# （向量/元数据在 SQLite 不丢，但 HNSW 图缺索引会导致重启后最近上传零召回），
# 调小到 100 缩小损失窗口，并由 flush_persistent_index() 在服务关闭时兜底落盘。
_HNSW_CONFIG = {
    "hnsw:space": "l2",
    "hnsw:M": 32,
    "hnsw:construction_ef": 200,
    "hnsw:search_ef": 200,
    "hnsw:batch_size": 100,
    "hnsw:sync_threshold": 100,
}


class PersistentVectorStore:
    """持久化向量库（chromadb_data）的进程级单例与写锁。

    类作用：懒加载并持有唯一的 langchain Chroma 持久化实例（集合元数据
            采用自定义 HNSW 参数），并提供一把进程级 RLock 串行化全部
            向量写入/删除。多文件并行上传时锁外并行做 embedding，仅
            collection.add/delete/update 在锁内执行。

    实例化位置：不在业务代码中直接 new；模块加载时创建唯一全局实例
            _store（见本模块模块级单例），业务代码一律经
            get_persistent_db()/persistent_lock() 访问。

    关键 self 属性：
    - _db：Chroma 持久库实例，首次 db() 调用时懒加载；被 RAGAgent 检索、
      上传入库、知识库删除、旧版本清理等所有持久库路径共用。
    - _lock：进程写锁 RLock，db() 双检创建时持有，写入路径经
      persistent_lock() 获取后在锁内操作 collection。
    """

    def __init__(self):
        # __init__ 无形参：持久化路径固定取模块常量 _PERSIST_PATH
        self._db: Optional[Chroma] = None
        # RLock 可重入：同一工作线程内嵌套获取写锁不会自死锁
        self._lock = threading.RLock()

    def db(self) -> Chroma:
        """双检锁懒加载并返回共享持久化 Chroma 实例。

        功能：_db 为空时持 _lock 二次确认后创建 Chroma（persist_directory
              = chromadb_data，embedding 函数取 embedding/text_embedding
              单例，集合元数据为 _HNSW_CONFIG）。
        被谁调用：模块级 get_persistent_db()。
        参数：无。
        返回：Chroma——全进程共享的持久化向量库；去向：上传入库/检索/
              删除/清理等所有持久库操作。
        """
        if self._db is None:
            with self._lock:
                if self._db is None:
                    self._db = Chroma(
                        persist_directory=_PERSIST_PATH,
                        embedding_function=get_embedding(),
                        collection_metadata=_HNSW_CONFIG,
                    )
        return self._db

    @property
    def lock(self) -> threading.RLock:
        """进程级向量写锁（只读属性，返回同一把 RLock）。

        被谁调用：模块级 persistent_lock()；写入路径（add_parent_child/
                  replace_document/add_new_version/DAO 删除/定时清理）
                  持此锁串行化 collection 操作。
        返回：threading.RLock——全局唯一写锁。
        """
        return self._lock


# 模块级全局单例：进程内唯一的持久化向量库持有者（导入即创建，库本身懒加载）
_store = PersistentVectorStore()


def get_persistent_db() -> Chroma:
    """应用级共享持久化向量库（RAGAgent/知识库管理/上传写入共用同一实例）。

    被谁调用：app/application/files/file_service.py、app/application/review/review_service.py、
              dao/knowledge.py、app/domain/agents/rag_agent.py、
              core/purge_scheduler.py、app/domain/tools/business/knowledge_business.py
              及 scripts/ 维护脚本。
    返回：Chroma——_store.db() 懒加载出的 chromadb_data 持久库实例。
    """
    return _store.db()


def persistent_lock() -> threading.RLock:
    """返回持久化向量库的进程级写锁。

    被谁调用：所有需要串行化 collection.add/delete/update 的写入路径
              （file_app/application/review/review_service/dao.knowledge/purge_scheduler）。
    返回：threading.RLock——与持久库绑定的全局写锁。
    """
    return _store.lock


def flush_persistent_index() -> None:
    """服务关闭时兜底：把残留在内存批次里的 HNSW 更新并入索引并落盘。

    功能：库尚未初始化则直接返回；否则持写锁以两轮等待方式调用
          _flush_collection_index，覆盖第一轮等待期间 chroma consumer
          又投递的在途记录。
    被谁调用：control/app.py 应用关闭钩子、app/api/v1/files.py
              上传批次结束后、app/application/review/review_service.py 审核入库后。
    参数：无。返回：无。数据去向：chromadb_data 的 HNSW 索引与 SQLite 落盘。
    """
    if _store._db is None:
        return
    with _store.lock:
        # 两轮：第二轮覆盖第一轮等待期间 consumer 又投递的在途记录
        _flush_collection_index(_store._db, wait_rounds=2)


def _flush_collection_index(
    db: Chroma,
    *,
    added_ids: Optional[List[str]] = None,
    deleted_ids: Optional[List[str]] = None,
    wait_rounds: int = 1,
) -> None:
    """把指定库已提交的向量变更可靠落盘（仅持久库生效；内存库静默跳过）。

    chromadb 0.5 写入链路是 producer/consumer 异步 apply，且持久段会把记录攒在
    _curr_batch（brute-force 内存区），攒满 sync_threshold 才并进 HNSW；_persist()
    只写已并入索引的部分。因此必须：
    1) 等本次 add/delete 的 id 全部被 consumer 处理到（图中或残批中）；
    2) 持 chroma 自身的写锁（与 consumer 线程互斥）把残批 _apply_batch 并入索引；
    3) 调 _persist() 落盘。

    功能：反射获取 chroma 内部 VectorReader segment；非持久化实现（无
          _persist）静默返回；有新增/删除 id 时最多等 10s 让 consumer
          处理完（settled 判定），随后按 wait_rounds 持 WriteRWLock
          反复 _apply_batch + _persist。
    被谁调用：flush_persistent_index()、replace_document()、
              add_new_version()；dao/knowledge.py 与 core/purge_scheduler.py
              也直接导入使用。
    参数：
    - db (Chroma)：目标库（持久库或会话库；会话库无 _persist 时跳过）。
    - added_ids (List[str]|None)：本次新增的向量 id（等待其被 consumer 处理）。
    - deleted_ids (List[str]|None)：本次删除的向量 id。
    - wait_rounds (int)：并批+落盘的重复轮数（默认 1，关闭兜底用 2）。
    返回：无。异常：反射/内部 API 调用失败时仅告警不抛出（落盘为兜底动作）。
    """
    import time

    try:
        from chromadb.segment import VectorReader
        from chromadb.segment.impl.vector.batch import Batch
        from chromadb.utils.read_write_lock import ReadRWLock, WriteRWLock

        col = db._collection
        segment = col._client._manager.get_segment(col.id, VectorReader)
        if not callable(getattr(segment, "_persist", None)):
            return  # 会话内存库等非持久化实现无需落盘

        added = list(added_ids or [])
        deleted = list(deleted_ids or [])

        def settled() -> bool:
            with ReadRWLock(segment._lock):
                batch = segment._curr_batch
                pending_writes = set(batch.get_written_ids()) if batch is not None else set()
                if any(i not in segment._id_to_label and i not in pending_writes
                       for i in added):
                    return False
                if any(i in segment._id_to_label
                       or (batch is not None and batch.is_deleted(i))
                       for i in deleted):
                    return False
                return True

        if added or deleted:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not settled():
                time.sleep(0.02)
            if not settled():
                logger.warning("hnsw drain timeout: added=%d deleted=%d",
                               len(added), len(deleted))

        for _ in range(max(1, wait_rounds)):
            with WriteRWLock(segment._lock):
                batch = segment._curr_batch
                if batch is not None and (batch.add_count or batch.delete_count):
                    segment._apply_batch(batch)  # 复刻 chroma 批满时的官方路径
                    segment._curr_batch = Batch()
                    segment._brute_force_index.clear()
                segment._persist()
            if wait_rounds > 1:
                time.sleep(0.1)
        logger.debug("hnsw index flushed: +%d -%d", len(added), len(deleted))
    except Exception:
        logger.warning("flush hnsw index failed", exc_info=True)


def _l2_normalize(vec: List[float]) -> List[float]:
    """L2 归一化向量（除以自身欧氏模长）。

    功能：把向量缩放到单位长度，使父块均值向量与子块向量处于同一度量空间；
          零向量（模长为 0）原样拷贝返回，避免除零。
    被谁调用：add_parent_child() 聚合父向量、app/application/files/file_service.py
              预聚合父向量时。
    参数：vec (List[float])——待归一化的向量（如某父块全部子向量的均值）。
    返回：List[float]——归一化后的同维向量。
    """
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else list(vec)


def _embed_in_batches(embedding_model, texts: List[str]) -> List[List[float]]:
    """批量 embedding：将 texts 按 _EMBED_BATCH 分批，多线程并发调用远程 API。

    功能：单批直接同步调用 embed_documents；多批时提交到 _embed_executor
          线程池（4 并发，受 DashScope QPS 约束）并发请求，再按原始批次
          顺序组装，保证返回向量下标与 texts 一一对应。
    被谁调用：add_parent_child()、app/application/files/file_service.py 去重前预计算。
    参数：
    - embedding_model：embedding 客户端（来源：embedding/text_embedding.get_embedding）。
    - texts (List[str])：待向量化文本（子块内容，来源：父子切分结果）。
    返回：List[List[float]]——与 texts 等长等序的向量列表；空输入返回 []。
    异常：任一批次 future.result() 失败时异常上抛（调用方在入库流程内统一处理）。
    """
    if not texts:
        return []
    batches = [texts[i : i + _EMBED_BATCH] for i in range(0, len(texts), _EMBED_BATCH)]
    if len(batches) == 1:
        return embedding_model.embed_documents(batches[0])
    # 并发执行各批次，按原始顺序组装结果
    results: List[Optional[List[List[float]]]] = [None] * len(batches)
    futures = {
        _embed_executor.submit(embedding_model.embed_documents, batch): idx
        for idx, batch in enumerate(batches)
    }
    for fut in futures:
        idx = futures[fut]
        results[idx] = fut.result()
    out: List[List[float]] = []
    for r in results:
        out.extend(r)
    return out


def add_parent_child(
    db: Chroma,
    lock: threading.RLock,
    *,
    source: str,
    scope: str,
    text: str,
    user_id: int = None,
    session_id: int = None,
    original_name: str = None,
    content_hash: str = None,
    version: int = 1,
    updated_at: float = None,
    is_latest: bool = True,
    precomputed_child_embs: Optional[List[List[float]]] = None,
) -> Tuple[int, int]:
    """把【单个文件】的脱敏文本以父子结构写入指定向量库（持久库或会话内存库）。

    功能：按 source 生成 file_id 并父子切分/构建 LangChain Document；
    embedding 计算发生在锁外（可被上传线程池并行调度），传入
    precomputed_child_embs（去重判定时已算）则直接复用以节省 token；
    父向量按 parent_id 聚合子向量均值并 L2 归一化（无子块时补零向量）；
    最后在 lock 内分父、子两批 collection.add 写入。本函数不逐文件
    flush，落盘时机由批次结束/服务关闭的 flush_persistent_index 统一兜底。

    被谁调用：app/application/files/file_service.py（全新文件入库）、
              app/application/review/review_service.py（审核通过入库）、
              app/infrastructure/vector_store/temp_store.py（会话临时库入库）。
    参数：
    - db (Chroma)：目标库（持久库 get_persistent_db 或会话临时库）。
    - lock (threading.RLock)：写锁（持久库传 persistent_lock()，临时库
      传 TempKnowledgeStore._write_lock）。
    - source (str)：文件物理路径，作为块 source 元数据与 file_id 输入。
    - scope (str)：public/private/temp 范围标记，写入元数据供检索过滤。
    - text (str)：已脱敏文件文本（调用方保证先经 mask_text）。
    - user_id/session_id：归属用户与会话（temp 必填；私有库隔离用）。
    - original_name (str|None)：用户上传原始文件名。
    - content_hash (str|None)：内容指纹（去重/版本元数据）。
    - version (int)/updated_at (float|None)/is_latest (bool)：版本元数据。
    - precomputed_child_embs：锁外预计算的子块向量（须与子块数一致）。
    返回：Tuple[int, int]——(父块数, 子块数)；切分为空时 (0, 0)。
    数据去向：db 对应 chroma 集合（chromadb_data 或 uploads/temp 目录）。
    """
    file_id = make_file_id(source)
    pairs = split_parent_child(text)
    if not pairs:
        return 0, 0
    parents, children = build_parent_child_documents(
        pairs,
        source=source,
        scope=scope,
        user_id=user_id,
        session_id=session_id,
        original_name=original_name,
        file_id=file_id,
        content_hash=content_hash,
        version=version,
        updated_at=updated_at,
        is_latest=is_latest,
    )

    # ---- 锁外：子块 embedding（远程 API，主要耗时，可并行） ----
    child_ids = [cid for cid, _, _ in children]
    child_texts = [doc.page_content for _, _, doc in children]
    child_metas = [doc.metadata for _, _, doc in children]
    if precomputed_child_embs is not None and len(precomputed_child_embs) == len(child_texts):
        child_embs = precomputed_child_embs
    else:
        embedding_model = get_embedding()
        child_embs = _embed_in_batches(embedding_model, child_texts)

    # 父向量 = 子块向量均值归一化（按 parent_id 聚合），零额外 API 调用
    sums: dict = {}
    counts: dict = {}
    for (_cid, pid, _doc), emb in zip(children, child_embs):
        acc = sums.get(pid)
        if acc is None:
            acc = [0.0] * len(emb)
            sums[pid] = acc
            counts[pid] = 0
        for i, v in enumerate(emb):
            acc[i] += v
        counts[pid] += 1
    parent_ids = [pid for pid, _ in parents]
    parent_embs = []
    for pid in parent_ids:
        acc = sums.get(pid)
        if acc:
            parent_embs.append(_l2_normalize([v / counts[pid] for v in acc]))
        else:
            parent_embs.append(None)
    # 理论上每个父块至少有 1 个子块；极端兜底（无子块）：补一个零向量占位
    dim = len(child_embs[0]) if child_embs else 1536
    parent_embs = [emb if emb is not None else [0.0] * dim for emb in parent_embs]

    # ---- 锁内：串行本地写入（父、子两批，元数据自描述所属文件） ----
    with lock:
        col = db._collection
        col.add(
            ids=parent_ids,
            embeddings=parent_embs,
            documents=[doc.page_content for _, doc in parents],
            metadatas=[doc.metadata for _, doc in parents],
        )
        if child_ids:
            col.add(ids=child_ids, embeddings=child_embs, documents=child_texts, metadatas=child_metas)
        # 注意：此处不再每文件 flush。flush 时机合并到上传批次结束后统一执行
        # （file_control 在所有文件 add 完毕后调用 flush_persistent_index），
        # 减少锁占用与 SQLite 写次数；服务关闭时 flush_persistent_index() 兜底落盘。
    logger.info(
        "parent-child vectors added: file_id=%s parents=%d children=%d scope=%s version=%s",
        file_id, len(parents), len(children), scope, version,
    )
    return len(parents), len(children)


def _cosine_from_l2(l2_distance: float) -> float:
    """L2 空间下，归一化向量的 cosine 相似度 = 1 - L2² / 2。

    功能：chroma hnsw:space=l2 返回的是 L2 距离，本函数换算为业务去重
          阈值使用的 cosine 相似度（向量均已 L2 归一化，等式成立）。
    被谁调用：find_max_similarity()；tests 中亦直接导入校验。
    参数：l2_distance (float)——chroma 返回的 L2 距离（非负）。
    返回：float——cosine 相似度，理论范围 [-1, 1]（实际近邻多在 0~1）。
    """
    return 1.0 - (l2_distance * l2_distance) / 2.0


def find_max_similarity(
    db: Chroma,
    query_embeddings: List[List[float]],
    where: Optional[dict] = None,
) -> Tuple[float, Optional[str]]:
    """查询新块与库中已有块的最大 cosine 相似度。

    功能：批量对每个新子块向量在库内（带 where 过滤）取 top-1 近邻，把
          chroma 的 L2 距离经 _cosine_from_l2 换算为 cosine，取全局最大
          值及其命中块的 source 元数据；查询异常时告警并按无匹配返回。
    被谁调用：app/application/files/file_service.py 的 FileService._check_duplicate
              （full 与 filename 两种去重策略均用）。
    参数：
    - db (Chroma)：持久化向量库（来源：get_persistent_db）。
    - query_embeddings：新文件所有子块的 embedding 列表（锁外预计算）。
    - where (dict|None)：chroma 过滤条件（scope + user_id 隔离，
      来源：_build_dedup_where）。
    返回：Tuple[float, str|None]——(max_similarity, matched_source)，
          无匹配/空输入/异常时 similarity=0、source=None；去向：
          _check_duplicate 与去重阈值比较决定 skip/replace/new。
    """
    if not query_embeddings:
        return 0.0, None
    col = db._collection
    max_sim = 0.0
    matched_source = None
    try:
        # 批量查询：每个 query 取 top-1
        results = col.query(
            query_embeddings=query_embeddings,
            n_results=1,
            where=where,
            include=["metadatas", "distances"],
        )
        distances_list = results.get("distances") or []
        metadatas_list = results.get("metadatas") or []
        for dists, metas in zip(distances_list, metadatas_list):
            if not dists:
                continue
            sim = _cosine_from_l2(dists[0])
            if sim > max_sim:
                max_sim = sim
                meta = (metas or [{}])[0] or {}
                matched_source = meta.get("source")
    except Exception as e:
        logger.warning("find_max_similarity query failed: %s", e)
    return max_sim, matched_source


def replace_document(
    db: Chroma,
    lock: threading.RLock,
    *,
    old_source: str,
    new_ids: List[str],
    new_embs: List[List[float]],
    new_docs: List[str],
    new_metas: List[dict],
) -> None:
    """策略A：先删后增 + 回滚（事务性替换旧文件）。

    1. 备份旧数据；2. 删除旧块；3. 添加新块；4. add 失败则回滚恢复旧数据。

    功能：在同一把写锁内按 source 取旧块全量备份（id/文档/embedding/
          元数据）→ 删除旧块 → 写入新块；写入异常时用备份回滚并把异常
          上抛。成功后 flush 索引（带 added/deleted id 等待落盘），并在
          锁外删除旧物理文件（删除失败仅告警）。
    被谁调用：app/application/files/file_service.py 的 process_file（UPDATE_STRATEGY
              = replace 分支）。
    参数：
    - db (Chroma)/lock (RLock)：持久库与其写锁。
    - old_source (str)：被替换旧文件的 source 路径（删旧块/旧物理文件用）。
    - new_ids/new_embs/new_docs/new_metas：新版本文件的全部父子块 id、
      向量、文本与元数据（来源：file_service 预构建）。
    返回：无。异常：新块 add 失败时先回滚再 raise，保证旧数据不丢。
    """
    with lock:
        col = db._collection
        # 1. 备份旧数据（用于回滚）
        old = col.get(
            where={"source": old_source},
            include=["documents", "embeddings", "metadatas"],
        )
        old_ids = old.get("ids") or []
        # 2. 删除旧块
        if old_ids:
            col.delete(ids=old_ids)
        # 3. 添加新块
        try:
            col.add(ids=new_ids, embeddings=new_embs, documents=new_docs, metadatas=new_metas)
        except Exception:
            # 4. 回滚：恢复旧数据
            if old_ids:
                col.add(
                    ids=old_ids,
                    embeddings=old.get("embeddings") or [],
                    documents=old.get("documents") or [],
                    metadatas=old.get("metadatas") or [],
                )
            raise
        # 5. flush
        _flush_collection_index(db, deleted_ids=old_ids, added_ids=new_ids)
    # 6. 清理旧物理文件（旧向量块已删除，物理文件不再被引用）
    if old_ids:
        try:
            from pathlib import Path
            old_path = Path(old_source)
            if old_path.exists():
                old_path.unlink()
                logger.info("replace_document: removed old file %s", old_source)
        except Exception as e:
            logger.warning("replace_document: failed to remove old file %s: %s", old_source, e)
    logger.info(
        "replace_document: old_source=%s old_chunks=%d new_chunks=%d",
        old_source, len(old_ids), len(new_ids),
    )


def add_new_version(
    db: Chroma,
    lock: threading.RLock,
    *,
    old_file_id: str,
    new_ids: List[str],
    new_embs: List[List[float]],
    new_docs: List[str],
    new_metas: List[dict],
    superseded_at: float,
) -> None:
    """策略B：版本标记（新增 is_latest=true 版本，旧版本标记 is_latest=false）。

    旧版本的 superseded_at 记录被替换时间，供三年过期清理判定。

    功能：写锁内先 add 新版本父子块，再按 file_id + is_latest=True 取出
          当前最新版本全部块，用 col.update 批量置 is_latest=False 并写
          superseded_at，最后 flush 索引。旧版本向量保留可回溯，由
          core/purge_scheduler.py 按保留期清理。
    被谁调用：app/application/files/file_service.py 的 process_file（UPDATE_STRATEGY
              = version 分支，默认策略）。
    参数：
    - db (Chroma)/lock (RLock)：持久库与其写锁。
    - old_file_id (str)：被替换文件的 file_id（新旧版本共享，定位旧块用）。
    - new_ids/new_embs/new_docs/new_metas：新版本全部父子块数据
      （来源：file_service 预构建，元数据已带 version/updated_at）。
    - superseded_at (float)：替换时间戳，写入旧版本元数据供过期清理判定。
    返回：无。数据去向：chromadb_data（新版本可检索，旧版本标记保留）。
    """
    with lock:
        col = db._collection
        # 1. 添加新版本（元数据自带 is_latest=True，成为检索命中版本）
        col.add(ids=new_ids, embeddings=new_embs, documents=new_docs, metadatas=new_metas)
        # 2. 旧版本标记为非最新 + 记录替换时间
        old = col.get(where={"$and": [{"file_id": old_file_id}, {"is_latest": True}]}, include=[])
        old_ids = old.get("ids") or []
        if old_ids:
            col.update(
                ids=old_ids,
                metadatas=[{"is_latest": False, "superseded_at": superseded_at} for _ in old_ids],
            )
        # 3. flush
        _flush_collection_index(db, added_ids=new_ids)
    logger.info(
        "add_new_version: old_file_id=%s superseded=%d new_chunks=%d",
        old_file_id, len(old_ids), len(new_ids),
    )
