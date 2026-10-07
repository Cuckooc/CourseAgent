# DB 数据库层

数据库层包含 SQLAlchemy ORM 模型、连接池管理与增量迁移 SQL，使用 MySQL 8.0 + SQLAlchemy 2.0。

## 📁 目录结构

```
db/
├── models.py            # ORM 模型（各业务表定义）
├── session.py           # 引擎/连接池/会话工厂 + session_scope 事务管理器
└── migrations/          # 增量表结构迁移 SQL（按版本号排序执行）
    ├── 002_document_review.sql   # 文档审核表
    ├── 003_soft_delete.sql       # 软删除字段
    ├── 004_session_keywords.sql  # 会话关键词表
    ├── 005_chat_feedback.sql     # 反馈表
    ├── 006_chain_log.sql         # Agent 链路日志表
    └── 007_token_version.sql     # JWT 令牌版本号（吊销支持）
```

## 📄 关键文件说明

### `models.py` - ORM 模型

- **user_information**: 用户信息表（账号与鉴权主体，含 role 角色、token_version）
- 其余业务表模型：会话/消息/历史/画像/审核/反馈/链路日志等
- 全量建表脚本见仓库根 [course.sql](../course.sql)（Docker 首次初始化自动执行）

### `session.py` - 连接与会话管理

- MySQL 8.0 + SQLAlchemy 2.0，`QueuePool` 连接池
- `pool_pre_ping` + `pool_recycle` 保障 FastAPI 长连接稳定性
- `session_scope()` 事务上下文管理器：`with session_scope() as session:` 自动提交/回滚
- 全局 `engine` 在应用关闭时由 `control/app.py` lifespan 调用 `engine.dispose()` 释放

## 🔄 迁移说明

- `db/migrations/*.sql` 为手工增量迁移（002 起步，001 基线由 course.sql 覆盖）
- `migrations/` 目录为 **Alembic** 版本化迁移（Python），两套并存：
  - SQL 版：生产手工执行
  - Alembic 版：`alembic upgrade head`（配置见 alembic.ini）

## 🔗 依赖关系

- 被 `dao/` 全部数据访问层依赖
- 环境变量配置（连接串）见 [env/README.md](../env/README.md) 与 `core/config.py`
