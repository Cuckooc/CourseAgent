"""
模块名：multi_agent.retrieval

作用：
统一知识库检索入口（供 RAGAgent / FileAgent 复用）——父子块两阶段检索 + 重排序精排。

检索策略（Small-to-Big + ReRank）：
1. 粗检（向量召回）：只对【子块】做 embedding 相似度检索。子块短、语义聚焦，命中率高，
   不需要每次都对整篇详细内容做细粒度全量向量检索；
2. 父块取回：命中子块按 parent_id 去重，批量取回对应【父块】完整内容（详细信息），
   一个父块无论命中多少子块只保留一个候选（取最小距离）；
3. 精排：候选父块（含旧数据 chunk）交给 DashScope gte-rerank cross-encoder 重排，
   取相关性最高的 top_k（默认 3）条作为模型回复依据；rerank 不可用时降级为向量距离排序。

可见范围：
- 持久化库：公共(public) + 当前用户私有(private)；
- 内存库：当前会话临时库(temp)，会话结束即释放；用户主动上传，粗检不设距离硬阈值。

旧数据兼容：历史入库的 chunk 没有 doc_level/parent_id，命中后作为独立候选直接进入精排池。

混合检索（v2）：在向量语义检索基础上增加关键词全文检索路径，
通过 Reciprocal Rank Fusion (RRF) 融合两路结果，提升精确匹配场景的召回率。

主要成员：
- retrieve_scoped：对外统一检索入口（持久库 + 会话临时库，混合检索+精排）；
- _query_chroma：底层 Chroma 查询（容忍 HNSW 孤儿向量脏数据）；
- _extract_keywords / _keyword_search / _doc_matches_scope：关键词
  全文检索路径（jieba 抽词 → $contains 匹配 → scope 后过滤）；
- _rrf_merge：多路结果 Reciprocal Rank Fusion 融合；
- _coarse_candidates / _fetch_parents：子块粗检与父块批量取回；
- _rerank：DashScope gte-rerank cross-encoder 精排（失败降级）；
- _is_active_chunk / _candidate_key：版本块过滤与候选去重键工具；
- 模块级常量 COARSE_K_*、RERANK_POOL、_KEYWORD_*、_RRF_K 等控制
  召回数量、精排成本与融合平滑度。

被谁使用（Grep 模块名结果）：
- multi_agent/rag_agent.py：RAGAgent.handle 以应用级共享持久库为 db；
- multi_agent/file_agent.py：FileAgent.handle 以 PDF 演示库（可 None）
  + 会话临时库检索；
- tools/business/knowledge_business.py：function calling 新工具链的
  knowledge_search / session_file_search 原样复用本算法；
- scripts/maintenance、scripts/dev/debug：运维/排障脚本直接调用。

向量库来源：
- 持久库 db 形参：由 service/vector_store.get_persistent_db 统一持有的
  应用级共享 Chroma（RAGAgent.build_shared_db 封装；dao/knowledge 同用）；
- 会话临时库：service/temp_knowledge_store.get_temp_store 按
  user_id+session_id 懒加载（上传文件入库由 service/file_service 写入）；
- embedding 与 scope 过滤构造来自 embedding.text_embedding。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document

from embedding.text_embedding import MAX_L2_DISTANCE, build_scope_filter, get_embedding
from service.temp_knowledge_store import get_temp_store

logger = logging.getLogger(__name__)

# 粗检召回数量（持久库 / 临时库）：召回放宽，由后续 rerank 精排把关
COARSE_K_PERSISTENT = 12
COARSE_K_TEMP = 10
# 进入 rerank 的候选父块上限（控制 cross-encoder 调用成本）
RERANK_POOL = 5
# 子块粗检距离阈值：比旧阈值(1.15)适度放宽以提升召回，噪音由 rerank 剔除
CHILD_COARSE_MAX_DISTANCE = 1.35

_RERANK_MODELS = ("gte-rerank-v2", "gte-rerank")

# 关键词检索参数
_KEYWORD_TOPK = 2          # jieba 提取的关键词数上限（控制全文检索查询次数）
_KEYWORD_PER_QUERY_K = 6   # 每个关键词全文检索的召回数
_RRF_K = 60               # RRF 融合常数（标准值，越大越平滑）

# 关键词检索降级阈值：向量召回父块数 >= 此值时跳过关键词全文检索，
# 避免 2-3 次 $contains 全扫描拖慢查询（仅在向量召回不足时兜底）
_KEYWORD_SKIP_THRESHOLD = 3


def _is_active_chunk(meta: Optional[dict]) -> bool:
    """旧版本块（is_latest=False）排除；无该字段的历史块与 is_latest=True 均保留。"""
    return (meta or {}).get("is_latest") is not False


def _query_chroma(
    db,
    query: str,
    k: int,
    where: Optional[dict] = None,
    where_document: Optional[dict] = None,
) -> List[Tuple[Document, float]]:
    """底层 Chroma 向量查询（单 query）。

    不使用 langchain 的 similarity_search(_with_score)：它不向下透传 include，
    且无条件用结果构造 Document，一旦 HNSW 索引残留“孤儿向量”（embedding 仍在、
    document/metadata 已缺失，历史写入中断或跨版本升级可致），整次查询会因
    page_content=None 抛 ValidationError，使整路检索静默失效。
    此处显式 include 并跳过 document 缺失的命中，保证检索链路对脏数据健壮。
    """
    params = dict(
        query_embeddings=[get_embedding().embed_query(query)],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    if where is not None:
        params["where"] = where
    if where_document is not None:
        params["where_document"] = where_document
    res = db._collection.query(**params)
    out: List[Tuple[Document, float]] = []
    docs_group = res.get("documents") or [[]]
    metas_group = res.get("metadatas") or [[]]
    dists_group = res.get("distances") or [[]]
    for content, meta, dist in zip(docs_group[0], metas_group[0], dists_group[0]):
        if not content:
            continue  # 孤儿向量：无文档内容，不可作为候选
        out.append((Document(page_content=content, metadata=meta or {}), float(dist)))
    return out


def _extract_keywords(query: str, topk: int = _KEYWORD_TOPK) -> List[str]:
    """jieba 分词提取关键词：中文 ≥2 字、英文 ≥1 字母、排除纯数字。"""
    import jieba.analyse

    words = jieba.analyse.extract_tags(query, topK=topk * 2, withWeight=False)
    result = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w.isdigit():
            continue
        if len(w) < 2 and not w.isascii():
            continue
        if len(w) == 1 and not w.isalpha():
            continue
        result.append(w)
        if len(result) >= topk:
            break
    return result


def _keyword_search(
    db,
    query: str,
    where: Optional[dict],
) -> List[Tuple[float, Document]]:
    """关键词全文检索路径：jieba 提取关键词 → ChromaDB where_document $contains 匹配。

    每个关键词单独查询，结果按出现顺序去重。返回的 score 为占位值（由 RRF 融合时重排）。
    """
    keywords = _extract_keywords(query)
    if not keywords:
        return []

    seen_contents = set()
    results: List[Tuple[float, Document]] = []

    for kw in keywords:
        try:
            if where is not None:
                # 注意：$contains 属于 where_document 维度，不能放进 where
                # （旧实现把它塞进 where 的 $and，Chroma 校验必失败，关键词路径从未生效）
                hits = _query_chroma(
                    db, kw, _KEYWORD_PER_QUERY_K,
                    where=where, where_document={"$contains": kw},
                )
            else:
                hits = _query_chroma(
                    db, kw, _KEYWORD_PER_QUERY_K,
                    where_document={"$contains": kw},
                )
        except Exception:
            try:
                # 全文条件失败时退化为语义检索 + Python 侧 scope 后过滤
                hits = _query_chroma(db, kw, _KEYWORD_PER_QUERY_K)
                if where is not None:
                    hits = [(d, s) for d, s in hits if _doc_matches_scope(d, where)]
            except Exception as e:
                logger.warning("keyword search for '%s' failed: %s", kw, e)
                continue

        for doc, _score in hits:
            if not _is_active_chunk(doc.metadata):
                continue
            content_key = doc.page_content[:100]
            if content_key in seen_contents:
                continue
            seen_contents.add(content_key)
            results.append((0.0, doc))

    return results


def _doc_matches_scope(doc: Document, where: dict) -> bool:
    """简易 scope 后过滤：检查文档 metadata 是否满足 scope 条件。"""
    meta = doc.metadata or {}
    scope = meta.get("scope", "")
    if scope == "public":
        return True
    if scope == "private":
        doc_uid = meta.get("user_id")
        or_clauses = where.get("$or", [])
        for clause in or_clauses:
            and_clause = clause.get("$and", [])
            if not and_clause:
                continue
            if all(
                (c.get("scope") in (None, scope)) and (c.get("user_id") in (None, doc_uid))
                for c in and_clause
            ):
                return True
    return False


def _rrf_merge(
    *result_lists: List[Tuple[float, Document]],
) -> List[Tuple[float, Document]]:
    """Reciprocal Rank Fusion：融合多路检索结果，按 RRF 分数降序返回。

    每路结果按原始排名赋予 RRF 分数 Σ 1/(k + rank)，k=60 为标准值。
    """
    scores: Dict[tuple, float] = {}
    doc_map: Dict[tuple, Document] = {}

    for results in result_lists:
        for rank, (score, doc) in enumerate(results):
            key = _candidate_key(doc)
            rrf_score = 1.0 / (_RRF_K + rank + 1)
            scores[key] = scores.get(key, 0.0) + rrf_score
            if key not in doc_map:
                doc_map[key] = doc

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    return [(scores[k], doc_map[k]) for k in sorted_keys]


def _fetch_parents(db, parent_ids: List[str]) -> Dict[str, Document]:
    """按 parent_id 批量取回父块完整内容（详细信息）。"""
    if not parent_ids:
        return {}
    try:
        data = db._collection.get(ids=parent_ids, include=["documents", "metadatas"])
    except Exception as e:
        logger.warning("fetch parent chunks failed: %s", e)
        return {}
    out: Dict[str, Document] = {}
    for pid, content, meta in zip(
        data.get("ids") or [], data.get("documents") or [], data.get("metadatas") or []
    ):
        if not content or not _is_active_chunk(meta):
            continue  # 孤儿父块或已被新版本取代的旧版本块
        out[pid] = Document(page_content=content, metadata=meta or {})
    return out


def _coarse_candidates(
    db,
    query: str,
    where: Optional[dict],
    k: int,
    child_distance: Optional[float],
    legacy_distance: Optional[float],
) -> List[Tuple[float, Document]]:
    """第一阶段：子块向量粗检 → 映射父块；旧结构 chunk 直接作为候选。

    :param child_distance: 子块粗检 L2 距离上限（None 表示不限）
    :param legacy_distance: 旧 chunk/父块直接命中时的距离上限（None 表示不限）
    """
    try:
        hits = _query_chroma(db, query, k, where=where)
    except Exception as e:
        logger.warning("coarse vector retrieval failed: %s", e)
        return []

    best_parent_score: Dict[str, float] = {}
    legacy: List[Tuple[float, Document]] = []
    for doc, score in hits:
        score = float(score)
        meta = doc.metadata or {}
        if not _is_active_chunk(meta):
            continue  # 已被新版本取代的旧版本块不参与候选
        pid = meta.get("parent_id")
        if meta.get("doc_level") == "child" and pid:
            if child_distance is not None and score > child_distance:
                continue
            prev = best_parent_score.get(pid)
            if prev is None or score < prev:
                best_parent_score[pid] = score
        else:
            # 历史数据（无 doc_level）或父块直接命中：保留原 Document 直接进入候选
            if legacy_distance is not None and score > legacy_distance:
                continue
            legacy.append((score, doc))

    parents: List[Tuple[float, Document]] = []
    id2doc = _fetch_parents(db, list(best_parent_score.keys()))
    for pid, score in best_parent_score.items():
        parent_doc = id2doc.get(pid)
        if parent_doc is not None:
            parents.append((score, parent_doc))
    return parents + legacy


def _candidate_key(doc: Document) -> tuple:
    """生成候选文档的去重/融合键（供 RRF 合并时识别同一篇父块/旧 chunk）。

    被谁调用：_rrf_merge() 对每路结果的每个候选计算键，实现跨路去重累加。
    参数：doc——候选 Document（向量或关键词路径产出）。
    返回：tuple——父子结构父块返回 ("parent", parent_id)（同一父块的
          任意子块命中都合并为一票）；旧 chunk 返回
          ("legacy", source, 正文前 64 字)。
    """
    meta = doc.metadata or {}
    if meta.get("doc_level") == "parent" and meta.get("parent_id"):
        return ("parent", meta["parent_id"])
    return ("legacy", meta.get("source"), doc.page_content[:64])


def _rerank(query: str, docs: List[Document], top_n: int) -> Optional[List[Document]]:
    """第三阶段：cross-encoder 精排。失败返回 None 由调用方降级到向量距离排序。"""
    if not docs:
        return []
    try:
        import dashscope
    except Exception as e:  # pragma: no cover - dashscope 为既有强依赖，此处理论不可达
        logger.warning("dashscope unavailable, skip rerank: %s", e)
        return None

    texts = [d.page_content for d in docs]
    api_key = None
    try:
        import os

        api_key = os.getenv("api_key") or os.getenv("DASHSCOPE_API_KEY")
    except Exception:
        api_key = None
    for model in _RERANK_MODELS:
        try:
            rsp = dashscope.TextReRank.call(
                model=model,
                query=query,
                documents=texts,
                top_n=min(top_n, len(docs)),
                return_documents=False,
                api_key=api_key,
            )
            if rsp.status_code != 200:
                logger.warning("rerank %s non-200: %s", model, getattr(rsp, "message", ""))
                continue
            output = getattr(rsp, "output", None)
            results = output.get("results") if isinstance(output, dict) else getattr(output, "results", None)
            if not results:
                continue
            picked: List[Document] = []
            for r in results[:top_n]:
                idx = r.get("index") if isinstance(r, dict) else getattr(r, "index", None)
                if isinstance(idx, int) and 0 <= idx < len(docs):
                    picked.append(docs[idx])
            if picked:
                logger.info("rerank(%s) picked %d/%d candidates", model, len(picked), len(docs))
                return picked
        except Exception as e:
            logger.warning("rerank with model %s failed: %s", model, e)
    return None


def retrieve_scoped(
    db,
    query: str,
    top_k: int = 3,
    user_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> List[Document]:
    """混合检索 + 精排：向量语义检索 + 关键词全文检索 → RRF 融合 → rerank。

    向量路径：子块粗检 → 父块取回（Small-to-Big）；
    关键词路径：jieba 分词 → ChromaDB where_document 全文匹配；
    融合：Reciprocal Rank Fusion (RRF) 合并两路候选；
    精排：DashScope gte-rerank cross-encoder 取 top_k。

    被谁调用：multi_agent/rag_agent.py 的 RAGAgent.handle（db=共享持久库）、
              multi_agent/file_agent.py 的 FileAgent.handle（db=PDF 演示库
              或 None），以及 tools/business/knowledge_business.py 的新旧
              工具检索函数与运维/排障脚本。
    参数：
    - db：持久 Chroma 实例（来源：service/vector_store 共享库；None 时
      仅检索会话临时库）；
    - query：检索问题（来源：AnalysisAgent 经总线下发的用户 query）；
    - top_k：最终返回文档条数（可由 Agent 的 top_k/工具协议收敛后传入）；
    - user_id：JWT 用户 ID，构建 scope 过滤（公共 + 本人私有）；
    - session_id：会话号，非空且该会话存在临时库时合并检索会话临时库。
    返回：List[Document]——精排后的 top_k 个父块/旧 chunk（page_content
          为正文、metadata 带 scope/parent_id/source/output 等）；无 query、
          两路均无候选或库异常时返回空列表（调用方据此走"未检索到"降级）。
    """
    if not query:
        return []

    vector_scored: List[Tuple[float, Document]] = []
    keyword_scored: List[Tuple[float, Document]] = []

    # 1. 持久化库：向量语义检索（关键词检索在第 3 步按向量召回充分度决定是否执行）
    if db is not None:
        try:
            where = build_scope_filter(user_id)
            vector_scored.extend(
                _coarse_candidates(
                    db, query, where, COARSE_K_PERSISTENT,
                    child_distance=CHILD_COARSE_MAX_DISTANCE,
                    legacy_distance=MAX_L2_DISTANCE,
                )
            )
        except Exception as e:
            logger.warning("persistent knowledge retrieval failed: %s", e)

    # 2. 当前会话临时库向量检索（避免与传入 db 重复查询同一库）
    if session_id is not None and user_id is not None:
        try:
            store = get_temp_store()
            if store.has_session(user_id, session_id):
                temp_db = store.get_db(user_id, session_id)
                if db is None or temp_db is not db:
                    vector_scored.extend(
                        _coarse_candidates(
                            temp_db, query, None, COARSE_K_TEMP,
                            child_distance=None,
                            legacy_distance=None,
                        )
                    )
        except Exception as e:
            logger.warning("temp knowledge retrieval skipped: %s", e)

    # 2.5 关键词检索降级：向量召回父块数 >= 阈值时跳过关键词全文检索，
    # 避免多次 $contains 全扫描拖慢查询；仅在向量召回不足时作为兜底。
    distinct_parents = len({doc.metadata.get("parent_id") or id(doc) for _, doc in vector_scored})
    if distinct_parents < _KEYWORD_SKIP_THRESHOLD:
        if db is not None:
            try:
                where = build_scope_filter(user_id)
                keyword_scored.extend(_keyword_search(db, query, where))
            except Exception as e:
                logger.warning("persistent keyword search failed: %s", e)
        if session_id is not None and user_id is not None:
            try:
                store = get_temp_store()
                if store.has_session(user_id, session_id):
                    temp_db = store.get_db(user_id, session_id)
                    if db is None or temp_db is not db:
                        keyword_scored.extend(_keyword_search(temp_db, query, None))
            except Exception as e:
                logger.warning("temp keyword search skipped: %s", e)
    else:
        logger.debug(
            "keyword search skipped: vector recall=%d parents >= threshold=%d",
            distinct_parents, _KEYWORD_SKIP_THRESHOLD,
        )

    # 3. RRF 融合向量 + 关键词两路结果，按融合分数降序取候选池
    merged = _rrf_merge(vector_scored, keyword_scored)
    if not merged:
        return []

    pool = [doc for _, doc in merged[:RERANK_POOL]]
    if not pool:
        return []

    # 4. rerank 精排取相关性最高的 top_k；不可用时降级为 RRF 融合排序
    final_docs = _rerank(query, pool, top_k)
    if final_docs is None:
        final_docs = pool[:top_k]
    return final_docs
