"""
模块名：migrations.env（Alembic 迁移运行环境脚本）

作用：
    Alembic 命令行（alembic upgrade / downgrade / revision --autogenerate 等）
    加载的环境入口。负责两件事：
    1. 把项目根插入 sys.path 并导入 db.models.Base，使 autogenerate 能拿到
       全部 ORM 表元数据（target_metadata = Base.metadata）；
    2. 提供离线/在线两种迁移执行模式，数据库连接串复用 db.session._DB_URL
       （来源 env/config.env 经 core/config.py Settings），alembic.ini 中
       不写明文密码。

主要成员：
    - target_metadata：迁移比对用的 SQLAlchemy 元数据（db.models.Base.metadata）；
    - _db_url()：返回与运行时一致的 mysql+pymysql 连接串；
    - run_migrations_offline()：只生成 SQL 不连库（alembic offline 模式）；
    - run_migrations_online()：建连接实际执行迁移（NullPool 短连接）。

被谁使用：
    不由业务代码 import；由 alembic 命令行在 migrations/ 目录下自动加载，
    执行方为运维/开发者命令 `alembic upgrade head`、`alembic downgrade -1`、
    `alembic revision --autogenerate -m "xxx"`（版本脚本位于 versions/）。
"""
import sys
from logging.config import fileConfig
from os.path import dirname

from sqlalchemy import engine_from_config, pool

from alembic import context

# 保证项目根在 sys.path（alembic 由任意 cwd 调用时也能导入项目包）
sys.path.insert(0, dirname(dirname(__file__)))

# 导入 ORM 基类：其 metadata 注册了 db/models.py 中全部表，供 autogenerate 比对
from db.models import Base  # noqa: E402

# Alembic 配置对象（对应 alembic.ini 与命令行参数）
config = context.config

# 按 alembic.ini 的 logging 配置初始化日志（仅在存在配置文件时）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的比对目标：db.models.Base.metadata（全部 ORM 表结构）
target_metadata = Base.metadata


def _db_url() -> str:
    """返回迁移用数据库连接串。

    被谁调用：run_migrations_offline / run_migrations_online 配置 context 时。
    返回：str，直接复用 db.session._DB_URL（env/config.env 的 host/port/user/
          password/database/charset 拼接，含 charset=utf8mb4），与运行时
          完全一致，避免在 alembic.ini 中明文写密码。
    """
    from db.session import _DB_URL

    return _DB_URL


def run_migrations_offline() -> None:
    """离线模式迁移：仅生成 SQL 脚本，不实际连接数据库。

    被谁调用：模块末尾根据 context.is_offline_mode() 分支调用（alembic
    -x offline 或 offline 命令场景）；literal_binds 把参数内联进 SQL，
    paramstyle=named 使用命名参数风格。执行的版本变更来自 versions/ 下
    各迁移脚本的 upgrade()/downgrade()。
    """
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式迁移：建立真实数据库连接并执行 DDL。

    被谁调用：模块末尾在线模式分支（最常用，对应 `alembic upgrade head`）。
    实现：把 _DB_URL() 注入配置段后用 engine_from_config 建引擎，连接池用
    NullPool（迁移为一次性短任务，不需常驻连接）；在单事务内
    context.run_migrations() 顺序执行版本链上各 upgrade()/downgrade()。
    """
    configuration = config.get_section(config.config_ini_section, {})
    # 运行时注入连接串，优先级高于 alembic.ini（ini 内不存密码）
    configuration["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


# Alembic 加载本脚本时的模式分发：离线只出 SQL，在线连库执行
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
