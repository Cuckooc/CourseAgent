"""
模块名：app.api.deps

作用：API 层的依赖注入组合根（Composition Root）。
    负责把 Application Port 装配到具体 Infrastructure Adapter，
    使路由层只依赖抽象（FastAPI Depends）而非直接 import 业务单例。

当前阶段为骨架实现：对既有单例工厂做简单委派，
    后续可在此替换为显式实例化或按配置切换 Adapter。
"""
from app.application.ports.kv import register_cycle_lock, register_redis_provider
from app.application.ports.llm import register_chat_model_builder
from app.application.ports import embeddings as _embeddings_ports
from app.application.ports import llm_business as _llm_business_ports
from app.application.ports import persistence as _persistence_ports
from app.infrastructure.persistence.repositories import (
    chain_log as _chain_log_impl,
    document_review as _document_review_impl,
    feedback as _feedback_impl,
    history as _history_impl,
    information as _information_impl,
    knowledge as _knowledge_impl,
    profile as _profile_impl,
    read as _read_impl,
    session as _session_impl,
    session_keyword as _session_keyword_impl,
    soft_delete as _soft_delete_impl,
    user as _user_impl,
)
from app.infrastructure.persistence.session import session_scope as _session_scope_impl
from app.infrastructure.embeddings import (
    embedding_model as _embedding_model_impl,
    parent_child as _parent_child_impl,
    text_embedding as _text_embedding_impl,
)
from core.config import settings
from app.application.ports.vector import (
    get_persistent_db,
    get_temp_store,
    register_flush_persistent_index,
    register_persistent_db_provider,
    register_persistent_lock_provider,
    register_persistent_ops,
    register_temp_store_provider,
)
from app.infrastructure.llm import llm_business as _llm_business_impl
from app.infrastructure.llm.gateway import build_chat_model as _build_chat_model_impl
from app.infrastructure.redis.locks import try_acquire_cycle_lock as _cycle_lock_impl
from app.infrastructure.redis.redis_client import get_redis as _get_redis_impl
import app.infrastructure.vector_store.persistent as _persistent_ops_impl
from app.infrastructure.vector_store.persistent import (
    flush_persistent_index as _flush_persistent_index_impl,
    get_persistent_db as _get_persistent_db_impl,
    persistent_lock as _persistent_lock_impl,
)
from app.infrastructure.vector_store.temp_store import get_temp_store as _get_temp_store_impl

# Port↔Adapter 装配：LLM 网关 Port ← infrastructure 网关实现
# （模块导入即注册；app/main.py 在路由导入前 import 本模块，保证请求路径可用）
register_chat_model_builder(_build_chat_model_impl)

# Port↔Adapter 装配：Redis 键值/周期锁 Port ← infrastructure Redis 实现
register_redis_provider(_get_redis_impl)
register_cycle_lock(_cycle_lock_impl)

# Port↔Adapter 装配：向量库 Port ← infrastructure vector_store 实现
register_temp_store_provider(_get_temp_store_impl)
register_persistent_db_provider(_get_persistent_db_impl)
register_persistent_lock_provider(_persistent_lock_impl)
register_flush_persistent_index(_flush_persistent_index_impl)
register_persistent_ops(_persistent_ops_impl)

# Port↔Adapter 装配：MySQL 持久化 DAO Port ← infrastructure repositories 实现
# （DAO 类均为无参构造的无状态类，直接以类本身为零参工厂注册；
#  KnowledgeDAO 构造需 upload_dir，以闭包注入 settings.UPLOAD_DIR）
_persistence_ports.register_session_dao(_session_impl.SessionDAO)
_persistence_ports.register_read_dao(_read_impl.Information_Read)
_persistence_ports.register_user_dao(_user_impl.Information)
_persistence_ports.register_information_dao(_information_impl.Information)
_persistence_ports.register_history_dao(_history_impl.Information_history)
_persistence_ports.register_feedback_dao(_feedback_impl.FeedbackDAO)
_persistence_ports.register_knowledge_dao(lambda: _knowledge_impl.KnowledgeDAO(settings.UPLOAD_DIR))
_persistence_ports.register_chain_log_dao(_chain_log_impl.ChainLogDAO)
_persistence_ports.register_profile_dao(_profile_impl.ProfileDAO)
_persistence_ports.register_session_keyword_dao(_session_keyword_impl.SessionKeywordDAO)
_persistence_ports.register_document_review_dao(_document_review_impl.DocumentReviewDAO)
_persistence_ports.register_build_stored_filename(_knowledge_impl.build_stored_filename)
_persistence_ports.register_recover_last_deleted(_soft_delete_impl.recover_last_deleted)
_persistence_ports.register_session_scope(_session_scope_impl)

# Port↔Adapter 装配：嵌入/切块 Port ← infrastructure embeddings 实现
_embeddings_ports.register_embedding_provider(_text_embedding_impl.get_embedding)
_embeddings_ports.register_retrying_embedding_provider(_embedding_model_impl.get_embedding)
_embeddings_ports.register_text_embedding_ops(_text_embedding_impl)
_embeddings_ports.register_parent_child_ops(_parent_child_impl)

# Port↔Adapter 装配：业务提示词链 Port ← infrastructure llm_business 实现
# （必须先于下方 app 服务导入完成：title.py 在类定义期经 Port 取 TitleLLM 基类）
_llm_business_ports.register_llm_business_ops(_llm_business_impl)

# 应用服务导入须位于全部 Port 注册之后：其模块级类定义/单例构造可能经
# Port 取实现（如 title.Title 继承 get_title_llm_cls()）
from app.application.chat.chat_service import get_chat_service  # noqa: E402
from app.application.knowledge.knowledge_service import get_knowledge_service  # noqa: E402

__all__ = [
    "get_chat_service",
    "get_knowledge_service",
    "get_temp_store",
    "get_persistent_db",
]
