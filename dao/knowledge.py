"""
模块名：dao.knowledge

作用：
    知识库文档管理 DAO。注意它不直接操作 MySQL 业务表，而是管理两处存储：
    1) Chroma 向量库（经 multi_agent.rag_agent.RAGAgent.build_shared_db() 取得
       共享 collection）——文档以父子块向量 + metadata 形式存在；
    2) 上传文件目录（settings.UPLOAD_DIR）——文档物理文件。

文档元数据（Chroma metadata）：
- source: 物理文件绝对路径
- scope: public / private / temp
- user_id: 私有/临时文档的归属用户
- session_id: 临时文档的归属会话

可见范围：公共 + 当前用户私有 + 当前用户所有会话的临时。

主要成员：
- 模块级函数：build_stored_filename()（生成安全存储文件名）、
  parse_stored_filename()（解析存储文件名）、is_valid_stored_filename()（格式白名单校验）；
- KnowledgeDAO：文档列举 / 单文件信息查询 / 删除（向量分块 + 物理文件）。

被谁使用：
- service/knowledge_service.py 的 KnowledgeService.__init__ 中实例化
  KnowledgeDAO(settings.UPLOAD_DIR)，其 list_documents/get_document_info/
  delete_document 直接转发本 DAO 同名方法，上层为
  control/knowledge_control.py 的知识库管理端点；
- control/file_control.py 上传处理中直接 import build_stored_filename() 生成落盘文件名。
"""
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from multi_agent.rag_agent import RAGAgent
from service.vector_store import _flush_collection_index, get_persistent_db, persistent_lock

logger = logging.getLogger(__name__)

# 上传存储文件名：{user_id}_{清洗后原始文件名}_{hash32}.{ext}（hash32 = uuid4 hex，
# 作为唯一标识防同名冲突与文件名猜解）；
# 兼容历史遗留的纯 hash 命名：{hash32}.{ext}（归属仅由向量 metadata 决定）。
_FILENAME_RE = re.compile(
    r"^(?:(?P<uid>\d{1,10})_(?P<name>.+?)_)?(?P<hash>[0-9a-f]{32})\.(?P<ext>pdf|txt|md)$"
)

# 原始文件名清洗：仅保留中文/字母/数字/下划线/连字符，其余折叠为下划线
_NAME_SAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+")
_NAME_MAX_LEN = 50


def build_stored_filename(user_id: int, original_name: str, ext: str) -> str:
    """构造知识库存储文件名：{user_id}_{安全原始名}_{hash32}.{ext}。

    功能：纯字符串处理，不访问数据库/文件系统。
    - user_id：归属用户（隔离标识之一，配合向量 metadata scope/user_id 双保险）；
    - 原始文件名清洗为安全字符并限长，避免路径注入/超长文件名；
    - hash32（uuid4）：唯一标识，防止同名覆盖与文件名猜解。

    被谁调用：control/file_control.py 的上传处理函数
    （文件.函数：file_control 上传接口处理逻辑，落盘前生成存储名）。
    参数：
        user_id: 上传者用户 ID（登录态），写入文件名前缀作为归属标识。
        original_name: 用户上传的原始文件名（可能含任意字符，需清洗）。
        ext: 文件扩展名（如 .pdf/.txt/.md，调用方已做后缀白名单校验）。
    返回：str，可直接安全落盘/拼接路径的存储文件名。
    """
    stem, _ = os.path.splitext(original_name or "")
    safe = _NAME_SAFE_RE.sub("_", stem).strip("_")[:_NAME_MAX_LEN] or "file"
    return f"{int(user_id or 0)}_{safe}_{uuid.uuid4().hex}{(ext or '').lower()}"


def parse_stored_filename(name: str) -> Optional[Dict[str, str]]:
    """解析存储文件名，返回 {user_id, name, hash, ext}（旧格式 user_id/name 为 None）。

    功能：按 _FILENAME_RE 白名单正则解析，同时兼容两种历史命名
    （{uid}_{name}_{hash}.{ext} 与旧的纯 {hash}.{ext}）；不匹配返回 None。
    被谁调用：KnowledgeDAO 的 list_documents()（孤儿归属判定）、
    _fallback_original_name()、get_document_info()、delete_document()
    （旧格式文件的权限兜底判定）。
    参数：
        name: 存储文件名（仅 basename，不含目录）。
    返回：Optional[Dict[str, str]]。命中返回含 user_id/name/hash/ext 的字典
          （旧格式下 user_id、name 为 None）；非法文件名返回 None。
    """
    m = _FILENAME_RE.match(name or "")
    if not m:
        return None
    return {
        "user_id": m.group("uid"),
        "name": m.group("name"),
        "hash": m.group("hash"),
        "ext": m.group("ext"),
    }


def is_valid_stored_filename(name: str) -> bool:
    """存储文件名白名单校验：整体必须匹配 _FILENAME_RE（防路径穿越的第一道关）。

    功能：只接受 {uid}_{name}_{hash32}.{ext} 或旧 {hash32}.{ext} 形态，
    任何含路径分隔符、../、双扩展名或非法字符的名称直接判 False。
    被谁调用：KnowledgeDAO._resolve()（删除/查询前校验前端传入名）、
    list_documents()（列举向量记录与扫描孤儿文件时过滤）。
    参数：
        name: 待校验的文件名（basename）。
    返回：bool，True 表示文件名形态合法（合法性不等于存在或有权限）。
    """
    return bool(_FILENAME_RE.match(name or ""))


class KnowledgeDAO:
    """知识库文档数据访问层：管理 Chroma 向量分块与 UPLOAD_DIR 物理文件。

    不对应单张 MySQL 表；向量元数据承担“表记录”角色。承担文档的
    列举（list_documents）、单文件信息查询（get_document_info）、
    删除（delete_document：向量分块 + 物理文件），不含文档入库
    （入库在 service/vector_store.py / service/review_service.py）。
    实例化位置：service/knowledge_service.py 的 KnowledgeService.__init__
    （self.dao = KnowledgeDAO(settings.UPLOAD_DIR)）。
    __init__ 形参：
        upload_dir: 上传文件根目录，来源 core.config.settings.UPLOAD_DIR；
                    保存为 self._upload_dir（Path 对象），所有物理文件操作
                    均限制在该目录内。
    关键属性去向：self._collection 为懒加载的共享 Chroma collection
    （经 RAGAgent.build_shared_db() 获取，单例复用），供各方法做向量 get/delete。
    """

    def __init__(self, upload_dir):
        self._upload_dir = Path(upload_dir)
        self._collection = None

    def _col(self):
        """懒加载并缓存共享 Chroma collection（首次调用时绑定，之后复用）。

        返回：Chroma collection 实例，来源 RAGAgent.build_shared_db()._collection；
        被本类 _iter_metadatas/get_document_info/delete_document 使用。
        """
        if self._collection is None:
            self._collection = RAGAgent.build_shared_db()._collection
        return self._collection

    # 分批拉取向量库记录的页大小：避免大库一次性全量载入内存
    _LIST_BATCH = 500

    def _iter_metadatas(self):
        """分批遍历向量库全部 metadata（生成器，limit/offset 滚动拉取，内存有界）。

        功能：以 _LIST_BATCH 为页大小反复 col.get(include=["metadatas"])，
        逐批 yield 每条 metadata；某批不足一页即视为末批结束。
        被谁调用：list_documents() 聚合统计文件片段数/长度与可见性。
        返回：生成器，逐项产出 metadata 字典（可能为 None/空字典，调用方自行判空）。
        """
        offset = 0
        while True:
            batch = (
                self._col()
                .get(include=["metadatas"], limit=self._LIST_BATCH, offset=offset)
                .get("metadatas")
                or []
            )
            if not batch:
                return
            yield from batch
            if len(batch) < self._LIST_BATCH:
                return
            offset += self._LIST_BATCH

    def _resolve(self, filename: str) -> Optional[Path]:
        """把外部传入的文件名解析为上传目录内的安全绝对路径。

        路径穿越防护（双重）：
        1) 先经 is_valid_stored_filename() 白名单校验，拒绝含分隔符/.. 的名称；
        2) 再对 resolve() 后的真实路径做 relative_to(root)  containment 校验，
           解析结果一旦逃逸出上传根目录（符号链接等绕过）即返回 None。
        被谁调用：get_document_info() / delete_document() 的第一步入参净化。
        参数：
            filename: 前端请求中的存储文件名（basename）。
        返回：Optional[Path]。安全时返回上传目录内的绝对路径；非法返回 None
              （调用方按“文件不存在/无权限”处理，不暴露具体原因）。
        """
        if not is_valid_stored_filename(filename):
            return None
        root = self._upload_dir.resolve()
        target = (root / filename).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        return target

    def list_documents(self, user_id: int = None, is_admin: bool = False) -> List[Dict[str, Any]]:
        """列出知识库管理页可见的文件：公共 + 自己的私有。
        临时知识库(scope=temp)仅在会话内存中、不持久化，不在管理页展示。
        孤儿文件按存储名前缀判归属；历史纯 hash 命名无法判定归属，仅 admin 可见。

        功能：聚合向量库 metadata（经 _iter_metadatas 分批拉取，父子结构只计
        父块、旧版本 is_latest=false 跳过），再扫描上传目录补充“孤儿文件”，
        两侧都按 scope/归属做同等可见性过滤，防止他人私有文件经孤儿通道泄露。
        被谁调用：service/knowledge_service.py 的 KnowledgeService.list_documents()
        （文件.函数：knowledge_service.KnowledgeService.list_documents），
        上层端点为 control/knowledge_control.py 的 list_documents。
        参数：
            user_id: 当前登录用户 ID，来源登录态；None 表示匿名上下文（看不到私有）。
            is_admin: 当前用户是否管理员（角色由 control 层依据库内 role 判定）；
                      admin 可见全部公共文档与无主孤儿文件。
        返回：List[Dict[str, Any]]，按 filename 字典序升序；每项含
              filename / original_name / scope / chunks（片段数）/
              total_length（文本总长）/ orphan（是否物理存在但无向量记录），
              交给 KnowledgeService 原样返回前端。
        """
        agg: Dict[str, Dict[str, Any]] = {}
        known_sources: set = set()  # 所有在向量库中有记录的文件（含旧版本），避免孤儿通道误展示
        for meta in self._iter_metadatas():
            meta = meta or {}
            source = meta.get("source")
            if source:
                known_sources.add(os.path.basename(str(source)))
            # 父子结构：子块仅为粗检索引，管理页按父块（含旧结构 chunk）统计片段数与长度，
            # 避免同一文件内容被父+子重复计数
            if meta.get("doc_level") == "child":
                continue
            # 仅展示最新版本：被替换的旧版本（is_latest=false）不在管理页展示，
            # 由定时清理任务满 3 年后硬删除
            if meta.get("is_latest") is False:
                continue
            if not source:
                continue
            fname = os.path.basename(str(source))
            if not is_valid_stored_filename(fname):
                continue
            scope = meta.get("scope", "public")
            owner = meta.get("user_id")
            # 临时知识库不在管理页展示
            if scope == "temp":
                continue
            # 可见性（管理页展示，与对话检索的"公共全员可检索"分离）：
            # - 公共(public)：admin 可见全部；teacher/user 仅可见自己上传的
            # - 私有(private)：仅所有者可见
            if scope == "public":
                visible = is_admin or (user_id is not None and owner == user_id)
            else:
                visible = user_id is not None and owner == user_id
            if not visible:
                continue
            bucket = agg.setdefault(
                fname,
                {
                    "filename": fname,
                    # 原始文件名：优先 metadata；历史数据回退从存储名解析，再回退存储名
                    "original_name": meta.get("original_name")
                    or self._fallback_original_name(fname),
                    "scope": scope,
                    "chunks": 0,
                    "total_length": 0,
                    "orphan": False,
                },
            )
            bucket["chunks"] += 1
            bucket["total_length"] += int(meta.get("length", 0) or 0)

        # 孤儿文件（UPLOAD_DIR 根目录中存在但未成功入库的残留）：
        # 必须与向量记录同等执行可见性过滤——他人私有文件即使被上面的 agg 排除，
        # 也不能借孤儿通道重新暴露给其他用户
        if self._upload_dir.exists():
            for f in self._upload_dir.iterdir():
                if not f.is_file():
                    continue
                if not is_valid_stored_filename(f.name) or f.name in agg or f.name in known_sources:
                    continue
                parsed = parse_stored_filename(f.name)
                owner_uid = int(parsed["user_id"]) if parsed and parsed["user_id"] else None
                if owner_uid is None:
                    if not is_admin:
                        continue  # 无主孤儿（历史纯 hash 命名）仅 admin 可见
                elif user_id != owner_uid:
                    continue  # 他人上传的残留文件不可见
                # 孤儿文件（物理存在但向量库无记录）：前端展示为「禁用」状态，
                # 此处仅后端日志留痕，不向用户暴露孤儿概念
                logger.warning(
                    "orphan file detected: name=%s owner_uid=%s",
                    f.name, owner_uid,
                )
                agg[f.name] = {
                    "filename": f.name,
                    "original_name": self._fallback_original_name(f.name),
                    "scope": "unknown",
                    "chunks": 0,
                    "total_length": 0,
                    "orphan": True,
                }
        return sorted(agg.values(), key=lambda x: x["filename"])

    @staticmethod
    def _fallback_original_name(fname: str) -> str:
        """无 original_name 元数据时，从存储名解析原始文件名（uid_名字_hash.ext）。

        功能：历史数据缺少 metadata.original_name 时的展示名兜底；
        解析失败或旧格式无名字段时直接返回存储名本身。
        被谁调用：list_documents()（向量记录与孤儿文件）、get_document_info()。
        参数：
            fname: 存储文件名（basename）。
        返回：str，尽量还原为“原始名.ext”，否则返回原存储名。
        """
        parsed = parse_stored_filename(fname)
        if parsed and parsed["name"]:
            return f"{parsed['name']}.{parsed['ext']}"
        return fname

    def get_document_info(self, filename: str, user_id: int = None, is_admin: bool = False) -> Optional[Dict[str, Any]]:
        """查询单个文件的基础信息（删除前预览，不存在或无权限返回 None）。

        功能：先经 _resolve() 做路径净化，再按 source 绝对路径查向量库 metadata；
        命中时非 admin 且 metadata.user_id 与当前用户不符则拒绝；
        向量库无记录时退化为按存储名前缀判归属（无主旧格式仅 admin 可见）。
        被谁调用：service/knowledge_service.py 的 KnowledgeService.get_document_info()
        （文件.函数：knowledge_service.KnowledgeService.get_document_info），
        上层端点为 control/knowledge_control.py 的 delete_document_preview。
        参数：
            filename: 前端传入的待查存储文件名（删除确认令牌流程中再次使用）。
            user_id: 当前登录用户 ID，来源登录态。
            is_admin: 是否管理员，来源角色鉴权。
        返回：Optional[Dict[str, Any]]。有权限返回
              {"file_name": 展示名, "scope": public/private/unknown}；
              不存在或无权限一律返回 None（不区分原因，避免探测）。
        """
        target = self._resolve(filename)
        if target is None:
            return None
        col = self._col()
        data = col.get(where={"source": str(target)}, include=["metadatas"])
        metadatas = data.get("metadatas") or []
        if metadatas:
            meta = metadatas[0] or {}
            owner = meta.get("user_id")
            if not is_admin and (owner is None or int(owner) != int(user_id or 0)):
                return None
            return {
                "file_name": meta.get("original_name") or self._fallback_original_name(filename),
                "scope": meta.get("scope", "public"),
            }
        parsed = parse_stored_filename(filename)
        if parsed and parsed["user_id"]:
            if not is_admin and int(parsed["user_id"]) != int(user_id or 0):
                return None
        elif not is_admin:
            return None
        return {"file_name": self._fallback_original_name(filename), "scope": "unknown"}

    def delete_document(self, filename: str, user_id: int = None, is_admin: bool = False) -> Optional[Dict[str, Any]]:
        """删除文档：该文件的全部向量分块 + 物理文件。权限校验：所有者或 admin。

        功能：路径净化后按 source 查出全部父子块 id；权限策略为
        公共文档需 admin 或上传者本人、私有文档需所有者，孤儿文件按存储名
        前缀判归属（历史纯 hash 命名无法判定，仅 admin 可清理）。
        向量删除在进程级 persistent_lock() 写锁内串行执行，并先
        _flush_collection_index 刷掉异步残批再落盘，防止重启后已删条目复现；
        随后 unlink 物理文件（文件可能已不存在，不视为错误）。
        被谁调用：service/knowledge_service.py 的 KnowledgeService.delete_document()
        （文件.函数：knowledge_service.KnowledgeService.delete_document），
        上层端点为 control/knowledge_control.py 的 delete_document_confirm
        （已通过二次确认令牌 PendingDeleteStore 校验）。
        参数：
            filename: 前端确认删除的存储文件名（经确认令牌绑定用户与动作）。
            user_id: 当前登录用户 ID，来源登录态。
            is_admin: 是否管理员，来源角色鉴权。
        返回：Optional[Dict[str, Any]]。成功返回
              {"deleted_chunks": 删除的向量条数, "file_removed": 物理文件是否删除}；
              路径非法或权限不足返回 None（service 层据此返回“无权限/不存在”）。
        """
        target = self._resolve(filename)
        if target is None:
            return None
        col = self._col()
        data = col.get(where={"source": str(target)}, include=["metadatas"])
        ids = data.get("ids") or []
        metadatas = data.get("metadatas") or []
        # 权限校验：公共需 admin 或上传者本人（teacher 维护自己的公共上传）；私有需所有者；
        # 孤儿文件（无向量条目）按存储名前缀判归属，历史纯 hash 命名无法判定 → 仅 admin 可清理
        if not is_admin:
            if metadatas:
                owner = (metadatas[0] or {}).get("user_id")
                scope = (metadatas[0] or {}).get("scope")
                if owner is None or int(owner) != int(user_id or 0):
                    return None  # 公共/私有均仅所有者（或 admin）可删
            else:
                parsed = parse_stored_filename(filename)
                if not parsed or not parsed["user_id"] or int(parsed["user_id"]) != int(user_id or 0):
                    return None
        if ids:
            # 父子结构：where source 命中该文件的全部父块+子块；删除在进程写锁内串行
            with persistent_lock():
                col.delete(ids=ids)
                # 删除同样先经异步残批，等其并入索引后落盘，避免重启后已删条目复现
                _flush_collection_index(get_persistent_db(), deleted_ids=ids)
            logger.info("deleted %d vectors for %s", len(ids), filename)
        file_removed = False
        if target.exists():
            target.unlink()
            file_removed = True
        return {"deleted_chunks": len(ids), "file_removed": file_removed}
