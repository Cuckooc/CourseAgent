"""
模块名：app.api.deps

作用：API 层的依赖注入组合根（Composition Root）。
    负责把 Application Port 装配到具体 Infrastructure Adapter，
    使路由层只依赖抽象（FastAPI Depends）而非直接 import 业务单例。

当前阶段为骨架实现：对既有单例工厂做简单委派，
    后续可在此替换为显式实例化或按配置切换 Adapter。
"""
from app.application.chat.chat_service import get_chat_service
from app.application.knowledge.knowledge_service import get_knowledge_service
from app.application.ports.kv import register_cycle_lock, register_redis_provider
from app.application.ports.llm import register_chat_model_builder
from app.application.ports.vector import (
    get_persistent_db,
    get_temp_store,
    register_flush_persistent_index,
    register_persistent_db_provider,
    register_persistent_lock_provider,
    register_persistent_ops,
    register_temp_store_provider,
)
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

__all__ = [
    "get_chat_service",
    "get_knowledge_service",
    "get_temp_store",
    "get_persistent_db",
]
