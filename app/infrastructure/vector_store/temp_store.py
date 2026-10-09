"""
临时知识库存储（会话级持久化）。

设计：
- 键为 (user_id, session_id) 复合键：会话号是 per-user 序列（每个用户都从 1 开始），
  纯 session_id 作键会导致不同用户同号会话共用同一个临时库、知识内容跨用户交错；
  复合键保证用户A永远检索不到用户B上传的临时知识库内容；
- 每个 (user_id, session_id) 一个独立的 Chroma persistent client，
  向量数据落盘在 UPLOAD_DIR/temp/<user_id>_<session_id>/chroma/，
  进程重启后 get_db/has_session 自动从磁盘恢复，不丢数据；
- 上传的物理文件存放在同目录（UPLOAD_DIR/temp/<user_id>_<session_id>/），
  用户删除会话时通过 drop() 一并清理（向量库+物理文件）；
- 多实例部署时临时知识库仅在处理上传/检索的实例上有效（会话粘性），
  不跨实例共享，符合"当前会话临时使用"的语义；
- 线程安全：注册表加锁，embedding 重调用由调用方放入线程池。

主要成员：
- TempKnowledgeStore：会话临时知识库注册表与生命周期管理类。
- TempKnowledgeStore.get_db()：懒创建/从磁盘恢复会话 Chroma 库。
- ensure_session_dir()/temp_file_path()：会话临时物理目录与文件路径。
- add_documents()/add_parent_child_text()：会话库写入（通用文档/父子块）。
- has_session()/list_files()：会话存在性判定与物理文件列举。
- drop()：销毁会话临时库（向量 + 物理文件）。
- relocate()：会话滚换时以复制方式迁移临时库。
- get_temp_store()：lru_cache 应用级单例工厂（根目录 uploads/temp）。

被谁使用：
- app/api/v1/files.py：上传 temp 文件时取单例并 ensure_session_dir 落盘；
- app/application/files/file_service.py：process_temp_file 经 get_temp_store() 父子块入库；
- app/domain/agents/retrieval.py：FileAgent 检索时 has_session/get_db 加载会话库；
- app/domain/tools/business/knowledge_business.py：session_file_search 工具 has_session 判定；
- app/api/v1/history.py：删除会话时 drop() 一并清理；
- app/domain/memory/session_rollover.py：长会话滚换时 relocate() 迁移临时库。
"""
import gc
import logging
import shutil
import threading
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

import chromadb
from langchain_chroma import Chroma

from core.config import settings
from app.infrastructure.embeddings.text_embedding import get_embedding

# 模块级日志器：临时库创建/恢复/迁移/销毁日志走该 logger
logger = logging.getLogger(__name__)

# 模块级类型别名：会话复合键 (user_id, session_id)，二者均为 int
_SKey = Tuple[int, int]


class TempKnowledgeStore:
    """进程内临时知识库注册表：(user_id, session_id) -> Chroma 持久化实例。

    类作用：以 (user_id, session_id) 复合键管理每个会话独立的 Chroma
            PersistentClient（落盘 uploads/temp/<uid>_<sid>/chroma/）与
            同目录物理文件，提供懒创建/磁盘恢复、写入、查询判定、销毁与
            滚换复制能力，保证会话号 per-user 序列下的跨用户隔离。

    实例化位置：不在业务代码中直接 new；唯一创建处为本模块末尾的
            get_temp_store()（@lru_cache 应用级单例，根目录
            settings.UPLOAD_DIR/temp），被 file_control/file_service/
            retrieval/knowledge_business/history_control/session_rollover
            经 get_temp_store() 共用。

    关键 self 属性：
    - _root：临时文件根目录 Path（uploads/temp），所有会话目录的父目录，
      由 get_temp_store() 用 settings.UPLOAD_DIR 构造传入。
    - _dbs：复合键 -> Chroma 实例的进程内注册表（懒加载缓存）。
    - _lock：注册表锁，保护 _dbs 的创建/销毁（被 get_db/has_session/drop 使用）。
    - _write_lock：向量写入 RLock，多文件并行上传时串行化同进程 Chroma 写入，
      被 add_documents/add_parent_child_text 持有，并传给
      vector_store.add_parent_child。
    """

    def __init__(self, temp_root):
        # temp_root (str|Path)：临时库根目录，来源：get_temp_store() 传入的
        # settings.UPLOAD_DIR/temp
        self._root = Path(temp_root)
        self._dbs: Dict[_SKey, Chroma] = {}
        self._lock = threading.Lock()       # 注册表锁：保护 _dbs 的创建/销毁
        self._write_lock = threading.RLock()  # 向量写入锁：多文件并行上传时串行化 Chroma 写入

    @staticmethod
    def _key(user_id: int, session_id: int) -> _SKey:
        """规范化会话复合键：None/缺省归零，统一转 int（user_id, session_id）。

        被谁调用：类内 get_db/has_session/drop/relocate 等所有按会话定位的方法。
        参数：user_id/session_id——JWT 用户 ID 与会话 ID。
        返回：Tuple[int, int]——规范化后的复合键。
        """
        return int(user_id or 0), int(session_id or 0)

    @staticmethod
    def _dir_name(user_id: int, session_id: int) -> str:
        """生成会话目录名 "<uid>_<sid>"（复合键的磁盘编码，保证跨用户隔离）。

        被谁调用：_session_dir/_chroma_dir/list_files/drop/relocate。
        参数：user_id/session_id——会话复合键的两部分。
        返回：str——目录名。
        """
        return f"{int(user_id or 0)}_{int(session_id or 0)}"

    def _session_dir(self, user_id: int, session_id: int) -> Path:
        """返回会话临时目录 Path 并确保其存在（parents 递归创建）。

        被谁调用：ensure_session_dir/temp_file_path/_chroma_dir。
        参数：user_id/session_id——会话复合键。
        返回：Path——uploads/temp/<uid>_<sid> 目录（物理文件落盘位置）。
        """
        d = self._root / self._dir_name(user_id, session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _chroma_dir(self, user_id: int, session_id: int) -> Path:
        """会话向量库的持久化目录（与物理文件同目录下的 chroma/）。

        被谁调用：get_db/has_session/relocate。注意本方法只拼路径不建目录，
        chromadb PersistentClient 会自行创建。
        参数：user_id/session_id——会话复合键。
        返回：Path——uploads/temp/<uid>_<sid>/chroma。
        """
        return self._root / self._dir_name(user_id, session_id) / "chroma"

    def get_db(self, user_id: int, session_id: int) -> Chroma:
        """获取（懒创建/恢复）会话的持久化向量库。

        首次访问时若磁盘已有 chroma 数据（进程重启场景）则直接加载，
        否则创建新的持久化库。

        功能：注册表命中直接返回；否则在 _lock 临界区内构造
              chromadb.PersistentClient + langchain Chroma（集合名
              temp_u<uid>_s<sid>，embedding 函数取 embedding/text_embedding
              单例），登记进 _dbs 并记录 created/restored 日志。
        被谁调用：add_documents/add_parent_child_text（入库）、
                  app/domain/agents/retrieval.py 与 knowledge_business（检索加载）、
                  relocate（创建新会话库）。
        参数：user_id/session_id——会话复合键。
        返回：Chroma——该会话专属的持久化向量库实例；去向：FileAgent 检索
              与父子块写入。
        """
        key = self._key(user_id, session_id)
        with self._lock:
            db = self._dbs.get(key)
            if db is not None:
                return db
            chroma_dir = self._chroma_dir(key[0], key[1])
            restored = chroma_dir.is_dir()
            client = chromadb.PersistentClient(path=str(chroma_dir))
            db = Chroma(
                collection_name=f"temp_u{key[0]}_s{key[1]}",
                embedding_function=get_embedding(),
                client=client,
            )
            self._dbs[key] = db
            logger.info(
                "temp knowledge db %s for user %s session %s",
                "restored from disk" if restored else "created",
                key[0], key[1],
            )
            return db

    def ensure_session_dir(self, user_id: int, session_id: int) -> Path:
        """确保会话临时目录存在并返回其绝对路径。

        被谁调用：app/api/v1/files.py 上传 temp 文件前确定落盘目录。
        参数：user_id/session_id——会话复合键（上传前已做会话归属校验）。
        返回：Path——会话临时目录绝对路径（uploads/temp/<uid>_<sid>）。
        """
        return self._session_dir(user_id, session_id)

    def temp_file_path(self, user_id: int, session_id: int, stored_name: str) -> Path:
        """返回会话临时目录内的文件路径（目录已创建）。

        被谁调用：上传落盘/读取临时文件时按存储名拼绝对路径（工具类辅助）。
        参数：user_id/session_id——会话复合键；stored_name (str)——落盘文件名。
        返回：Path——会话目录内该文件的完整路径（不保证文件本身已存在）。
        """
        return self._session_dir(user_id, session_id) / stored_name

    def add_documents(self, user_id: int, session_id: int, docs) -> None:
        """将 LangChain Document 列表写入会话内存库（分批，避免单次过大）。

        功能：取会话库后持 _write_lock，按每批 25 条调 db.add_documents
              串行写入（embedding 由 Chroma 的 embedding 函数内部计算）。
        被谁调用：通用文档写入入口（当前主链路父子块入库走
                  add_parent_child_text；本方法保留给直接 Document 入库场景）。
        参数：user_id/session_id——会话复合键；docs——LangChain Document 列表。
        返回：无。数据去向：会话 chroma 持久目录。
        """
        db = self.get_db(user_id, session_id)
        batch = 25
        # 写锁内分批 add：避免单次负载过大并串行化同进程 Chroma 写
        with self._write_lock:
            for i in range(0, len(docs), batch):
                db.add_documents(docs[i:i + batch])

    def add_parent_child_text(
        self,
        user_id: int,
        session_id: int,
        *,
        source: str,
        text: str,
        original_name: str = None,
    ) -> Tuple[int, int]:
        """单个临时文件以父子结构入库（embedding 在锁外并行计算，写入持写锁串行）。

        功能：获取会话库后委托 app/infrastructure/vector_store/persistent.add_parent_child 完成
              切分、embedding（锁外远程调用）与父子块写入（持本 store 的
              _write_lock 串行），scope 固定为 "temp"。
        被谁调用：app/application/files/file_service.py 的 FileService.process_temp_file。
        参数：
        - user_id/session_id：会话复合键（JWT 用户 + per-user 会话号）。
        - source (str)：文件物理路径，作为向量块 source 元数据。
        - text (str)：已脱敏的文件文本（来源：process_temp_file 的 mask_text 输出）。
        - original_name (str|None)：用户上传原始文件名。
        返回：Tuple[int, int]——(父块数, 子块数)；去向：file_service 组装
              上传结果。数据去向：会话 chroma 持久目录。
        """
        # 局部 import 规避 service 层之间的循环导入
        from app.infrastructure.vector_store.persistent import add_parent_child

        db = self.get_db(user_id, session_id)
        return add_parent_child(
            db,
            self._write_lock,
            source=source,
            scope="temp",
            text=text,
            user_id=user_id,
            session_id=session_id,
            original_name=original_name,
        )

    def has_session(self, user_id: int, session_id: int) -> bool:
        """注册表已加载，或磁盘存在持久化向量库（进程重启后未访问过）即视为有会话。

        被谁调用：app/domain/agents/retrieval.py 检索前判定、
                  app/domain/tools/business/knowledge_business.py 的 session_file_search。
        参数：user_id/session_id——会话复合键。
        返回：bool——True 表示该会话存在临时库（内存已加载或磁盘 chroma
              目录存在），调用方才执行临时库检索；False 返回空召回。
        """
        key = self._key(user_id, session_id)
        with self._lock:
            if key in self._dbs:
                return True
        # 注册表未命中时查磁盘：覆盖进程重启后尚未访问过该会话的场景
        return self._chroma_dir(key[0], key[1]).is_dir()

    def list_files(self, user_id: int, session_id: int) -> List[str]:
        """列出当前会话临时目录中的文件名（物理文件为准）。

        被谁调用：需要展示会话已传临时文件清单的场景（辅助查询）。
        参数：user_id/session_id——会话复合键。
        返回：List[str]——排序后的普通文件名；目录不存在时返回空列表
              （chroma/ 子目录不是普通文件，不会出现在结果中）。
        """
        d = self._root / self._dir_name(user_id, session_id)
        if not d.is_dir():
            return []
        return sorted(f.name for f in d.iterdir() if f.is_file())

    def drop(self, user_id: int, session_id: int) -> int:
        """销毁会话临时知识库：释放向量库实例并删除目录（含持久化向量+物理文件）。

        功能：从注册表弹出并释放 Chroma 实例（del + gc.collect 释放
              Windows 下 sqlite 句柄），统计并递归删除整个会话目录；
              目录删除失败仅告警（容忍残留，不阻断删除会话主流程）。
        被谁调用：app/api/v1/history.py 删除会话接口。
        参数：user_id/session_id——待销毁会话的复合键。
        返回：int——删除的物理文件数（统计口径为删前目录内普通文件）。
        """
        key = self._key(user_id, session_id)
        with self._lock:
            db = self._dbs.pop(key, None)
        # 释放 Chroma 持久化实例；Windows 下 sqlite 文件句柄需先释放才能删目录
        del db
        gc.collect()
        count = 0
        session_dir = self._root / self._dir_name(key[0], key[1])
        if session_dir.is_dir():
            count = sum(1 for f in session_dir.iterdir() if f.is_file())
            try:
                shutil.rmtree(session_dir, ignore_errors=True)
            except OSError as e:
                logger.error("remove temp dir failed: %s", e)
        logger.info(
            "temp knowledge dropped for user %s session %s, files=%d", key[0], key[1], count
        )
        return count

    def relocate(self, user_id: int, old_session_id: int, new_session_id: int) -> bool:
        """会话自动滚换：把旧会话临时知识库复制到新会话。

        Windows 下 Chroma 持久化客户端的 sqlite 句柄无法通过 del+gc 立即释放
        （chromadb 按路径缓存 System 单例），move 目录 / rename collection 都会
        触发 WinError 32。因此采用"复制式迁移"，全程不删除、不改名旧库：
        - 物理文件 copy2 到新会话目录（文件无句柄占用）；
        - 向量数据经底层 client get(include=embeddings/...) 全量读出，连同
          已计算好的 embedding 原样写入新库（不重新调用 embedding API），
          数据量为单次会话上传内容（通常几十到几百 chunk），秒级完成；
        - 旧库目录保留，随旧会话被用户删除时由 drop() 一并清理
          （与既有删除路径的容忍策略一致）。

        被谁调用：app/domain/memory/session_rollover.py 在长会话自动滚换时调用。
        参数：
        - user_id (int)：会话归属用户 ID（滚换前后不变）。
        - old_session_id (int)：触发滚换的旧会话 ID。
        - new_session_id (int)：刚创建的新会话 ID。
        返回：bool——True 已迁移（物理文件 + 向量原样复制，不重算 embedding）；
              False 旧会话本无临时目录，无需迁移。
        异常：目标新会话目录意外已存在时抛 RuntimeError（理论不可达，
              遇此放弃复制以避免覆盖）。
        """
        old_dir = self._root / self._dir_name(user_id, old_session_id)
        if not old_dir.is_dir():
            return False

        new_dir = self._root / self._dir_name(user_id, new_session_id)
        if new_dir.exists():
            # 理论不可达（新会话刚创建）；遇此情况放弃复制，避免覆盖
            raise RuntimeError(f"relocate target already exists: {new_dir}")
        new_dir.mkdir(parents=True, exist_ok=True)

        # 1) 物理文件复制（仅普通文件，chroma/ 子目录由下面的向量迁移重建）
        for f in old_dir.iterdir():
            if f.is_file():
                shutil.copy2(str(f), str(new_dir / f.name))

        # 2) 向量复制：旧 collection 全量读（含原 embedding）→ 新 collection
        old_chroma = old_dir / "chroma"
        if old_chroma.is_dir():
            old_name = f"temp_u{int(user_id or 0)}_s{int(old_session_id or 0)}"
            new_name = f"temp_u{int(user_id or 0)}_s{int(new_session_id or 0)}"
            src_client = chromadb.PersistentClient(path=str(old_chroma))
            try:
                src_col = src_client.get_collection(old_name)
            except Exception:
                src_col = None
            if src_col is not None:
                data = src_col.get(include=["embeddings", "documents", "metadatas"])
                ids = data.get("ids") or []
                if ids:
                    # 经 get_db 以标准方式创建新库（collection 名=新 sid，
                    # 携带项目 embedding function，供后续检索直接使用）
                    self.get_db(user_id, new_session_id)
                    dst_client = chromadb.PersistentClient(
                        path=str(new_dir / "chroma")
                    )
                    # 直接取 collection 且不传 embedding_function：
                    # add 时显式携带原 embeddings，chroma 不会再调用 EF
                    # （langchain DashScopeEmbeddings 不满足新版 chroma 的 EF 签名校验）
                    dst_col = dst_client.get_collection(new_name)
                    dst_col.add(
                        ids=ids,
                        embeddings=data.get("embeddings"),
                        documents=data.get("documents"),
                        metadatas=data.get("metadatas"),
                    )
                    logger.info(
                        "temp knowledge vectors copied: user=%s %s -> %s, chunks=%d",
                        user_id, old_session_id, new_session_id, len(ids),
                    )
        logger.info(
            "temp knowledge relocated (copy mode): user=%s session %s -> %s",
            user_id, old_session_id, new_session_id,
        )
        return True


@lru_cache(maxsize=1)
def get_temp_store() -> TempKnowledgeStore:
    """TempKnowledgeStore 应用级单例工厂。

    功能：lru_cache 保证全进程一个临时库注册表实例；根目录固定为
          settings.UPLOAD_DIR/temp（uploads/temp）。
    被谁调用：app/api/v1/files.py、app/api/v1/history.py、
              app/application/files/file_service.py、app/domain/agents/retrieval.py、
              app/domain/tools/business/knowledge_business.py、app/domain/memory/session_rollover.py。
    返回：TempKnowledgeStore 唯一实例。
    """
    root = Path(settings.UPLOAD_DIR) / "temp"
    return TempKnowledgeStore(root)
