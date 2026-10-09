# Migrations Alembic 版本迁移

使用 Alembic 管理 MySQL 表结构版本化迁移，`alembic.ini` 位于仓库根目录。

## 📁 目录结构

```
migrations/
├── env.py                    # Alembic 运行环境（返回迁移用数据库连接串）
├── script.py.mako            # 迁移脚本模板
└── versions/
    ├── 0001_baseline.py      # 基线（无 DDL，三张初始表由 course.sql 预建）
    ├── 0002_user_role.py     # user_information 增加 role 列
    ├── 0003_user_profile.py  # 创建 user_profile 表（含外键与注释）
    └── ...                   # 后续版本按序追加
```

## 🔧 使用方法

```bash
# 升级到最新
alembic upgrade head

# 查看当前版本
alembic current

# 新增迁移（修改 models.py 后）
alembic revision --autogenerate -m "描述"
```

> 注意：历史手工增量 SQL（原 `db/migrations/002~007`）已归档至 `legacy_sql/`（生产手工执行备用），与本目录 Alembic 迁移并存，表结构最终以 `app/infrastructure/persistence/models.py` 为准。
