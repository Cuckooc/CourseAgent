"""
迁移版本：0003_user_profile —— 新建用户画像表

变更表/字段：CREATE TABLE user_profile（长期记忆中的"用户习惯/画像"）：
- user_id 整数主键且为外键 → user_information.id（一个用户一条画像记录）；
- profile_text TEXT 存 LLM 提取 + 人工编辑合并后的画像要点（MySQL TEXT
  不允许默认值，故 nullable 由 DAO 写入时保证非空）；
- interests/topics VARCHAR(500) NOT NULL DEFAULT ''（兴趣爱好 / 常问主题，
  顿号分隔），供个人信息页展示与编辑；
- create_time / update_time 由 MySQL CURRENT_TIMESTAMP 维护；
- 主键约束 pk_user_profile、外键约束 fk_profile_user，表字符集 utf8mb4。
画像变更先暂存 Redis（7 天无更新才 flush），本表为最终持久化基线；
对应 ORM 模型 db/models.py: UserProfile，DAO 为 dao/profile.py。

版本链：revision=0003_user_profile，down_revision=0002_user_role
（当前迁移链 head）。执行方：`alembic upgrade head` 在线执行
upgrade() 建表；`alembic downgrade -1` 执行 downgrade() 删表。

Revision ID: 0003_user_profile
Revises: 0002_user_role
Create Date: 2026-09-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# 本版本 ID（当前迁移链最新版本/head）
revision: str = "0003_user_profile"
# 上游版本：接在 0002_user_role 之后（升级时 role 列必须已存在）
down_revision: Union[str, None] = "0002_user_role"
# 单链迁移，无分支标签与额外依赖
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：CREATE TABLE user_profile（含主键、外键与表/列注释）。

    被谁调用：`alembic upgrade head` 经 migrations/env.py 在线执行；
    外键 fk_profile_user 引用 user_information.id，故要求库已处于
    0002_user_role（含基线三表）版本。建表后 dao/profile.py 与
    app/domain/memory/profile_service.py 方可写入画像数据。
    """
    # DDL：创建用户画像表（字段含义见各 comment；时间戳交由 MySQL 维护）
    op.create_table(
        "user_profile",
        # 用户 ID：同时充当主键与外键（一对一挂在 user_information 上）
        sa.Column("user_id", sa.Integer(), nullable=False, comment="用户ID"),
        # MySQL TEXT 列不允许默认值：由 DAO 在写入时保证非空
        sa.Column("profile_text", sa.Text(), nullable=True, comment="用户画像要点（完整文本）"),
        # 结构化画像字段：顿号分隔，默认空串便于前端直接展示/编辑
        sa.Column("interests", sa.String(500), nullable=False, server_default="", comment="兴趣爱好（顿号分隔）"),
        sa.Column("topics", sa.String(500), nullable=False, server_default="", comment="常问主题/关注方向（顿号分隔）"),
        # 创建时间：插入时取 CURRENT_TIMESTAMP
        sa.Column("create_time", sa.DateTime(),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        # 更新时间：行更新时由 MySQL 自动刷新为当前时间
        sa.Column("update_time", sa.DateTime(),
                  server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), nullable=False),
        # 主键即用户 ID（一对一）
        sa.PrimaryKeyConstraint("user_id", name="pk_user_profile"),
        # 外键：画像从属用户，用户删除时画像行的处置由应用层/硬删任务负责
        sa.ForeignKeyConstraint(["user_id"], ["user_information.id"], name="fk_profile_user"),
        mysql_charset="utf8mb4",
        comment="用户画像表（长期记忆）",
    )


def downgrade() -> None:
    """回滚：DROP TABLE user_profile。

    被谁调用：`alembic downgrade -1`（回退到 0002_user_role）时执行；
    删表会永久丢弃全部用户画像数据，回滚前需自行备份/评估。
    """
    # 逆向 DDL：删除画像表及其主键/外键约束，库结构恢复到 0002 版本
    op.drop_table("user_profile")
