"""
模块名：app.infrastructure.persistence.session

作用：
    数据库引擎与会话管理（SQLAlchemy 2.0 + PyMySQL 驱动，目标库 MySQL 8.0）。
    - 模块导入期用 core.config.settings 的连接字段拼接 mysql+pymysql 连接串，
      创建全局唯一 Engine（内置 QueuePool 连接池，pool_pre_ping 剔除失效连接）；
    - SessionLocal：会话工厂；
    - session_scope()：请求/任务级事务边界上下文管理器，自动 commit /
      异常 rollback / 关闭会话；
    - get_db()：FastAPI 依赖注入用会话生成器（请求级 Session）。

连接串字段来源（env/config.env 经 core/config.py 的 Settings 读取）：
    host→DB_HOST(默认 localhost)、port→DB_PORT(3306)、user→DB_USER(root)、
    password→DB_PASSWORD、database→DB_NAME、charset→DB_CHARSET(utf8mb4)。
    密码经 urllib.parse.quote_plus 转义后再拼接，避免特殊字符破坏连接串。

主要成员：_DB_URL、engine、SessionLocal、session_scope()、get_db()。

被谁使用（Grep "from db.session import" 确认）：
    - 全部 DAO：dao/user.py、history.py、information.py、session.py、read.py、
      profile.py、feedback.py、chain_log.py、session_keyword.py、
      document_review.py、soft_delete.py（统一 with session_scope() as session）；
    - core/purge_scheduler.py（注销硬删除定时任务）、app/domain/memory/session_rollover.py、
      scripts/maintenance/cleanup_web.py；
    - control/app.py 启动/关闭时引用 engine 做连通性检查与连接池处置；
    - migrations/env.py 复用 _DB_URL 作为 alembic 迁移连接串；
    - tests/ 下夹具与集成测试。
"""
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from core.config import settings

# 拼接 SQLAlchemy 连接串（mysql+pymysql 方言）：
# 用户名/密码/主机/端口/库名/字符集全部来自 core.config.settings（env/config.env）；
# quote_plus 对密码做 URL 转义（如 @ : / 等字符），空密码兜底为空串
_DB_URL = (
    f"mysql+pymysql://{settings.DB_USER}:{quote_plus(settings.DB_PASSWORD or '')}"
    f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}?charset={settings.DB_CHARSET}"
)

# 全局唯一 Engine：QueuePool 连接池
# pool_size=10 常驻连接；max_overflow=20 峰值最多再溢出 20 条；
# pool_pre_ping=True 取连接前先发 ping，剔除 MySQL 8 小时断连等失效连接；
# pool_recycle=3600 每小时回收重建连接；pool_timeout=30 取连接最多等待 30s；
# echo=False 不向日志打印 SQL（SQL 审计由 core/sql_guard 等环节负责）
engine = create_engine(
    _DB_URL,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=30,
    echo=False,
)

# 会话工厂：autocommit=False 显式事务（由 session_scope 统一提交）；
# autoflush=False 禁止查询前自动 flush，避免触发非预期 SQL
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文管理器：with session_scope() as session: ...

    生命周期：进入时从 SessionLocal 取新 Session → with 块内正常结束自动
    commit → 任意异常自动 rollback 后重新抛出（保证事务原子性，如 core/sql_guard
    拦截非法 SQL 时整条事务随异常回滚）→ finally 始终 close 归还连接池。
    被谁调用：dao/ 下全部 DAO 的写/读方法、core/purge_scheduler.py、
    app/domain/memory/session_rollover.py、scripts/maintenance/cleanup_web.py 及测试夹具。
    返回：产出 SQLAlchemy Session（Iterator[Session]），仅在 with 块内有效，
    去向 DAO 内 text() 原生 SQL / ORM 操作；块结束后不得继续使用。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        # 任何异常都回滚事务（撤销本事务内全部未提交改动），再原样上抛由上层处理
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入：提供请求级 Session（不自动 commit）。

    生命周期：每请求创建一个 Session，请求结束 finally 关闭归还连接池；
    提交由路由/服务层显式控制（写操作主要走 DAO 的 session_scope 事务）。
    被谁调用：FastAPI 路由的 Depends(get_db) 依赖链。
    返回：产出 Session（Iterator[Session]），去向请求处理函数。
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
