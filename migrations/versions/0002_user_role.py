"""
迁移版本：0002_user_role —— 为用户表新增 role 角色列

变更表/字段：user_information 增加 role VARCHAR(16) NOT NULL
DEFAULT 'user'（角色：user/admin，教师角色 teacher 在 ORM 注释中预留）。
业务用途：
- 登录签发的 JWT 携带 role，前端侧边栏按角色过滤导航项；
- /admin/* 管理端点以 require_admin 依赖校验，非 admin 返回 403。

版本链：revision=0002_user_role，down_revision=0001_baseline
（基线之后的第一个增量版本），后续 0003_user_profile 接在本版本之后。
执行方：`alembic upgrade head` 在线执行 upgrade() 的 ALTER TABLE
ADD COLUMN；回滚执行 `alembic downgrade -1` 调 downgrade() 删除该列。

Revision ID: 0002_user_role
Revises: 0001_baseline
Create Date: 2026-09-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# 本版本 ID
revision: str = "0002_user_role"
# 上游版本：接在 0001_baseline 之后（升级时先确保基线已 stamp/执行）
down_revision: Union[str, None] = "0001_baseline"
# 单链迁移，无分支标签与额外依赖
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：ALTER TABLE user_information ADD COLUMN role。

    被谁调用：`alembic upgrade head` 经 migrations/env.py 在线执行；
    新列非空且带 server_default='user'，存量行自动回填为普通用户，
    无需手工补数据。
    """
    # DDL：新增角色列（VARCHAR(16)，非空，默认 user，列注释同步写入 MySQL）
    op.add_column(
        "user_information",
        sa.Column("role", sa.String(16), nullable=False, server_default="user", comment="角色: user/admin"),
    )


def downgrade() -> None:
    """回滚：ALTER TABLE user_information DROP COLUMN role。

    被谁调用：`alembic downgrade -1`（或回退到 0001_baseline）时执行；
    删列会同时丢弃该列全部数据，回滚前需自行评估。
    """
    # 逆向 DDL：删除 role 列，表结构恢复到 0001_baseline 状态
    op.drop_column("user_information", "role")
