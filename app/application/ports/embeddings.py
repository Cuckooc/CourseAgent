"""
模块名：app.application.ports.embeddings

作用：
    向量嵌入与文档切块能力的运行时装配点（服务定位器）。api/application/
    domain 层的 RAG 检索、文件入库、上下文改写等流程统一经本模块获取
    embedding 客户端与切块原语，不直接 import
    app.infrastructure.embeddings.*（分层守卫 RULES 禁止业务层依赖基础
    设施）；具体实现由组合根 app/api/deps.py 在应用启动时注册
    （Port↔Adapter 装配）。

主要成员：
    - register_embedding_provider(provider) / get_embedding()：
      text_embedding 基础版 embedding 客户端（被 file_service / rag_agent /
      retrieval / context 使用）；
    - register_retrying_embedding_provider(provider) / get_retrying_embedding()：
      embedding_model 重试版 embedding 客户端（被 chat_service 使用）；
    - register_text_embedding_ops(ops) + build_scope_filter / build_chromadb /
      split_documents / load_json_data / json_to_documents：
      text_embedding 模块中的工具函数门面；
    - register_parent_child_ops(ops) + build_parent_child_documents /
      split_parent_child / make_file_id：
      parent_child 模块中的工具函数门面；
    - MAX_L2_DISTANCE：检索距离阈值（在 Port 层直接定义，与基础设施实现
      保持一致；基础设施变更时需同步更新）。

被谁使用：
    - 调用方：app/application/chat/{chat_service,context}.py、
      app/application/files/file_service.py、
      app/domain/agents/{rag_agent,retrieval}.py；
    - 装配方：app/api/deps.py（import 时执行各 register_*）。
"""
from typing import Any, Callable, Optional

__all__ = [
    "get_embedding",
    "register_embedding_provider",
    "get_retrying_embedding",
    "register_retrying_embedding_provider",
    "MAX_L2_DISTANCE",
    "build_scope_filter",
    "register_text_embedding_ops",
    "build_chromadb",
    "split_documents",
    "load_json_data",
    "json_to_documents",
    "build_parent_child_documents",
    "register_parent_child_ops",
    "split_parent_child",
    "make_file_id",
]

# 已注册的实现（组合根装配前为 None）
_embedding_provider: Optional[Callable[[], Any]] = None
_retrying_embedding_provider: Optional[Callable[[], Any]] = None
_text_embedding_ops: Any = None
_parent_child_ops: Any = None

# 检索距离阈值（与 infrastructure.embeddings.text_embedding 保持一致）
MAX_L2_DISTANCE: float = 1.15


def _unloaded(what: str, registrar: str) -> RuntimeError:
    """组装「未装配」错误：组合根未注册实现属启动期配置错误。"""
    return RuntimeError(
        "嵌入能力未装配：组合根 app/api/deps.py 未注册{}（{}）".format(what, registrar)
    )


# ---------------------------------------------------------------------------
# text_embedding 基础版 embedding 客户端
# ---------------------------------------------------------------------------

def register_embedding_provider(provider: Callable[[], Any]) -> None:
    """注册 text_embedding 基础版 embedding 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _embedding_provider
    _embedding_provider = provider


def get_embedding() -> Any:
    """获取 text_embedding 基础版 embedding 客户端（语义同 infrastructure.embeddings.text_embedding.get_embedding）。"""
    if _embedding_provider is None:
        raise _unloaded("embedding 客户端", "register_embedding_provider")
    return _embedding_provider()


# ---------------------------------------------------------------------------
# embedding_model 重试版 embedding 客户端
# ---------------------------------------------------------------------------

def register_retrying_embedding_provider(provider: Callable[[], Any]) -> None:
    """注册 embedding_model 重试版 embedding 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _retrying_embedding_provider
    _retrying_embedding_provider = provider


def get_retrying_embedding() -> Any:
    """获取 embedding_model 重试版 embedding 客户端（语义同 infrastructure.embeddings.embedding_model.get_embedding）。"""
    if _retrying_embedding_provider is None:
        raise _unloaded("重试版 embedding 客户端", "register_retrying_embedding_provider")
    return _retrying_embedding_provider()


# ---------------------------------------------------------------------------
# text_embedding 模块原语门面
# ---------------------------------------------------------------------------

def register_text_embedding_ops(ops: Any) -> None:
    """注册 text_embedding 模块原语（组合根启动时调用一次，传模块对象）。"""
    global _text_embedding_ops
    _text_embedding_ops = ops


def _text_ops() -> Any:
    if _text_embedding_ops is None:
        raise _unloaded("text_embedding 原语", "register_text_embedding_ops")
    return _text_embedding_ops


def build_scope_filter(*args: Any, **kwargs: Any) -> Any:
    """构建 Chroma 元数据 scope 过滤条件（语义同 text_embedding.build_scope_filter）。"""
    return _text_ops().build_scope_filter(*args, **kwargs)


def build_chromadb(*args: Any, **kwargs: Any) -> Any:
    """构建/加载 Chroma 库（语义同 text_embedding.build_chromadb）。"""
    return _text_ops().build_chromadb(*args, **kwargs)


def split_documents(*args: Any, **kwargs: Any) -> Any:
    """文档分块（语义同 text_embedding.split_documents）。"""
    return _text_ops().split_documents(*args, **kwargs)


def load_json_data(*args: Any, **kwargs: Any) -> Any:
    """加载 JSON 数据（语义同 text_embedding.load_json_data）。"""
    return _text_ops().load_json_data(*args, **kwargs)


def json_to_documents(*args: Any, **kwargs: Any) -> Any:
    """JSON 转 Document（语义同 text_embedding.json_to_documents）。"""
    return _text_ops().json_to_documents(*args, **kwargs)


# ---------------------------------------------------------------------------
# parent_child 模块原语门面
# ---------------------------------------------------------------------------

def register_parent_child_ops(ops: Any) -> None:
    """注册 parent_child 模块原语（组合根启动时调用一次，传模块对象）。"""
    global _parent_child_ops
    _parent_child_ops = ops


def _pc_ops() -> Any:
    if _parent_child_ops is None:
        raise _unloaded("parent_child 原语", "register_parent_child_ops")
    return _parent_child_ops


def build_parent_child_documents(*args: Any, **kwargs: Any) -> Any:
    """构建父子文档（语义同 parent_child.build_parent_child_documents）。"""
    return _pc_ops().build_parent_child_documents(*args, **kwargs)


def split_parent_child(*args: Any, **kwargs: Any) -> Any:
    """父子切块（语义同 parent_child.split_parent_child）。"""
    return _pc_ops().split_parent_child(*args, **kwargs)


def make_file_id(*args: Any, **kwargs: Any) -> Any:
    """生成文件 ID（语义同 parent_child.make_file_id）。"""
    return _pc_ops().make_file_id(*args, **kwargs)
