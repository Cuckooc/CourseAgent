"""
模块名：tests/phase/test_dedup_version.py。

知识库去重与版本管理机制单元脚本测试（脚本式用例：模块导入即顺序执行 9 个段落，
全局 PASS/FAIL + check() 汇总，末尾 sys.exit(0/1)；pytest 收集时同样按脚本执行）。
不依赖外部 LLM / DashScope API / MySQL / Redis：向量库交互用 MagicMock 替身，
仅第 8 节用 tempfile 临时目录起一个真实 chromadb 本地实例（不发起网络请求）。

覆盖段落（内嵌测试点是模块级语句，非 pytest test_ 函数）：
1. _cosine_from_l2 — L2 距离转余弦相似度（0/√2/阈值/相反/单调边界）
2. _build_dedup_where — scope + user_id 隔离条件构建（public/private/None）
3. build_scope_filter — 检索范围过滤（$or 结构、不含非法 $exists/is_latest）
4. _base_metadata / build_parent_child_documents — 元数据字段完整性与父子块传播
5. _check_duplicate — full/filename 去重策略（skip/replace/new 八组分支）
6. replace_document — 先删后增 + add 失败回滚（mock collection）
7. add_new_version — 版本标记（is_latest 切换 + superseded_at；无旧版不 update）
8. _purge_expired_old_versions — 三年过期清理（临时目录真实 Chroma）
9. 配置默认值 — DEDUP/UPDATE 策略、相似度阈值、保留天数

被测对象来源：service/vector_store.py、service/file_service.py、
embedding/text_embedding.py、embedding/parent_child.py、
core/purge_scheduler.py、core/config.py。

运行方式：
    python tests/phase/test_dedup_version.py
    # 文件头 sys.path.insert、os.environ.setdefault("jwt_secret", ...) 与
    # load_dotenv(env/qianwen_config.env) 为 import 引导，不可改动；
    # 无需后端服务，第 8 节结束自动 rmtree 临时 Chroma 目录。

Mock/patch 替身说明：
- mock_db/mock_col（第 5 节）：替身 Chroma collection，按用例编排
  query/get 返回的 distances/metadatas，驱动 full/filename 各分支；
- mock_db2/mock_col2、mock_db3/mock_col3（第 6/7 节）：替身 collection，
  断言 delete/add/update 调用参数，并以 add.side_effect 模拟失败验证回滚；
- 第 8 节 patch get_persistent_db/persistent_lock/_flush_collection_index，
  把清理函数指向临时目录真实 Chroma 实例。
"""
import os
import sys
import time
import threading
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("jwt_secret", "test-secret-for-validation")

# 加载 qianwen_config.env（text_embedding 模块 import 时读取 api_key）
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "env", "qianwen_config.env"))

PASS = 0  # 全局通过断言计数
FAIL = 0  # 全局失败断言计数（末尾非 0 则 sys.exit(1)）


def check(name, condition):
    """断言辅助：累加全局 PASS/FAIL 并打印，不抛异常（保证 9 节全部跑完）。

    调用方：本脚本全部内嵌测试点。参数：name 用例名，condition 实际布尔判定。
    """
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# ====================== 1. _cosine_from_l2 ======================
print("\n=== 1. _cosine_from_l2 — L2 距离转余弦相似度 ===")

from service.vector_store import _cosine_from_l2

# 相同向量：L2=0 → cosine=1.0
check("L2=0 → cosine=1.0", abs(_cosine_from_l2(0.0) - 1.0) < 1e-9)

# 正交向量：L2=√2 → cosine=0.0
check("L2=√2 → cosine=0.0", abs(_cosine_from_l2(1.41421356) - 0.0) < 0.001)

# 相似度阈值 0.95 对应 L2=√0.1≈0.3162
sim_95 = _cosine_from_l2(0.3162)
check("L2=0.3162 → cosine≈0.95", abs(sim_95 - 0.95) < 0.001)

# 完全相反：L2=2 → cosine=-1.0
check("L2=2 → cosine=-1.0", abs(_cosine_from_l2(2.0) - (-1.0)) < 1e-9)

# 单调递减：L2 越大 cosine 越小
check("单调递减", _cosine_from_l2(0.1) > _cosine_from_l2(0.5) > _cosine_from_l2(1.0))


# ====================== 2. _build_dedup_where ======================
print("\n=== 2. _build_dedup_where — scope 隔离条件 ===")

from service.file_service import _build_dedup_where

# public scope：只过滤 scope=public
w = _build_dedup_where("public", None)
check("public: 单条件 scope", w == {"scope": "public"})

w = _build_dededup_where_pub = _build_dedup_where("public", 4475)
check("public: 忽略 user_id", w == {"scope": "public"})

# private scope：scope + user_id 双条件
w = _build_dedup_where("private", 4475)
check("private: $and 结构", "$and" in w)
check("private: scope=private", w["$and"][0] == {"scope": "private"})
check("private: user_id 隔离", w["$and"][1] == {"user_id": 4475})

# private scope, user_id=None
w = _build_dedup_where("private", None)
check("private None: user_id=0", w["$and"][1] == {"user_id": 0})


# ====================== 3. build_scope_filter ======================
print("\n=== 3. build_scope_filter — 检索范围过滤（仅 scope 维度） ===")

from embedding.text_embedding import build_scope_filter

f = build_scope_filter(user_id=4475)
# is_latest 不能放进 Chroma where（不支持 $exists，且历史块无该字段，曾导致
# 整个 query 抛错、登录用户 RAG 全量静默失效），旧版本块由检索层 Python 侧过滤
check("scope: $or 结构", "$or" in f)
check("scope: public 条件", {"scope": "public"} in f["$or"])
private_cond = {"$and": [{"scope": "private"}, {"user_id": 4475}]}
check("scope: private+user_id 条件", private_cond in f["$or"])
check("filter: 不含非法 $exists", "$exists" not in str(f))
check("filter: 不含 is_latest 条件", "is_latest" not in str(f))

# user_id=None：仅 public
f_no_user = build_scope_filter(user_id=None)
check("no user: scope=public", f_no_user == {"scope": "public"})


# ====================== 4. _base_metadata / build_parent_child_documents ======================
print("\n=== 4. metadata 字段完整性 ===")

from embedding.parent_child import _base_metadata, build_parent_child_documents, make_file_id

meta = _base_metadata(
    source="/tmp/test.txt", file_id="abc123", scope="private",
    user_id=4475, session_id=99, original_name="test.txt",
    content_hash="hash123", version=2, updated_at=1234567890.0,
)
check("meta: source", meta["source"] == "/tmp/test.txt")
check("meta: file_id", meta["file_id"] == "abc123")
check("meta: scope=private", meta["scope"] == "private")
check("meta: version=2", meta["version"] == 2)
check("meta: is_latest=True", meta["is_latest"] is True)
check("meta: content_hash", meta["content_hash"] == "hash123")
check("meta: updated_at", meta["updated_at"] == 1234567890.0)
check("meta: original_name", meta["original_name"] == "test.txt")
check("meta: user_id", meta["user_id"] == 4475)
check("meta: session_id", meta["session_id"] == 99)

# is_latest=False 场景
meta_old = _base_metadata(
    source="/tmp/old.txt", file_id="old456", scope="private",
    user_id=1, session_id=None, original_name=None,
    is_latest=False,
)
check("meta_old: is_latest=False", meta_old["is_latest"] is False)
check("meta_old: 无 original_name", "original_name" not in meta_old)

# build_parent_child_documents：父子块结构 + 元数据传播
text = "这是一段测试文本，用于验证父子块切分。" * 10
from embedding.parent_child import split_parent_child
pairs = split_parent_child(text)
fid = make_file_id("/tmp/test.txt")
parents, children = build_parent_child_documents(
    pairs, source="/tmp/test.txt", scope="private", user_id=4475,
    original_name="test.txt", file_id=fid, content_hash="ch1", version=3,
    updated_at=999.0,
)
check("build: 有父块", len(parents) > 0)
check("build: 有子块", len(children) > 0)
check("build: 父块 id 前缀", parents[0][0].startswith(f"{fid}-p"))
check("build: 子块 id 前缀", children[0][0].startswith(f"{fid}-p"))
check("build: 父 metadata doc_level=parent", parents[0][1].metadata["doc_level"] == "parent")
check("build: 子 metadata doc_level=child", children[0][2].metadata["doc_level"] == "child")
check("build: 父 metadata file_id 一致", parents[0][1].metadata["file_id"] == fid)
check("build: 子 metadata file_id 一致", children[0][2].metadata["file_id"] == fid)
check("build: 父 metadata version=3", parents[0][1].metadata["version"] == 3)
check("build: 父 metadata content_hash", parents[0][1].metadata["content_hash"] == "ch1")
check("build: 父 metadata updated_at", parents[0][1].metadata["updated_at"] == 999.0)
check("build: 父 metadata is_latest=True", parents[0][1].metadata["is_latest"] is True)
check("build: 子 metadata parent_id 关联", children[0][1] == children[0][2].metadata["parent_id"])


# ====================== 5. _check_duplicate ======================
print("\n=== 5. _check_duplicate — 去重判定逻辑 ===")

from unittest.mock import MagicMock, patch
from service.file_service import FileService, _list_existing_files

fs = FileService()

# --- 5.1 full 策略：高相似 → skip ---
# 被替身外部依赖：Chroma collection（query 返回编排好的 distance=0.1≈cosine 0.995）
mock_db = MagicMock()
mock_col = MagicMock()
mock_db._collection = mock_col
mock_db._lock = threading.RLock()

# 模拟 chroma query 返回：distance 小 → 相似度高
mock_col.query.return_value = {
    "ids": [["id1"]],
    "distances": [[0.1]],  # L2=0.1 → cosine ≈ 0.995 ≥ 0.95
    "metadatas": [[{"source": "/tmp/existing.txt"}]],
}
result = fs._check_duplicate(mock_db, "test.txt", "hash1", [[0.1]*10], "private", 4475, "full")
check("full: 高相似 skip", result["action"] == "skip")
check("full: 返回 old_source", result.get("old_source") == "/tmp/existing.txt")

# --- 5.2 full 策略：低相似 → new ---
mock_col.query.return_value = {
    "ids": [["id2"]],
    "distances": [[1.5]],  # L2=1.5 → cosine ≈ -0.125 < 0.95
    "metadatas": [[{"source": "/tmp/other.txt"}]],
}
result = fs._check_duplicate(mock_db, "test.txt", "hash1", [[0.1]*10], "private", 4475, "full")
check("full: 低相似 new", result["action"] == "new")

# --- 5.3 full 策略：空 embedding ---
result = fs._check_duplicate(mock_db, "test.txt", "hash1", [], "private", 4475, "full")
check("full: 空 embedding → new", result["action"] == "new")

# --- 5.4 filename 策略：无匹配文件 → new ---
mock_col.get.return_value = {"ids": [], "metadatas": []}
mock_col.query.return_value = {
    "ids": [[]], "distances": [[]], "metadatas": [[]],
}
result = fs._check_duplicate(mock_db, "newfile.txt", "hash1", [[0.1]*10], "private", 4475, "filename")
check("filename: 无匹配 → new", result["action"] == "new")

# --- 5.5 filename 策略：同名 + content_hash 相同 → skip ---
mock_col.get.return_value = {
    "ids": ["p1"],
    "metadatas": [{
        "source": "/tmp/test.txt", "original_name": "test.txt",
        "content_hash": "same_hash", "file_id": "fid1", "version": 1,
        "doc_level": "parent", "scope": "private", "user_id": 4475,
    }],
}
result = fs._check_duplicate(mock_db, "test.txt", "same_hash", [[0.1]*10], "private", 4475, "filename")
check("filename: content_hash 相同 → skip", result["action"] == "skip")

# --- 5.6 filename 策略：同名 + content_hash 不同 + 向量高相似 → skip ---
mock_col.get.return_value = {
    "ids": ["p1"],
    "metadatas": [{
        "source": "/tmp/test.txt", "original_name": "test.txt",
        "content_hash": "old_hash", "file_id": "fid1", "version": 1,
        "doc_level": "parent", "scope": "private", "user_id": 4475,
    }],
}
mock_col.query.return_value = {
    "ids": [["id1"]],
    "distances": [[0.2]],  # cosine ≈ 0.98 ≥ 0.95
    "metadatas": [[{"source": "/tmp/test.txt"}]],
}
result = fs._check_duplicate(mock_db, "test.txt", "new_hash", [[0.1]*10], "private", 4475, "filename")
check("filename: 向量高相似 → skip", result["action"] == "skip")

# --- 5.7 filename 策略：同名 + content_hash 不同 + 向量低相似 → replace ---
mock_col.query.return_value = {
    "ids": [["id1"]],
    "distances": [[1.5]],  # cosine ≈ -0.125 < 0.95
    "metadatas": [[{"source": "/tmp/test.txt"}]],
}
result = fs._check_duplicate(mock_db, "test.txt", "new_hash", [[0.1]*10], "private", 4475, "filename")
check("filename: 向量低相似 → replace", result["action"] == "replace")
check("filename: replace 返回 old_source", result.get("old_source") == "/tmp/test.txt")
check("filename: replace 返回 old_file_id", result.get("old_file_id") == "fid1")
check("filename: replace 返回 old_version", result.get("old_version") == 1)

# --- 5.8 filename 策略：文件名相似度不够（< 0.95）→ new ---
mock_col.get.return_value = {
    "ids": ["p1"],
    "metadatas": [{
        "source": "/tmp/totally_different.txt", "original_name": "totally_different.txt",
        "content_hash": "h", "file_id": "fid2", "version": 1,
        "doc_level": "parent", "scope": "private", "user_id": 4475,
    }],
}
result = fs._check_duplicate(mock_db, "test.txt", "new_hash", [[0.1]*10], "private", 4475, "filename")
check("filename: 文件名不相似 → new", result["action"] == "new")


# ====================== 6. replace_document — 策略A ======================
print("\n=== 6. replace_document — 先删后增+回滚 ===")

from service.vector_store import replace_document

mock_db2 = MagicMock()
mock_col2 = MagicMock()
mock_db2._collection = mock_col2
lock2 = threading.RLock()

# --- 6.1 正常替换流程 ---
# 替身 collection：get 返回 2 个旧块，add/delete/flush 均为成功桩，
# 用 patch 替身 _flush_collection_index 避免触发真实索引落盘
mock_col2.get.return_value = {
    "ids": ["old1", "old2"],
    "embeddings": [[0.1]*4, [0.2]*4],
    "documents": ["old doc1", "old doc2"],
    "metadatas": [{"source": "/tmp/old.txt", "version": 1}, {"source": "/tmp/old.txt", "version": 1}],
}
mock_col2.add.return_value = None
mock_col2.delete.return_value = None

with patch("service.vector_store._flush_collection_index") as mock_flush:
    replace_document(
        mock_db2, lock2,
        old_source="/tmp/old.txt",
        new_ids=["new1"], new_embs=[[0.5]*4],
        new_docs=["new doc"], new_metas=[{"source": "/tmp/new.txt"}],
    )
check("replace: 删除旧块调用", mock_col2.delete.called)
check("replace: 删除用旧 ids", mock_col2.delete.call_args.kwargs.get("ids") == ["old1", "old2"])
check("replace: 添加新块调用", mock_col2.add.called)
check("replace: 新 ids 正确", mock_col2.add.call_args.kwargs.get("ids") == ["new1"])
check("replace: flush 调用", mock_flush.called)

# --- 6.2 add 失败 → 回滚 ---
mock_col2.reset_mock()
mock_col2.get.return_value = {
    "ids": ["old1"],
    "embeddings": [[0.1]*4],
    "documents": ["old doc"],
    "metadatas": [{"source": "/tmp/old.txt", "version": 1}],
}
mock_col2.add.side_effect = RuntimeError("add failed")

with patch("service.vector_store._flush_collection_index"):
    try:
        replace_document(
            mock_db2, lock2,
            old_source="/tmp/old.txt",
            new_ids=["new1"], new_embs=[[0.5]*4],
            new_docs=["new doc"], new_metas=[{"source": "/tmp/new.txt"}],
        )
        raised = False
    except RuntimeError:
        raised = True
check("replace: add 失败抛异常", raised)
# 回滚：应该用旧 ids/embeddings/documents 重新 add
rollback_call = mock_col2.add.call_args_list[-1]
check("replace: 回滚用旧 ids", rollback_call.kwargs.get("ids") == ["old1"])
check("replace: 回滚用旧 embeddings", rollback_call.kwargs.get("embeddings") == [[0.1]*4])


# ====================== 7. add_new_version — 策略B ======================
print("\n=== 7. add_new_version — 版本标记 ===")

from service.vector_store import add_new_version

mock_db3 = MagicMock()
mock_col3 = MagicMock()
mock_db3._collection = mock_col3
lock3 = threading.RLock()

# 模拟旧版本查询返回
# 替身 collection：get 返回 2 个旧块 id，验证 add_new_version 把它们
# update 为 is_latest=False 并写入 superseded_at
mock_col3.get.return_value = {"ids": ["old_p1", "old_c1"]}
mock_col3.add.return_value = None
mock_col3.update.return_value = None

ts = time.time()
with patch("service.vector_store._flush_collection_index"):
    add_new_version(
        mock_db3, lock3,
        old_file_id="old_fid",
        new_ids=["new_p1", "new_c1"],
        new_embs=[[0.1]*4, [0.2]*4],
        new_docs=["parent", "child"],
        new_metas=[{"file_id": "new_fid", "is_latest": True}, {"file_id": "new_fid", "is_latest": True}],
        superseded_at=ts,
    )

check("version: 添加新版本调用 add", mock_col3.add.called)
check("version: 新 ids 正确", mock_col3.add.call_args.kwargs.get("ids") == ["new_p1", "new_c1"])
check("version: 查询旧版本用 $and", mock_col3.get.call_args.kwargs.get("where", {}).get("$and") is not None)

# 旧版本标记 is_latest=False + superseded_at
update_metas = mock_col3.update.call_args.kwargs.get("metadatas", [])
check("version: 更新旧块数量=2", len(update_metas) == 2)
check("version: 旧块 is_latest=False", all(m["is_latest"] is False for m in update_metas))
check("version: 旧块 superseded_at 设置", all(m["superseded_at"] == ts for m in update_metas))

# --- 7.2 无旧版本时（新文件首次上传），不 update ---
mock_col3.reset_mock()
mock_col3.get.return_value = {"ids": []}  # 无旧版本
mock_col3.add.return_value = None

with patch("service.vector_store._flush_collection_index"):
    add_new_version(
        mock_db3, lock3,
        old_file_id="no_old",
        new_ids=["new1"], new_embs=[[0.1]*4],
        new_docs=["doc"], new_metas=[{"is_latest": True}],
        superseded_at=ts,
    )
check("version: 无旧版本不调用 update", not mock_col3.update.called)


# ====================== 8. _purge_expired_old_versions ======================
print("\n=== 8. _purge_expired_old_versions — 三年过期清理 ===")

# 用临时目录构建真实 chromadb 实例进行测试
from langchain_chroma import Chroma
from langchain_community.embeddings import DashScopeEmbeddings

tmp_dir = tempfile.mkdtemp(prefix="test_chroma_")
try:
    emb = DashScopeEmbeddings(model="text-embedding-v2", dashscope_api_key=os.getenv("api_key", "dummy"))
    test_db = Chroma(persist_directory=tmp_dir, embedding_function=emb)
    test_col = test_db._collection
    test_lock = threading.RLock()

    now = time.time()
    expired_ts = now - 1100 * 86400  # 超过 3 年
    recent_ts = now - 10 * 86400     # 仅 10 天前

    # 写入：2 个过期旧版本块 + 1 个未过期旧版本块 + 1 个最新版本块
    test_col.add(
        ids=["exp1", "exp2", "recent1", "latest1"],
        embeddings=[[0.1]*4, [0.2]*4, [0.3]*4, [0.4]*4],
        documents=["expired1", "expired2", "recent_old", "latest"],
        metadatas=[
            {"source": "/tmp/old1.txt", "is_latest": False, "superseded_at": expired_ts, "file_id": "fid1", "doc_level": "parent"},
            {"source": "/tmp/old1.txt", "is_latest": False, "superseded_at": expired_ts, "file_id": "fid1", "doc_level": "child"},
            {"source": "/tmp/old2.txt", "is_latest": False, "superseded_at": recent_ts, "file_id": "fid2", "doc_level": "parent"},
            {"source": "/tmp/new.txt", "is_latest": True, "file_id": "fid3", "doc_level": "parent"},
        ],
    )

    # 验证写入
    all_data = test_col.get(include=["metadatas"])
    check("purge: 初始 4 块", len(all_data["ids"]) == 4)

    # mock get_persistent_db / persistent_lock 指向测试实例
    with patch("service.vector_store.get_persistent_db", return_value=test_db), \
         patch("service.vector_store.persistent_lock", return_value=test_lock), \
         patch("service.vector_store._flush_collection_index"):
        from core.purge_scheduler import _purge_expired_old_versions
        deleted_count = _purge_expired_old_versions()

    check("purge: 删除 2 个过期块", deleted_count == 2)

    # 验证：过期块已删除，未过期和最新块保留
    remaining = test_col.get(include=["metadatas"])
    remaining_ids = set(remaining["ids"])
    check("purge: 过期块 exp1 已删除", "exp1" not in remaining_ids)
    check("purge: 过期块 exp2 已删除", "exp2" not in remaining_ids)
    check("purge: 未过期块 recent1 保留", "recent1" in remaining_ids)
    check("purge: 最新块 latest1 保留", "latest1" in remaining_ids)

    # --- 8.2 无过期块时返回 0 ---
    with patch("service.vector_store.get_persistent_db", return_value=test_db), \
         patch("service.vector_store.persistent_lock", return_value=test_lock), \
         patch("service.vector_store._flush_collection_index"):
        from core.purge_scheduler import _purge_expired_old_versions
        deleted_count = _purge_expired_old_versions()
    check("purge: 无过期块返回 0", deleted_count == 0)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ====================== 9. 配置项默认值 ======================
print("\n=== 9. 配置项默认值 ===")

from core.config import settings

check("config: DEDUP_STRATEGY=filename", settings.DEDUP_STRATEGY == "filename")
check("config: UPDATE_STRATEGY=version", settings.UPDATE_STRATEGY == "version")
check("config: SIMILARITY_THRESHOLD > 0", settings.SIMILARITY_THRESHOLD > 0)
check("config: NAME_SIMILARITY_THRESHOLD=0.95", abs(settings.NAME_SIMILARITY_THRESHOLD - 0.95) < 1e-9)
check("config: OLD_VERSION_RETENTION_DAYS=1095", settings.OLD_VERSION_RETENTION_DAYS == 1095)


# ====================== 汇总 ======================
print(f"\n{'='*50}")
print(f"总计: {PASS} 通过, {FAIL} 失败")
print(f"{'='*50}")
sys.exit(0 if FAIL == 0 else 1)
