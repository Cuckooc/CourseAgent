"""
模块名：service.file_service
作用：知识库文件入库服务。把上传的 txt/md/pdf 文件解析为文本（按 PDF 类型
      路由纯文本提取/OCR/双栏/多模态四条路径），入库前脱敏，切分为父子块
      并计算 embedding，写入持久化向量库（public/private）或会话临时库
      （temp）；同时实现并发安全的内容去重（content_hash + 向量相似度 +
      文件名相似度）与内容变更后的两种替换策略（先删后增+回滚 / 版本标记）。

主要成员：
- FileService：文件处理服务类，process_file 入持久库、process_temp_file 入临时库。
- _dedup_commit_lock()：上下文管理器，按 (scope, 归属, content_hash) 串行化
  "去重判定 + 入库提交"，叠加进程锁与可选的 Redis 跨副本锁。
- _build_dedup_where()：构造去重扫描的 chroma scope 过滤条件。
- _list_existing_files()：分页扫描库内最新版本文件元数据（文件名策略用）。
- _extract_text()：按扩展名/PDF 类型选择文本提取路径。
- _dedup_lock_guard / _dedup_key_locks / _DEDUP_RELEASE_LUA：模块级锁与
  Redis 释放锁 Lua 脚本。

被谁使用：
- control/file_control.py：_process_saved_file 中 `FileService(file_path)`
  实例化，按 scope 调 process_temp_file（temp）或 process_file
  （private/public）；该函数运行于上传线程池，结果回传上传接口/异步任务进度。
- tests/phase/test_dedup_version.py：测试直接导入 FileService 及去重辅助函数。
"""
import hashlib
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from core.config import settings
from app.infrastructure.embeddings.parent_child import (
    build_parent_child_documents,
    make_file_id,
    split_parent_child,
)
from app.infrastructure.embeddings.text_embedding import get_embedding
from app.infrastructure.document.doc_type_detector import detect_pdf_type
from app.infrastructure.document.file import pdf_text
from app.infrastructure.document.ocr_clean import clean_ocr_text
from app.infrastructure.document.ocr_service import ocr_pdf
from service.mask_service import mask_text
from service.temp_knowledge_store import get_temp_store
from service.vector_store import (
    _embed_in_batches,
    _l2_normalize,
    add_new_version,
    add_parent_child,
    find_max_similarity,
    get_persistent_db,
    persistent_lock,
    replace_document,
)

# 模块级日志器：上传解析/去重/入库的计时与告警日志统一走该 logger
logger = logging.getLogger(__name__)


# ---------------- 去重入库临界区（防并发重复插入 TOCTOU 竞态） ----------------
# 并发上传相同内容时，"去重扫描→入库" 是 check-then-act：多个线程可能在首个提交前
# 都扫描到"不存在"，于是重复内容插入多份（file_id 各不同，向量库无 content_hash
# 唯一约束兜底）。按 (scope, 归属, content_hash) 加互斥锁，把"判定+提交"串行化。
# 模块级全局对象：保护 _dedup_key_locks 注册表本身创建/读取的守卫锁
_dedup_lock_guard = threading.Lock()
# 模块级全局对象：去重键 -> 进程内 RLock 的常驻注册表（每键约百字节，
# 受上传限流约束规模可控，进程重启自动清空）
_dedup_key_locks: Dict[str, threading.RLock] = {}

# Redis 释放锁：仅当 token 匹配才删除，避免误删他人的锁
# （跨副本去重锁 lock:dedup:<key> 的安全释放脚本，CAS 语义）
_DEDUP_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@contextmanager
def _dedup_commit_lock(scope: str, user_id: Optional[int], content_hash: str):
    """相同 (scope, 归属, content_hash) 的去重判定 + 入库串行执行。

    功能：
    - 进程内 RLock 始终生效，保证单副本正确性；
    - 配置 Redis 时叠加跨副本锁（SET NX PX 自旋等待，30s 超时/租期）。
      此处不允许静默降级为无锁（chroma 无 content_hash 唯一约束，跨副本
      重复插入无法事后纠正）；Redis 异常或等待超时时退化为仅进程内锁，
      等价于单副本语义。释放时用 token 比对的 Lua 脚本防误删他人锁。
    被谁调用：FileService.process_file() 包裹"_check_duplicate + 向量写入"。
    参数：
    - scope (str)：知识库范围 private/public（来源：上传接口表单，默认 private）。
    - user_id (int|None)：上传用户 ID（JWT 注入），私有库归属隔离用。
    - content_hash (str)：脱敏文本 MD5 前 16 位内容指纹（本模块内计算）。
    返回：上下文管理器，yield 后无返回值；临界区内执行去重判定与入库提交。
    """
    # 归属标识：公共库统一为 pub，私有库按用户 ID 区分
    owner = "pub" if scope == "public" else f"u{int(user_id or 0)}"
    key = f"{scope}:{owner}:{content_hash}"
    with _dedup_lock_guard:
        local_lock = _dedup_key_locks.get(key)
        if local_lock is None:
            local_lock = threading.RLock()
            _dedup_key_locks[key] = local_lock
        # 注册表条目为常驻级（每键仅约百字节），上传受 5次/分/user 限流约束，
        # 进程生命周期内规模可控，进程重启自动清空。

    redis_token = None
    redis_client = None
    redis_key = f"lock:dedup:{key}"
    try:
        from app.infrastructure.redis.redis_client import get_redis

        redis_client = get_redis()
    except Exception:  # noqa: BLE001 - Redis 探测异常按未配置处理
        redis_client = None
    if redis_client is not None:
        redis_token = uuid.uuid4().hex
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                if redis_client.set(redis_key, redis_token, nx=True, px=30000):
                    break
            except Exception:  # noqa: BLE001 - Redis 故障：退化为仅进程内锁
                redis_token = None
                break
            time.sleep(0.05)
        else:
            logger.warning("dedup commit lock: redis 锁等待超时，降级仅进程内锁 key=%s", key)
            redis_token = None

    local_lock.acquire()
    try:
        yield
    finally:
        local_lock.release()
        if redis_token is not None:
            try:
                redis_client.eval(_DEDUP_RELEASE_LUA, 1, redis_key, redis_token)
            except Exception:  # noqa: BLE001 - 释放失败有 PX 自动过期兜底
                pass


def _build_dedup_where(scope: str, user_id: Optional[int]) -> Optional[dict]:
    """构建去重查询的 scope 过滤条件（同 scope + user_id 隔离）。

    功能：生成 chroma where 子句，保证去重扫描只在公共库全量、或当前
          用户私有库范围内进行，杜绝跨 scope/跨用户误判重复。
    被谁调用：FileService._check_duplicate()；tests 中亦有直接导入。
    参数：
    - scope (str)：private/public（上传表单）。
    - user_id (int|None)：私有库归属用户 ID（JWT 注入）。
    返回：dict|None——public 返回 {"scope": "public"}；private 返回
          {"$and": [{"scope": "private"}, {"user_id": uid}]}；去向：
          作为 find_max_similarity 的 where 参数下推给 chroma 查询。
    """
    if scope == "public":
        return {"scope": "public"}
    return {"$and": [{"scope": "private"}, {"user_id": int(user_id or 0)}]}


def _list_existing_files(db, scope: str, user_id: Optional[int]) -> List[Dict]:
    """列出已有文件（按父块去重），返回 [{source, original_name, content_hash, file_id, version}]。

    功能：分页（500/批）扫描 chroma 集合元数据，跳过 child 块与
          is_latest=False 旧版本，按 source 去重，并按 scope/user_id
          做归属过滤，汇总出每个最新版本文件一条记录。
    被谁调用：FileService._check_duplicate() 的 filename 去重策略；
              tests/phase/test_dedup_version.py 亦直接调用。
    参数：
    - db：持久化 Chroma 实例（来源：service/vector_store.get_persistent_db）。
    - scope (str) / user_id (int|None)：范围与归属过滤（同 _build_dedup_where）。
    返回：List[Dict]——文件元数据记录列表；去向：供文件名相似度匹配定位
          旧文件（数据来源：chroma 集合 metadatas）。
    """
    col = db._collection
    files: Dict[str, Dict] = {}
    offset = 0
    batch = 500
    while True:
        data = col.get(include=["metadatas"], limit=batch, offset=offset)
        metas = data.get("metadatas") or []
        if not metas:
            break
        for meta in metas:
            meta = meta or {}
            if meta.get("doc_level") == "child":
                continue
            # 仅匹配最新版本（is_latest=False 的旧版本不参与去重判定）
            if meta.get("is_latest") is False:
                continue
            src = meta.get("source")
            if not src or src in files:
                continue
            f_scope = meta.get("scope")
            f_uid = meta.get("user_id")
            if scope == "public":
                if f_scope != "public":
                    continue
            else:
                if f_scope != "private" or (f_uid is not None and int(f_uid) != int(user_id or 0)):
                    continue
            files[src] = {
                "source": src,
                "original_name": meta.get("original_name"),
                "content_hash": meta.get("content_hash"),
                "file_id": meta.get("file_id"),
                "version": meta.get("version", 1),
            }
        if len(metas) < batch:
            break
        offset += batch
    return list(files.values())


def _extract_text(file_path: str) -> tuple:
    """根据文件类型选择文本提取路径。

    功能：txt/md 直接读取；PDF 先经 file_analysis/doc_type_detector 分类，
          再路由到纯文本提取、OCR+清洗、双栏有序提取或多模态模型提取；
          未知类型兜底按纯文本处理。
    被谁调用：FileService.process_file() / process_temp_file() 的第一步。
    参数：file_path (str)——已落盘文件的绝对路径（来源：
          control/file_control.py 上传后保存的物理文件）。
    返回：tuple (text, doc_type)——
    - text (str)：提取出的原始文本（尚未脱敏，脱敏由调用方执行）；
    - doc_type (str)：文件类型 pure_text/scanned/two_column/image_rich，
      随入库结果回传给上传接口用于前端展示。
    数据来源：file_analysis 包（pdf_text/ocr_pdf/clean_ocr_text/
              two_column_handler/multimodal_service）。
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in (".txt", ".md"):
        return pdf_text(file_path), "pure_text"

    if ext == ".pdf":
        doc_type = detect_pdf_type(file_path)
        if doc_type == "pure_text":
            return pdf_text(file_path), doc_type
        elif doc_type == "scanned":
            logger.info("Scanned PDF detected, running OCR: %s", file_path)
            raw = ocr_pdf(file_path)
            return clean_ocr_text(raw), doc_type
        elif doc_type == "two_column":
            from app.infrastructure.document.two_column_handler import extract_two_column
            logger.info("Two-column PDF detected, using column-ordered extraction: %s", file_path)
            return extract_two_column(file_path), doc_type
        elif doc_type == "image_rich":
            from app.infrastructure.document.multimodal_service import extract_with_multimodal
            logger.info("Image-rich PDF detected, running multimodal extraction: %s", file_path)
            return extract_with_multimodal(file_path), doc_type

    return pdf_text(file_path), "pure_text"


class FileService:
    """知识库文件处理服务：解析 → 脱敏 → 切分 → embedding → 去重 → 入库。

    类作用：每个上传文件对应一个轻量实例（仅持有默认文件路径），线程安全
            的并发控制依赖模块级去重锁与 service/vector_store 的进程写锁，
            实例本身无跨请求可变状态。

    实例化位置：control/file_control.py 的 _process_saved_file 中
            `FileService(file_path)`（上传线程池 worker，每文件一个实例）；
            tests/phase/test_dedup_version.py 中有无参实例化。

    关键 self 属性：
    - path：默认文件路径；process_file/process_temp_file 显式传 path 时
      以入参为准（file_control 始终显式传 path）。
    """

    def __init__(self, path: str = None):
        # path (str|None)：默认处理的文件绝对路径；来源：file_control 传入的
        # 已落盘上传文件路径，None 时须在处理方法中显式传 path
        self.path = path

    def _check_duplicate(
        self, db, original_name: str, content_hash: str,
        child_embs: List[List[float]], scope: str, user_id: Optional[int],
        dedup_strategy: str = None,
    ) -> Dict:
        """去重判定，返回 {action: skip|replace|new, old_source, old_file_id, old_version}。

        功能：full 策略用新文件全部子块向量扫全库取最大相似度，超阈值即
              skip；filename 策略先用文件名 SequenceMatcher 相似度定位旧
              文件，再依次用 content_hash 精确判重、向量相似度判重，指纹
              不同且不相似则判定为 replace（同名文件内容变更）。阈值/策略
              缺省取 core.config.settings。
        被谁调用：process_file() 的去重临界区内。
        参数：
        - db：持久化 Chroma 实例（vector_store.get_persistent_db）。
        - original_name (str)：用户上传的原始文件名（表单文件对象）。
        - content_hash (str)：脱敏文本指纹（MD5 前 16 位）。
        - child_embs：新文件子块 embedding 列表（锁外预计算，来源
          embedding/text_embedding 远程 API）。
        - scope (str) / user_id (int|None)：范围与归属。
        - dedup_strategy (str|None)：full/filename，None 用 settings.DEDUP_STRATEGY。
        返回：Dict——skip（附 old_source）/ new / replace（附 old_source、
              old_file_id、old_version），驱动 process_file 的入库分支。
        """
        where = _build_dedup_where(scope, user_id)
        threshold = settings.SIMILARITY_THRESHOLD
        strategy = dedup_strategy or settings.DEDUP_STRATEGY

        if strategy == "full":
            # 全程扫描：新块向量 vs 全库
            max_sim, matched_source = find_max_similarity(db, child_embs, where)
            if max_sim >= threshold:
                logger.info("dedup(full): skipped, max_sim=%.4f source=%s", max_sim, matched_source)
                return {"action": "skip", "old_source": matched_source}
            return {"action": "new"}

        # filename 策略：先按文件名定位旧文件
        existing = _list_existing_files(db, scope, user_id)
        name_threshold = settings.NAME_SIMILARITY_THRESHOLD
        matched = None
        for f in existing:
            old_name = f.get("original_name") or os.path.basename(f["source"])
            sim = SequenceMatcher(None, original_name or "", old_name).ratio()
            if sim >= name_threshold:
                matched = f
                break

        if not matched:
            return {"action": "new"}

        # 文件名匹配 → content_hash 快速判重
        if matched.get("content_hash") == content_hash:
            logger.info("dedup(filename): skipped by content_hash, source=%s", matched["source"])
            return {"action": "skip", "old_source": matched["source"]}

        # 指纹不同 → 向量相似度判定
        max_sim, _ = find_max_similarity(db, child_embs, where)
        if max_sim >= threshold:
            logger.info("dedup(filename): skipped by similarity=%.4f, source=%s", max_sim, matched["source"])
            return {"action": "skip", "old_source": matched["source"]}

        return {
            "action": "replace",
            "old_source": matched["source"],
            "old_file_id": matched["file_id"],
            "old_version": matched.get("version", 1),
        }

    def process_file(self, path: str = None, scope: str = "private", user_id: int = None, session_id: int = None, original_name: str = None,
                     dedup_strategy: str = None, update_strategy: str = None):
        """解析并入库单个文件到【持久化】向量库（public/private），父子块结构。

        功能流水线：提取文本（_extract_text）→ 脱敏（mask_text）→ MD5
        指纹 → 父子切分 → 锁外批量计算子块 embedding → 在
        _dedup_commit_lock 临界区内去重判定（_check_duplicate）：
        skip 直接返回；replace 按 UPDATE_STRATEGY 走 replace_document
        （策略A 先删后增+回滚）或 add_new_version（策略B 版本标记）；
        new 走 add_parent_child 全量入库。父向量由子向量均值归一化得到，
        不额外调用 embedding API。输出各阶段耗时日志。
        被谁调用：control/file_control.py 的 _process_saved_file
                  （scope 非 temp 分支；运行于上传线程池）。
        参数：
        - path (str|None)：已落盘文件绝对路径（上传文件对象，None 用 self.path）。
        - scope (str)：private/public（上传表单，已做角色鉴权）。
        - user_id (int|None)：上传用户 ID（JWT 注入，私有库隔离）。
        - session_id (int|None)：持久库场景由 file_control 固定传 None。
        - original_name (str|None)：用户上传的原始文件名（前端展示/文件名去重）。
        - dedup_strategy (str|None)：full/filename，None 用全局配置。
        - update_strategy (str|None)：replace/version，None 用全局配置。
        返回：Dict——成功 {"success": True, "file_path", "doc_type",
              "parent_chunks", "child_chunks", "action", "version"}，
              重复时额外 {"skipped": True, "reason"}；失败
              {"success": False, "error"}。去向：file_control 汇总为每文件
              上传结果返回前端（同步接口）或写异步任务进度。
        异常：全部异常在方法内捕获并转为 {"success": False, "error": str(e)}，
              不向线程池调用方抛出。数据去向：chromadb_data 持久化向量库。
        """
        file_path = path or self.path
        t_start = time.perf_counter()
        try:
            t0 = time.perf_counter()
            text, doc_type = _extract_text(file_path)
            t_parse = time.perf_counter() - t0
            if not text.strip():
                return {"success": False, "error": "未从文件中解析到文本内容"}
            # 入库前脱敏
            t0 = time.perf_counter()
            text = mask_text(text)
            t_mask = time.perf_counter() - t0

            # 内容指纹
            content_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]

            # 切分 + 构建文档（用于提前计算 embedding 供去重复用）
            pairs = split_parent_child(text)
            if not pairs:
                return {"success": False, "error": "未从文件中解析到有效文本块"}
            file_id = make_file_id(file_path)
            parents, children = build_parent_child_documents(
                pairs,
                source=file_path, scope=scope, user_id=user_id,
                session_id=session_id, original_name=original_name, file_id=file_id,
                content_hash=content_hash,
            )
            child_texts = [doc.page_content for _, _, doc in children]

            # 锁外：子块 embedding（去重判定 + 入库复用，避免重复调用）
            t0 = time.perf_counter()
            child_embs = _embed_in_batches(get_embedding(), child_texts)
            t_embed = time.perf_counter() - t0

            # 去重判定 + 入库提交必须在同一临界区内（防并发相同内容 TOCTOU 重复插入）；
            # embedding 已在锁外完成，临界区内只有查询与向量写入。
            db = get_persistent_db()
            with _dedup_commit_lock(scope, user_id, content_hash):
                dedup = self._check_duplicate(db, original_name, content_hash, child_embs, scope, user_id, dedup_strategy)

                if dedup["action"] == "skip":
                    return {
                        "success": True, "skipped": True,
                        "reason": "内容已存在", "doc_type": doc_type,
                    }

                updated_at = time.time()
                new_version = dedup.get("old_version", 0) + 1
                parent_ids = [pid for pid, _ in parents]
                parent_docs = [doc.page_content for _, doc in parents]
                parent_metas = [doc.metadata for _, doc in parents]
                child_ids = [cid for cid, _, _ in children]
                child_metas = [doc.metadata for _, _, doc in children]

                # 父向量 = 子块均值归一化
                sums: dict = {}
                counts: dict = {}
                for (_cid, pid, _doc), emb in zip(children, child_embs):
                    acc = sums.setdefault(pid, [0.0] * len(emb))
                    for i, v in enumerate(emb):
                        acc[i] += v
                    counts[pid] = counts.get(pid, 0) + 1
                parent_embs = []
                dim = len(child_embs[0]) if child_embs else 1536
                for pid in parent_ids:
                    acc = sums.get(pid)
                    if acc:
                        parent_embs.append(_l2_normalize([v / counts[pid] for v in acc]))
                    else:
                        parent_embs.append([0.0] * dim)

                t0 = time.perf_counter()
                if dedup["action"] == "replace":
                    old_source = dedup["old_source"]
                    effective_update = update_strategy or settings.UPDATE_STRATEGY
                    # 新版本元数据统一更新 version / updated_at
                    for m in parent_metas + child_metas:
                        m["version"] = new_version
                        m["updated_at"] = updated_at
                    if effective_update == "replace":
                        # 策略A：先删后增 + 回滚
                        all_ids = parent_ids + child_ids
                        all_embs = parent_embs + child_embs
                        all_docs = parent_docs + child_texts
                        all_metas = parent_metas + child_metas
                        replace_document(
                            db, persistent_lock(),
                            old_source=old_source,
                            new_ids=all_ids, new_embs=all_embs,
                            new_docs=all_docs, new_metas=all_metas,
                        )
                    else:
                        # 策略B：版本标记
                        all_ids = parent_ids + child_ids
                        all_embs = parent_embs + child_embs
                        all_docs = parent_docs + child_texts
                        all_metas = parent_metas + child_metas
                        add_new_version(
                            db, persistent_lock(),
                            old_file_id=dedup["old_file_id"],
                            new_ids=all_ids, new_embs=all_embs,
                            new_docs=all_docs, new_metas=all_metas,
                            superseded_at=updated_at,
                        )
                else:
                    # 全新文件：直接入库（复用预计算 embedding）
                    add_parent_child(
                        db, persistent_lock(),
                        source=file_path, scope=scope, text=text,
                        user_id=user_id, session_id=session_id, original_name=original_name,
                        content_hash=content_hash, version=new_version,
                        updated_at=updated_at, precomputed_child_embs=child_embs,
                    )
                t_store = time.perf_counter() - t0

            logger.info(
                "[upload-timing] file=%s doc_type=%s parse=%.3fs mask=%.3fs embed=%.3fs store=%.3fs total=%.3fs parents=%d children=%d action=%s",
                file_path, doc_type, t_parse, t_mask, t_embed, t_store,
                time.perf_counter() - t_start, len(parents), len(children), dedup["action"],
            )
            return {
                "success": True,
                "file_path": file_path,
                "doc_type": doc_type,
                "parent_chunks": len(parents),
                "child_chunks": len(children),
                "action": dedup["action"],
                "version": new_version,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def process_temp_file(self, path: str = None, user_id: int = None, session_id: int = None, original_name: str = None):
        """解析并入库单个文件到【会话内存】临时知识库（不落盘到本地向量数据库）。

        功能：提取文本 → 脱敏 → 经 TempKnowledgeStore.add_parent_child_text
        以父子块写入 (user_id, session_id) 维度的会话临时 Chroma 持久目录
        （uploads/temp/<uid>_<sid>/chroma）；不做去重判定，也不走
        _dedup_commit_lock（临时目录按会话天然隔离）。会话结束时由
        TempKnowledgeStore.drop 释放，会话滚换时由 relocate 复制迁移。
        被谁调用：control/file_control.py 的 _process_saved_file
                  （scope == "temp" 分支；上传前已做会话归属校验）。
        参数：
        - path (str|None)：已落盘文件绝对路径（位于会话临时目录，None 用 self.path）。
        - user_id (int|None)：上传用户 ID（JWT 注入）。
        - session_id (int|None)：当前会话 ID（per-user 序列，临时库隔离键）。
        - original_name (str|None)：用户上传的原始文件名。
        返回：Dict——成功 {"success": True, "file_path", "doc_type",
              "persisted": False, "parent_chunks", "child_chunks"}；
              失败 {"success": False, "error"}。去向：file_control 汇总为
              上传结果返回前端/写异步任务进度；向量去向：会话临时向量库，
              供 multi_agent/retrieval.py 与 FileAgent 当轮检索使用。
        异常：全部异常捕获后转为 success=False 结果，不向调用方抛出。
        """
        file_path = path or self.path
        t_start = time.perf_counter()
        try:
            t0 = time.perf_counter()
            text, doc_type = _extract_text(file_path)
            t_parse = time.perf_counter() - t0
            if not text.strip():
                return {"success": False, "error": "未从文件中解析到文本内容"}
            t0 = time.perf_counter()
            text = mask_text(text)
            t_mask = time.perf_counter() - t0
            t0 = time.perf_counter()
            parents, children = get_temp_store().add_parent_child_text(
                user_id,
                session_id,
                source=file_path,
                text=text,
                original_name=original_name,
            )
            t_store = time.perf_counter() - t0
            if parents == 0:
                return {"success": False, "error": "未从文件中解析到有效文本块"}
            logger.info(
                "[upload-timing] file=%s doc_type=%s parse=%.3fs mask=%.3fs store=%.3fs total=%.3fs parents=%d children=%d",
                file_path, doc_type, t_parse, t_mask, t_store,
                time.perf_counter() - t_start, parents, children,
            )
            return {
                "success": True,
                "file_path": file_path,
                "doc_type": doc_type,
                "persisted": False,
                "parent_chunks": parents,
                "child_chunks": children,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
