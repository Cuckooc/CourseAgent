"""
迁移版本：0001_baseline —— 引入 Alembic 前的现有库结构基线（中文注释）

变更内容：无 DDL（空操作）。三张表 user_information /
history_information / session_information 及其索引、唯一键
（uk_user_session、idx_user_id、idx_user_session_time、idx_session_id）
在引入迁移机制前已通过 course.sql + 升级 ALTER 就位，故基线仅占位。
新建空库时请先执行 course.sql，再 `alembic stamp head` 把库标记到本版本。

版本链：revision=0001_baseline，down_revision=None（迁移链起点）；
后续 0002_user_role 接在本版本之后。
执行方：开发者/运维执行 `alembic upgrade head`（经 migrations/env.py
在线连接 MySQL 后调用本脚本 upgrade()）；降级链回到本版本即止，不可再回滚。
后续表结构变更流程：改 db/models.py →
`alembic revision --autogenerate -m "xxx"` → 审查生成脚本 → `alembic upgrade head`。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-08
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers, used by Alembic.
# 本版本 ID：迁移链中的唯一标识，alembic_version 表记录当前库所处版本
revision: str = "0001_baseline"
# 上游版本：None 表示本版本是迁移链起点（基线）
down_revision: Union[str, None] = None
# 分支标签/额外依赖：单链迁移，均为 None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级操作：无 DDL（基线，三张表已由 course.sql 预先建好）。

    被谁调用：`alembic upgrade head` 时由 Alembic 经 migrations/env.py
    按版本链顺序调用；空库首次应先 course.sql 再 stamp head，不实际执行本函数。
    """
    pass


def downgrade() -> None:
    """回滚操作：基线不可再向下回滚（down_revision=None），保持空操作。

    被谁调用：`alembic downgrade` 回退到基线时调用；如需清空结构应手动
    处理 course.sql 建出的表，不由迁移系统删除。
    """
    pass
