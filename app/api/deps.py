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
from app.infrastructure.llm.gateway import build_chat_model as _build_chat_model_impl
from app.infrastructure.redis.locks import try_acquire_cycle_lock as _cycle_lock_impl
from app.infrastructure.redis.redis_client import get_redis as _get_redis_impl
from app.infrastructure.vector_store.temp_store import get_temp_store
from app.infrastructure.vector_store.persistent import get_persistent_db

# Port↔Adapter 装配：LLM 网关 Port ← infrastructure 网关实现
# （模块导入即注册；app/main.py 在路由导入前 import 本模块，保证请求路径可用）
register_chat_model_builder(_build_chat_model_impl)

# Port↔Adapter 装配：Redis 键值/周期锁 Port ← infrastructure Redis 实现
register_redis_provider(_get_redis_impl)
register_cycle_lock(_cycle_lock_impl)

__all__ = [
    "get_chat_service",
    "get_knowledge_service",
    "get_temp_store",
    "get_persistent_db",
]
