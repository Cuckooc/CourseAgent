"""
模块名：embedding.parent_child

作用：
    父子块（Parent-Child / Small-to-Big）切分与 LangChain Document 构建。

设计动机：
- 子块（child，~300 字）：短、语义聚焦，用于第一阶段向量粗检（短块 embedding 命中率高）；
- 父块（parent，~1200 字）：完整论述单元，不直接参与粗检，命中子块后按 parent_id 取回，
  作为"详细信息"交给重排序模型与最终 LLM，避免小块上下文碎片化；
- 文件隔离：同一文件的父子块共享 file_id/source/original_name，parent_id 仅在文件内编号，
  多线程并行处理多个文件时各自独立构建、独立批次写入，文件内容不会跨文件串块。

旧数据兼容：历史入库的 chunk 没有 doc_level/parent_id 元数据，检索侧走 legacy 路径直接参与精排。

主要成员：
- split_parent_child：整篇文本 → [(父块, [子块...]), ...]（自适应 fine/medium/coarse 三档）；
- build_parent_child_documents：把切分结果包装为带完整 metadata 的父子 Document 列表；
- make_file_id：按文件物理路径生成 16 位 md5 文件级 ID；
- _select_strategy / _pack_parents / _split_children / _base_metadata：内部策略与元数据工具。

被谁使用（Grep "embedding.parent_child" 确认）：
- service/vector_store.py：add_parent_child（split_parent_child +
  build_parent_child_documents + make_file_id），父/子向量写入持久化 Chroma
  （chromadb_data；子块直接 embedding，父向量=子向量均值归一化）；
- service/file_service.py：上传/审核入库前的切分与 file_id 生成；
- service/temp_knowledge_store.py：会话临时库复用同一套父子结构（纯内存 Chroma）；
- tests/phase/test_dedup_version.py：校验 _base_metadata/build_parent_child_documents。

下游存储与检索去向：
    父子 Document 经 service/vector_store.py 写入 Chroma（metadata 携带
    doc_level/parent_id/file_id/scope/version/is_latest）；检索时
    multi_agent/retrieval.py 先用子块粗检，再按 parent_id 取回父块参与精排与 LLM。
    向量必须与库内同为 text-embedding-v2（1536 维），维度约束见 embedding_model 模块说明。
"""
from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

from langchain_core.documents import Document

from file_analysis.file import split_str

# 自适应切分策略：按文本长度路由，短文本细粒度、长文本粗粒度。
# 各档均以段落边界打包父块、以句末标点切分子块，避免语义截断与多主题混杂。
_STRATEGY_PARAMS = {
    # 短文本：细粒度，追求精确检索
    "fine":   {"parent_target": 1000, "parent_max": 1500, "child_chars": 250, "child_overlap": 50, "max_children": 12},
    # 中等：平衡精度与规模
    "medium": {"parent_target": 1300, "parent_max": 1800, "child_chars": 350, "child_overlap": 40, "max_children": 10},
    # 长文本：粗粒度，控制子块数量
    "coarse": {"parent_target": 1600, "parent_max": 2200, "child_chars": 450, "child_overlap": 30, "max_children": 8},
}

# 文本长度阈值（字符数）
_FINE_THRESHOLD = 5_000
_COARSE_THRESHOLD = 30_000

# 子块句末对齐标点集合（中英文句末）
_SENTENCE_END = set("。！？；.!?;")

# 类型别名：(父块全文, 子块文本列表)
ParentChildPair = Tuple[str, List[str]]


def _select_strategy(text_len: int) -> dict:
    """根据文本长度选择切分策略参数。

    被谁调用：split_parent_child（内部路由）。
    参数：text_len —— 待切分文本的字符数。
    返回：_STRATEGY_PARAMS 中 fine/medium/coarse 某一档的参数字典
          （父块目标/上限长度、子块长度/重叠、每父块最大子块数）。
    """
    if text_len <= _FINE_THRESHOLD:
        return _STRATEGY_PARAMS["fine"]
    if text_len <= _COARSE_THRESHOLD:
        return _STRATEGY_PARAMS["medium"]
    return _STRATEGY_PARAMS["coarse"]


def make_file_id(source: str) -> str:
    """按物理存储路径生成文件级短 ID（同一文件的父子块共享，跨文件互不相同）。

    被谁调用：build_parent_child_documents（file_id 缺省时）；
    service/file_service.py 与 service/vector_store.py 在入库前显式生成，
    用于去重/版本判断与 Chroma metadata.file_id。
    参数：source —— 文件物理存储路径或唯一来源标识。
    返回：md5 哈希前 16 位十六进制字符串；去向父子块 ID（{fid}-p{i}-c{j}）与 metadata。
    """
    return hashlib.md5(str(source).encode("utf-8")).hexdigest()[:16]


def _pack_parents(text: str, params: dict) -> List[str]:
    """把原子段落（split_str 产出，≤500 字）贪心打包成父块。

    - 累积长度达到 parent_target 即封块；
    - 单个原子段不拆（split_str 已保证 ≤500 字），故父块最长约 target+500；
    - 段落不跨父块复制，避免同一内容被多个父块重复携带造成重复召回；
    - 硬上限 parent_max 兜底，防止单块过大塞入多个主题。

    被谁调用：split_parent_child（内部步骤）。
    参数：text —— 文件解析/脱敏后的全文（来源 file_analysis 解析结果）；
          params —— _select_strategy 选出的策略参数。
    返回：父块文本列表（段落以 \\n\\n 连接），去向 _split_children 切子块。
    """
    target = params["parent_target"]
    max_chars = params["parent_max"]
    units = split_str(text)
    parents: List[str] = []
    buf: List[str] = []
    buf_len = 0
    for unit in units:
        if buf and buf_len + len(unit) > target:
            parents.append("\n\n".join(buf))
            buf, buf_len = [], 0
        buf.append(unit)
        buf_len += len(unit)
        if buf_len >= target:
            parents.append("\n\n".join(buf))
            buf, buf_len = [], 0
    if buf:
        parents.append("\n\n".join(buf))
    # 硬上限保护（正常路径不会触发，因为原子段 ≤500）
    capped = []
    for p in parents:
        while len(p) > max_chars:
            capped.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            capped.append(p)
    return capped


def _split_children(parent_text: str, params: dict) -> List[str]:
    """父块内切子块，对齐句末标点避免语意截断。

    - 切到目标长度后回退到最近的句末标点（。！？；.!?;）；
    - 找不到句末时用固定窗口兜底；
    - 步长 = child_chars - child_overlap，保证相邻子块有重叠。

    被谁调用：split_parent_child（每个父块切一次）。
    参数：parent_text —— 单个父块全文；params —— 策略参数。
    返回：子块文本列表（至多 max_children 个，超出截断以控制子块总量），
          随父块一起组成 ParentChildPair，去向 build_parent_child_documents。
    """
    child_chars = params["child_chars"]
    overlap = params["child_overlap"]
    max_children = params["max_children"]
    n = len(parent_text)
    if n <= child_chars:
        return [parent_text]
    step = child_chars - overlap
    pieces: List[str] = []
    i = 0
    while i < n:
        end = min(i + child_chars, n)
        # 回退到最近的句末标点（不低于窗口一半，避免块过小）
        if end < n:
            lower = max(i + child_chars // 2, i + 1)
            for j in range(end, lower, -1):
                if parent_text[j - 1] in _SENTENCE_END:
                    end = j
                    break
        piece = parent_text[i:end]
        if piece:
            pieces.append(piece)
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return pieces[:max_children]


def split_parent_child(text: str) -> List[ParentChildPair]:
    """整篇文本 -> [(父块全文, [子块...]), ...]，父块内容拼接即覆盖全文主要信息。

    被谁调用：service/vector_store.py 的 add_parent_child（持久化入库）、
    service/file_service.py（上传/更新入库前切分）。
    参数：text —— 文件解析并脱敏后的全文（来源 file_analysis 解析结果/DAO 触发）。
    返回：ParentChildPair 列表，去向 build_parent_child_documents 包装为 Document。
    """
    # 按总长度自适应选档：短文本细粒度、长文本粗粒度
    params = _select_strategy(len(text))
    pairs: List[ParentChildPair] = []
    # 先打包父块，再在每个父块内切子块，保证子块绝不跨父块
    for parent in _pack_parents(text, params):
        children = _split_children(parent, params)
        if children:
            pairs.append((parent, children))
    return pairs


def _base_metadata(
    *,
    source: str,
    file_id: str,
    scope: str,
    user_id: Optional[int],
    session_id: Optional[int],
    original_name: Optional[str],
    content_hash: Optional[str] = None,
    version: int = 1,
    updated_at: Optional[float] = None,
    is_latest: bool = True,
) -> dict:
    """组装父子块共用的基础 metadata（仅关键字参数，None 值字段省略不写入）。

    被谁调用：build_parent_child_documents（父块、子块各调一次）；
    tests/phase/test_dedup_version.py 校验字段完整性。
    参数：source —— 文件来源路径；file_id —— make_file_id 产出；
          scope —— public/private/temp 可见范围；user_id/session_id —— 归属；
          original_name —— 原始文件名；content_hash —— 内容 SHA-256（去重）；
          version/is_latest/updated_at —— 版本管理字段（旧块 is_latest=False）。
    返回：dict，作为 langchain Document 的 metadata 写入 Chroma，
          供检索层做范围过滤（where）与版本/Python 侧过滤。
    """
    meta = {
        "source": str(source),
        "file_id": file_id,
        "scope": scope,
        "version": int(version),
        "is_latest": bool(is_latest),
    }
    if content_hash:
        meta["content_hash"] = content_hash
    if updated_at is not None:
        meta["updated_at"] = float(updated_at)
    if original_name:
        meta["original_name"] = original_name
    if user_id is not None:
        meta["user_id"] = int(user_id)
    if session_id is not None:
        meta["session_id"] = int(session_id)
    return meta


def build_parent_child_documents(
    pairs: List[ParentChildPair],
    *,
    source: str,
    scope: str = "private",
    user_id: int = None,
    session_id: int = None,
    original_name: str = None,
    file_id: str = None,
    content_hash: str = None,
    version: int = 1,
    updated_at: float = None,
    is_latest: bool = True,
) -> Tuple[List[Tuple[str, Document]], List[Tuple[str, str, Document]]]:
    """构建父子 Document 列表（同一文件内完成，绝不跨文件混块）。

    被谁调用：service/vector_store.py 的 add_parent_child 与
    service/file_service.py 上传/审核入库链路；会话临时库经
    temp_knowledge_store.add_parent_child_text 间接使用。
    参数：pairs —— split_parent_child 的切分结果；source —— 文件来源；
          scope/user_id/session_id/original_name —— 归属与展示元数据；
          file_id —— 外部预生成的文件 ID（None 时由 source 现算）；
          content_hash/version/updated_at/is_latest —— 去重与版本字段，
          来源为 file_service/vector_store 的版本管理逻辑与 DAO。
    :returns: (parents, children)
        parents:  [(parent_id, Document)]  —— doc_level=parent，检索命中子块后按 parent_id 取回
        children: [(child_id, parent_id, Document)] —— doc_level=child，参与向量粗检
    返回去向：由 service/vector_store.py 分别向量化（父向量=子向量均值归一化）
        后写入持久化 Chroma（chromadb_data）或会话内存库，供 multi_agent/retrieval.py 检索。
    """
    fid = file_id or make_file_id(source)
    parents: List[Tuple[str, Document]] = []
    children: List[Tuple[str, str, Document]] = []
    for pi, (parent_text, child_texts) in enumerate(pairs):
        parent_id = f"{fid}-p{pi}"
        pmeta = _base_metadata(
            source=source, file_id=fid, scope=scope, user_id=user_id,
            session_id=session_id, original_name=original_name,
            content_hash=content_hash, version=version, updated_at=updated_at,
            is_latest=is_latest,
        )
        pmeta.update({
            "doc_level": "parent",
            "parent_id": parent_id,
            "parent_index": pi,
            "length": len(parent_text),
        })
        parents.append((parent_id, Document(page_content=parent_text, metadata=pmeta)))

        for ci, child_text in enumerate(child_texts):
            child_id = f"{parent_id}-c{ci}"
            cmeta = _base_metadata(
                source=source, file_id=fid, scope=scope, user_id=user_id,
                session_id=session_id, original_name=original_name,
                content_hash=content_hash, version=version, updated_at=updated_at,
                is_latest=is_latest,
            )
            cmeta.update({
                "doc_level": "child",
                "parent_id": parent_id,
                "child_index": ci,
                "length": len(child_text),
            })
            children.append((child_id, parent_id, Document(page_content=child_text, metadata=cmeta)))
    return parents, children
