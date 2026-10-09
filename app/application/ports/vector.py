"""
模块名：app.application.ports.vector

作用：
    向量库能力的运行时装配点（服务定位器）。application/domain/api 层的
    文件入库、检索、审核、会话滚换等流程统一经本模块获取临时/持久向量库
    与入库原语，不直接 import app.infrastructure.vector_store.*（分层守卫
    RULES 禁止业务层依赖基础设施）；具体实现由组合根 app/api/deps.py 在
    应用启动时注册（Port↔Adapter 装配）。

主要成员：
    - register_temp_store_provider(provider) / get_temp_store()：
      会话级临时知识库注册表（TempKnowledgeStore 单例）；
    - register_persistent_db_provider(provider) / get_persistent_db()：
      应用级共享持久 Chroma 库；
    - register_persistent_lock_provider(provider) / persistent_lock()：
      持久库进程级写锁（threading.RLock）；
    - register_flush_persistent_index(fn) / flush_persistent_index()：
      关闭时兜底落盘 HNSW 尾部索引；
    - register_persistent_ops(ops) + add_parent_child / add_new_version /
      replace_document / find_max_similarity / embed_in_batches /
      l2_normalize：入库管线原语门面（ops 为实现模块对象，组合根注册
      infrastructure.vector_store.persistent 模块本身；两个批量工具在
      实现层为私有名 _embed_in_batches/_l2_normalize，门面以公开名透出）。

被谁使用：
    - 调用方：app/application/files/file_service.py、
      app/application/review/review_service.py、app/api/v1/{files,history}.py、
      app/domain/agents/{rag_agent,retrieval}.py、
      app/domain/memory/session_rollover.py、
      app/domain/tools/business/knowledge_business.py；
    - 装配方：app/api/deps.py（import 时执行各 register_*）。
"""
from typing import Any, Callable, Optional

__all__ = [
    "get_temp_store",
    "register_temp_store_provider",
    "get_persistent_db",
    "register_persistent_db_provider",
    "persistent_lock",
    "register_persistent_lock_provider",
    "flush_persistent_index",
    "register_flush_persistent_index",
    "register_persistent_ops",
    "add_parent_child",
    "add_new_version",
    "replace_document",
    "find_max_similarity",
    "embed_in_batches",
    "l2_normalize",
]


# 已注册的实现（组合根装配前为 None）
_temp_store_provider: Optional[Callable[[], Any]] = None
_persistent_db_provider: Optional[Callable[[], Any]] = None
_persistent_lock_provider: Optional[Callable[[], Any]] = None
_flush_persistent_index: Optional[Callable[[], None]] = None
_persistent_ops: Any = None  # 入库原语实现模块（infrastructure.vector_store.persistent）


def _unloaded(what: str, registrar: str) -> RuntimeError:
    """组装「未装配」错误：组合根未注册实现属启动期配置错误。"""
    return RuntimeError(
        "向量库能力未装配：组合根 app/api/deps.py 未注册{}（{}）".format(what, registrar)
    )


def register_temp_store_provider(provider: Callable[[], Any]) -> None:
    """注册临时知识库注册表工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _temp_store_provider
    _temp_store_provider = provider


def get_temp_store() -> Any:
    """获取会话级临时知识库注册表（语义同 infrastructure.temp_store.get_temp_store）。

    返回：TempKnowledgeStore 应用级单例（lru_cache），按 (user_id, session_id)
          隔离会话临时库。
    """
    if _temp_store_provider is None:
        raise _unloaded("临时知识库", "register_temp_store_provider")
    return _temp_store_provider()


def register_persistent_db_provider(provider: Callable[[], Any]) -> None:
    """注册持久向量库工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _persistent_db_provider
    _persistent_db_provider = provider


def get_persistent_db() -> Any:
    """获取应用级共享持久化 Chroma 库（语义同 infrastructure.persistent.get_persistent_db）。"""
    if _persistent_db_provider is None:
        raise _unloaded("持久向量库", "register_persistent_db_provider")
    return _persistent_db_provider()


def register_persistent_lock_provider(provider: Callable[[], Any]) -> None:
    """注册持久库写锁工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _persistent_lock_provider
    _persistent_lock_provider = provider


def persistent_lock() -> Any:
    """返回持久化向量库的进程级写锁（threading.RLock，语义同 infrastructure 实现）。"""
    if _persistent_lock_provider is None:
        raise _unloaded("持久库写锁", "register_persistent_lock_provider")
    return _persistent_lock_provider()


def register_flush_persistent_index(fn: Callable[[], None]) -> None:
    """注册持久索引落盘函数（组合根启动时调用一次）。"""
    global _flush_persistent_index
    _flush_persistent_index = fn


def flush_persistent_index() -> None:
    """服务关闭时兜底：把 HNSW 不足 sync_threshold 的尾部索引强制落盘
    （语义同 infrastructure.persistent.flush_persistent_index）。"""
    if _flush_persistent_index is None:
        raise _unloaded("索引落盘", "register_flush_persistent_index")
    _flush_persistent_index()


def register_persistent_ops(ops: Any) -> None:
    """注册入库原语实现模块（组合根启动时调用一次，传 persistent 模块对象）。

    参数：ops —— 需具备 add_parent_child / add_new_version / replace_document /
          find_max_similarity / _embed_in_batches / _l2_normalize 属性
          （后两者为私有名，门面以公开名 embed_in_batches/l2_normalize 透出）。
    """
    global _persistent_ops
    _persistent_ops = ops


def add_parent_child(*args: Any, **kwargs: Any) -> Any:
    """父子块入库（签名与行为同 infrastructure.persistent.add_parent_child）。"""
    if _persistent_ops is None:
        raise _unloaded("入库原语", "register_persistent_ops")
    return _persistent_ops.add_parent_child(*args, **kwargs)


def add_new_version(*args: Any, **kwargs: Any) -> Any:
    """文档新版本入库（签名与行为同 infrastructure.persistent.add_new_version）。"""
    if _persistent_ops is None:
        raise _unloaded("入库原语", "register_persistent_ops")
    return _persistent_ops.add_new_version(*args, **kwargs)


def replace_document(*args: Any, **kwargs: Any) -> Any:
    """整文档替换入库（签名与行为同 infrastructure.persistent.replace_document）。"""
    if _persistent_ops is None:
        raise _unloaded("入库原语", "register_persistent_ops")
    return _persistent_ops.replace_document(*args, **kwargs)


def find_max_similarity(*args: Any, **kwargs: Any) -> Any:
    """向量最大相似度查询（签名与行为同 infrastructure.persistent.find_max_similarity）。"""
    if _persistent_ops is None:
        raise _unloaded("入库原语", "register_persistent_ops")
    return _persistent_ops.find_max_similarity(*args, **kwargs)


def embed_in_batches(*args: Any, **kwargs: Any) -> Any:
    """批量向量化（签名与行为同 infrastructure.persistent._embed_in_batches）。"""
    if _persistent_ops is None:
        raise _unloaded("入库原语", "register_persistent_ops")
    return _persistent_ops._embed_in_batches(*args, **kwargs)


def l2_normalize(*args: Any, **kwargs: Any) -> Any:
    """向量 L2 归一化（签名与行为同 infrastructure.persistent._l2_normalize）。"""
    if _persistent_ops is None:
        raise _unloaded("入库原语", "register_persistent_ops")
    return _persistent_ops._l2_normalize(*args, **kwargs)
