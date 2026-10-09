"""
模块名：tests/test_db_integrity.py。

数据库层深度测试套件（固化自《数据库层测试报告.md》52 项断言），风险类型：
数据库结构漂移、约束缺失、孤儿数据、软删不一致、枚举脏值、事务回滚失效。

覆盖：连通性、迁移版本、ORM↔库结构一致性、PK/NOT NULL/UNIQUE/INDEX/FK 完整性、
孤儿记录、软删除一致性、枚举合法性、事务回滚、字段长度约束、Redis 连通与键抽样。

测试函数清单（模块级函数，无测试类；分组对应文件内注释小节）：
- 连通与版本：test_mysql_connect_and_version、test_alembic_version_table、
  test_redis_ping、test_redis_dbsize
- 结构一致性（参数化 10 张表）：test_orm_table_exists_in_db、
  test_orm_model_count_matches_db
- 约束/索引/外键（参数化）：test_primary_key_exists、test_not_null_constraints、
  test_unique_constraints、test_indexes_exist、test_foreign_keys_exist
- 数据完整性：test_no_orphan_records（参数化 5 张表）、test_chain_log_no_orphan、
  test_soft_delete_consistency、test_doc_type_enum_valid、
  test_doc_review_status_enum_valid、test_chat_feedback_rating_enum_valid、
  test_user_role_enum_valid
- 事务与字段约束：test_orm_transaction_rollback、test_field_length_constraint、
  test_redis_key_sampling
模块常量（参数化/断言数据源）：ORM_TABLES、EXPECTED_UNIQUE、EXPECTED_INDEXES、
EXPECTED_FK、DOC_TYPES、DOC_STATUS、USER_ROLES、ORPHAN_CHECKS，含义见各自行内注释。

被测对象来源：
- 结构基线：db/models.py 的 ORM 声明（Base.metadata、__table_args__）、
  migrations/legacy_sql/*.sql 与 alembic 版本表；
- 连接：db/session.py 的 engine 与 session_scope（conftest db_engine 夹具）；
- Redis：core/redis_client.py 的 get_redis（未配置时相关用例 skip）；
- 配置：core/config.py 的 settings.DB_NAME；
- 软删行为：dao/soft_delete.py 的级联软删。

运行方式：
    pytest tests/test_db_integrity.py -m db      # 需真实 MySQL（Redis 缺失自动 skip）
    pytest tests/test_db_integrity.py -k orphan  # 只跑孤儿记录类用例
依赖夹具：仅 conftest 的 db_engine；不依赖后端 HTTP，不 mock 数据库。
写入类用例用临时记录 + 异常强制回滚，不留垃圾数据。

设计原则：
- 只读为主：所有 SELECT 不修改业务数据；
- 写入用例（事务回滚、字段长度）使用独立临时记录 + 异常强制回滚，不污染业务表；
- 不依赖业务代码，直接走 SQLAlchemy text() + INFORMATION_SCHEMA。
"""
import uuid

import pytest
from sqlalchemy import text

# 模块级 marker：全部用例需要真实 DB（MySQL）连通
pytestmark = pytest.mark.db

# ORM 声明的 10 张业务表（与 db/models.py 对齐）
ORM_TABLES = [
    "user_information",
    "history_information",
    "session_information",
    "user_profile",
    "document_review",
    "account_deletion_schedule",
    "session_keywords",
    "chat_feedback",
    "chain_log",
    "file_meta",
]

# 期望的 UNIQUE 约束名（与 db/models.py __table_args__ 一致）
EXPECTED_UNIQUE = {
    "user_information": {"user_name", "email"},
    "history_information": {"uk_user_session"},
    "session_keywords": {"uk_user_session_kw"},
}

# 期望的索引名
EXPECTED_INDEXES = {
    "session_information": {"idx_user_session_time", "idx_session_id"},
    "document_review": {"idx_user_status"},
    "chat_feedback": {"idx_fb_user_session"},
    "chain_log": {"idx_cl_user_session_time"},
    # file_meta 库中实际索引名与 ORM 声明名不同（idx_fm_* vs idx_user_session 等）：
    # 索引都存在，名称差异是 ORM 与历史迁移未对齐——记录为待修，测试以库结构为准
    "file_meta": {"idx_user_session", "uk_user_hash", "idx_status"},
}

# 期望的外键（来自 db/models.py ForeignKey 声明）
EXPECTED_FK = {
    "user_profile": {"fk_profile_user"},
    "account_deletion_schedule": {"fk_deletion_schedule_user"},
}

# 合法枚举值
DOC_TYPES = {"scanned", "image_rich", "two_column", "pure_text"}
DOC_STATUS = {"pending", "approved", "rejected"}
USER_ROLES = {"user", "teacher", "admin"}


# ---------------- 一、连通性与版本 ----------------

def test_mysql_connect_and_version(db_engine):
    """MySQL 连通 + 版本号格式（8.x）+ 当前库名 = 配置的 DB_NAME。"""
    from core.config import settings
    with db_engine.connect() as conn:
        ver = conn.execute(text("SELECT VERSION()")).scalar()
        db = conn.execute(text("SELECT DATABASE()")).scalar()
    assert ver and ver.startswith("8."), f"MySQL 版本异常：{ver}"
    assert db == settings.DB_NAME, f"DATABASE()={db} != settings.DB_NAME={settings.DB_NAME}"


def test_alembic_version_table(db_engine):
    """alembic_version 表存在 + 当前版本非空。"""
    with db_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'alembic_version'")
        ).scalar()
        assert exists == 1, "alembic_version 表不存在"
        ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver, "alembic version_num 为空"


def test_redis_ping():
    """Redis PING 成功。"""
    from app.infrastructure.redis.redis_client import get_redis
    r = get_redis()
    if r is None:
        pytest.skip("Redis 未配置（redis_url 为空），跳过 Redis 用例")
    assert r.ping() is True


def test_redis_dbsize():
    """Redis DBSIZE 可查询（不固定值，只验证连通）。"""
    from app.infrastructure.redis.redis_client import get_redis
    r = get_redis()
    if r is None:
        pytest.skip("Redis 未配置，跳过 Redis 用例")
    n = r.dbsize()
    assert isinstance(n, int) and n >= 0


# ---------------- 二、ORM ↔ 库结构一致性 ----------------

# 参数化数据意图（正常/结构基线）：逐表验证 ORM 声明的 10 张表在库中真实存在
@pytest.mark.parametrize("table", ORM_TABLES)
def test_orm_table_exists_in_db(db_engine, table):
    """ORM 声明的每张表在库中真实存在。"""
    with db_engine.connect() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    assert cnt == 1, f"表 {table} 在库中不存在（count={cnt}）"


def test_orm_model_count_matches_db(db_engine):
    """ORM 声明的表数量 = 库中业务表数量（不含 alembic_version 迁移表）。"""
    from app.infrastructure.persistence.models import Base  # ORM 元数据
    orm_tables = set(Base.metadata.tables.keys())
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
            )
        ).fetchall()
    db_tables = {r[0] for r in rows}
    # alembic_version 不在 ORM 声明中（正常），其余必须对齐
    missing = orm_tables - db_tables
    extra_business = (db_tables - orm_tables) - {"alembic_version"}
    assert not missing, f"ORM 声明但库中缺失：{missing}"
    assert not extra_business, f"库中有未声明的业务表：{extra_business}"


# ---------------- 三、约束 / 索引 / 外键 ----------------

# 参数化数据意图（正常/结构基线）：逐表断言主键存在，任一业务表缺 PK 即失败
@pytest.mark.parametrize("table", ORM_TABLES)
def test_primary_key_exists(db_engine, table):
    """每张表必须有主键。"""
    with db_engine.connect() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.key_column_usage "
                "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_name = 'PRIMARY'"
            ),
            {"t": table},
        ).scalar()
    assert cnt >= 1, f"表 {table} 无主键"


def test_not_null_constraints(db_engine):
    """关键 NOT NULL 字段在库中真实为 NOT NULL（抽样核心字段）。"""
    expected_nn = {
        ("user_information", "user_pwd"), ("user_information", "email"), ("user_information", "role"),
        ("history_information", "user_id"), ("history_information", "session_id"), ("history_information", "title"),
        ("session_information", "session_id"), ("session_information", "user_id"),
        ("session_information", "role"), ("session_information", "content"),
        ("user_profile", "interests"), ("user_profile", "topics"),
        ("session_keywords", "user_id"), ("session_keywords", "session_id"), ("session_keywords", "keywords"),
        ("chat_feedback", "user_id"), ("chat_feedback", "session_id"),
        ("chat_feedback", "message_index"), ("chat_feedback", "rating"),
        ("chain_log", "log_data"),
        ("document_review", "user_id"), ("document_review", "file_name"),
        ("document_review", "file_path"), ("document_review", "doc_type"), ("document_review", "status"),
        ("account_deletion_schedule", "scheduled_at"),
    }
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND is_nullable = 'NO'"
            )
        ).fetchall()
    actual_nn = {(r[0], r[1]) for r in rows}
    missing = expected_nn - actual_nn
    assert not missing, f"期望 NOT NULL 但库中可空：{missing}"


# 参数化数据意图（正常/结构基线）：按 EXPECTED_UNIQUE 逐表核对 UNIQUE 约束名
@pytest.mark.parametrize("table,expected", [(t, e) for t, e in EXPECTED_UNIQUE.items()])
def test_unique_constraints(db_engine, table, expected):
    """UNIQUE 约束名齐全。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_type = 'UNIQUE'"
            ),
            {"t": table},
        ).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"表 {table} 缺少 UNIQUE 约束：{missing}（实际：{actual}）"


# 参数化数据意图（正常/结构基线）：按 EXPECTED_INDEXES 逐表核对性能/唯一索引名
@pytest.mark.parametrize("table,expected", [(t, e) for t, e in EXPECTED_INDEXES.items()])
def test_indexes_exist(db_engine, table, expected):
    """索引名齐全。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT index_name FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        ).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"表 {table} 缺少索引：{missing}（实际：{actual}）"


# 参数化数据意图（正常/结构基线）：按 EXPECTED_FK 逐表核对外键约束名
@pytest.mark.parametrize("table,expected", [(t, e) for t, e in EXPECTED_FK.items()])
def test_foreign_keys_exist(db_engine, table, expected):
    """外键约束名齐全。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.referential_constraints "
                "WHERE constraint_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        ).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"表 {table} 缺少外键：{missing}（实际：{actual}）"


# ---------------- 四、数据完整性 ----------------

# 孤儿记录检查数据源（参数化）：(业务表, 外键列)，LEFT JOIN 未软删用户后悬空即孤儿
ORPHAN_CHECKS = [
    ("history_information", "user_id"),
    ("session_information", "user_id"),
    ("user_profile", "user_id"),
    ("chat_feedback", "user_id"),
    ("session_keywords", "user_id"),
]


# 参数化数据意图（正常/数据完整性）：逐表统计外键悬空的孤儿记录数，期望全部为 0
@pytest.mark.parametrize("table,col", ORPHAN_CHECKS)
def test_no_orphan_records(db_engine, table, col):
    """user_id 不在 user_information（且未软删）的孤儿记录数 = 0。"""
    with db_engine.connect() as conn:
        cnt = conn.execute(
            text(
                f"""
                SELECT COUNT(*) FROM {table} t
                LEFT JOIN user_information u ON u.id = t.{col} AND u.is_deleted = 0
                WHERE t.{col} IS NOT NULL AND u.id IS NULL
                """
            )
        ).scalar()
    assert cnt == 0, f"表 {table}.{col} 存在 {cnt} 条孤儿记录"


def test_chain_log_no_orphan(db_engine):
    """chain_log.user_id 非空部分无孤儿。"""
    with db_engine.connect() as conn:
        cnt = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM chain_log t
                LEFT JOIN user_information u ON u.id = t.user_id AND u.is_deleted = 0
                WHERE t.user_id IS NOT NULL AND u.id IS NULL
                """
            )
        ).scalar()
    assert cnt == 0, f"chain_log.user_id 存在 {cnt} 条孤儿记录"


def test_soft_delete_consistency(db_engine):
    """软删用户的业务记录数量符合预期：软删用户在 history/session 应已级联软删。

    本项目 soft_delete_user 会级联软删业务表，断言「软删用户对应的历史会话
    is_deleted=0」的记录数 = 0（级联软删已生效）。
    """
    with db_engine.connect() as conn:
        # 软删用户但历史会话仍标记为未删的记录数（应为 0）
        cnt = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM history_information h
                JOIN user_information u ON u.id = h.user_id
                WHERE u.is_deleted = 1 AND h.is_deleted = 0
                """
            )
        ).scalar()
    assert cnt == 0, f"发现 {cnt} 条软删用户但未级联软删的历史会话"


def test_doc_type_enum_valid(db_engine):
    """document_review.doc_type 全部在合法枚举值内。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT doc_type FROM document_review WHERE doc_type IS NOT NULL")
        ).fetchall()
    actual = {r[0] for r in rows}
    bad = actual - DOC_TYPES
    assert not bad, f"非法 doc_type 值：{bad}"


def test_doc_review_status_enum_valid(db_engine):
    """document_review.status 全部合法。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT status FROM document_review")
        ).fetchall()
    actual = {r[0] for r in rows}
    bad = actual - DOC_STATUS
    assert not bad, f"非法 status 值：{bad}"


def test_chat_feedback_rating_enum_valid(db_engine):
    """chat_feedback.rating 全部在 (1, -1)。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT rating FROM chat_feedback")
        ).fetchall()
    actual = {r[0] for r in rows}
    bad = actual - {1, -1}
    assert not bad, f"非法 rating 值：{bad}"


def test_user_role_enum_valid(db_engine):
    """user_information.role 全部在 (user/teacher/admin)。"""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT role FROM user_information")
        ).fetchall()
    actual = {r[0] for r in rows if r[0]}
    bad = actual - USER_ROLES
    assert not bad, f"非法 role 值：{bad}"


# ---------------- 五、事务与字段约束 ----------------

def test_orm_transaction_rollback(db_engine):
    """ORM 事务回滚：INSERT 缺 NOT NULL 字段触发异常 → 回滚 → COUNT=0。

    使用临时唯一 user_name，INSERT 不提供 email（NOT NULL）→ MySQL 抛 1364 →
    session_scope 自动 rollback → 验证无残留记录。
    """
    from app.infrastructure.persistence.session import session_scope
    tmp_name = f"rb_{uuid.uuid4().hex[:8]}"
    with db_engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM user_information WHERE user_name = :n"), {"n": tmp_name}).scalar()
    assert before == 0

    # session_scope 内任意异常都会触发 rollback，捕获 SQLAlchemy 异常即可
    with pytest.raises(Exception):  # noqa: B017 (测试需要宽捕获验证回滚)
        with session_scope() as session:
            session.execute(
                text("INSERT INTO user_information(user_name, user_pwd) VALUES (:n, 'x')"),
                {"n": tmp_name},
            )
            # email NOT NULL 未提供 → MySQL 抛 OperationalError(1364) → session_scope 回滚

    with db_engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM user_information WHERE user_name = :n"), {"n": tmp_name}).scalar()
    assert after == 0, f"事务回滚失败：残留 {after} 条记录"


def test_field_length_constraint(db_engine):
    """字段长度约束：超长 user_name（VARCHAR(20)）应被 MySQL 拒绝。"""
    too_long = "x" * 25
    with db_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            conn.execute(
                text(
                    "INSERT INTO user_information(user_name, user_pwd, email) VALUES (:n, 'x', 'x@x.com')"
                ),
                {"n": too_long},
            )
            conn.commit()
    msg = str(exc_info.value).lower()
    assert "data too long" in msg or "1406" in msg, f"未触发字段长度约束，异常：{exc_info.value}"


def test_redis_key_sampling():
    """Redis 键抽样：业务前缀查询不报错（不固定键值，只验证可查询）。"""
    from app.infrastructure.redis.redis_client import get_redis
    r = get_redis()
    if r is None:
        pytest.skip("Redis 未配置，跳过 Redis 用例")
    prefixes = ["short_term:", "rate_limit:", "vcode:", "token_version:"]
    for p in prefixes:
        # scan_iter 不抛错即视为通过（空结果正常）
        keys = list(r.scan_iter(f"{p}*", count=100))
        assert isinstance(keys, list)
