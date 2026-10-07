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
from app.infrastructure.vector_store.temp_store import get_temp_store
from app.infrastructure.vector_store.persistent import get_persistent_db

__all__ = [
    "get_chat_service",
    "get_knowledge_service",
    "get_temp_store",
    "get_persistent_db",
]
