"""
模块名：core.purge_scheduler（定时清理调度器）。

作用：
    以 daemon 守护线程在后台周期性执行三类“硬删除”清理任务：
    任务 1 — 注销账户到期清理：
      account_deletion_schedule 中 scheduled_at <= NOW() 的记录，
      对每个 user_id 执行硬删除级联（会话 / 历史 / 画像 / 审核记录 /
      注销计划 / 账号本身，均为 MySQL 物理 DELETE）。
    任务 2 — 软删除记录过期清理：
      4 张业务表中 is_deleted=1 且 deleted_at <= NOW() - retention_days 的行，
      从 MySQL 彻底移除（保留期来自 settings.SOFT_DELETE_RETENTION_DAYS）。
    任务 3 — 旧版本知识库清理：
      向量库中 is_latest=false 且被替换时间超过 settings.OLD_VERSION_RETENTION_DAYS
      的向量块物理删除，并删除已无任何向量块引用的物理文件。

启动位置：
    control/app.py 的 FastAPI lifespan 启动阶段调用
    get_purge_scheduler().start_background_scheduler()（单例，幂等启动）。
扫描周期来源：
    settings.PURGE_INTERVAL_HOURS（环境变量 purge_interval_hours，默认 6 小时）；
    也可由 start_background_scheduler(interval_seconds=...) 显式覆盖。
多副本互斥：
    每轮先经 core.locks.try_acquire_cycle_lock 抢 Redis 周期锁，
    保证多副本部署下同一时刻只有一个副本执行清理（沿用 LongTermFlusher 模式）。

主要成员：
    - _hard_delete_account / _purge_expired_accounts：任务 1；
    - _purge_old_soft_deleted：任务 2；
    - _purge_expired_old_versions：任务 3；
    - PurgeScheduler：守护线程封装；get_purge_scheduler()：进程级单例工厂。

被谁使用：
    - control/app.py（lifespan 启动后台调度）；
    - tests/phase/test_dedup_version.py 直接导入 _purge_expired_old_versions 做验证。
"""
import logging
import threading
import time
from typing import List

from sqlalchemy import text

from core.config import settings
from app.infrastructure.redis.locks import try_acquire_cycle_lock
from core.sql_guard import safe_execute
from app.infrastructure.persistence.session import session_scope

logger = logging.getLogger(__name__)

# 周期锁在 Redis 中的键名：try_acquire_cycle_lock 据此实现多副本互斥
_PURGE_LOCK_KEY = "purge:scheduler"

# 需要做软删除过期清理的 4 张业务表（表名固定，故走白名单拼接而非外部输入）
_SOFT_DELETE_TABLES = (
    "user_information",
    "history_information",
    "session_information",
    "user_profile",
)


def _hard_delete_account(user_id: int) -> bool:
    """硬删除单个用户的全部数据（注销到期清理）。

    功能：在单个事务会话内按外键依赖顺序物理 DELETE 该用户的会话、历史、
    画像、文档审核记录、注销计划行，最后删除账号主表行。
    被谁调用：_purge_expired_accounts（core.purge_scheduler）逐用户调用。
    参数：
        user_id: 到期用户 id，来源为 account_deletion_schedule 表查询结果。
    返回：bool；全部删除并提交成功返回 True，任一步异常返回 False
        （异常仅记录日志，不中断后续其他用户的清理）。
    """
    try:
        with session_scope() as session:
            # 删除顺序：先子表（会话/历史/画像/审核/计划）后主表（账号）
            safe_execute(session,
                text("DELETE FROM session_information WHERE user_id = :uid"),
                {"uid": user_id},
            )
            safe_execute(session,
                text("DELETE FROM history_information WHERE user_id = :uid"),
                {"uid": user_id},
            )
            safe_execute(session,
                text("DELETE FROM user_profile WHERE user_id = :uid"),
                {"uid": user_id},
            )
            safe_execute(session,
                text("DELETE FROM document_review WHERE user_id = :uid"),
                {"uid": user_id},
            )
            safe_execute(session,
                text("DELETE FROM account_deletion_schedule WHERE user_id = :uid"),
                {"uid": user_id},
            )
            safe_execute(session,
                text("DELETE FROM user_information WHERE id = :uid"),
                {"uid": user_id},
            )
        logger.info("Hard-deleted account: user_id=%s", user_id)
        return True
    except Exception as e:
        logger.error("Hard-delete account failed: user_id=%s, error=%s", user_id, e)
        return False


def _purge_expired_accounts() -> int:
    """清理所有到期的注销账户。

    功能：先查出 scheduled_at <= NOW() 的全部待删 user_id，再逐个硬删除。
    被谁调用：PurgeScheduler._run_cycle（每轮清理的任务 1）。
    返回：int，本轮成功硬删除的账户数量；查询阶段异常返回 0。
    """
    try:
        with session_scope() as session:
            # 计划行先查出后在事务外逐个删除：单用户失败不影响其他用户
            rows = safe_execute(session,
                text(
                    """
                    SELECT user_id FROM account_deletion_schedule
                    WHERE scheduled_at <= NOW()
                    """
                ),
            ).fetchall()
        user_ids = [r[0] for r in rows]
    except Exception as e:
        logger.error("Query expired account_deletion_schedule failed: %s", e)
        return 0

    purged = 0
    for uid in user_ids:
        if _hard_delete_account(uid):
            purged += 1
    if purged:
        logger.info("Purged %d expired accounts", purged)
    return purged


def _purge_old_soft_deleted() -> int:
    """硬删除超过保留期的软删除记录。

    功能：遍历 _SOFT_DELETE_TABLES 白名单中的 4 张表，物理删除 is_deleted=1
    且 deleted_at 早于保留期（settings.SOFT_DELETE_RETENTION_DAYS）的行。
    被谁调用：PurgeScheduler._run_cycle（每轮清理的任务 2）。
    返回：int，本轮各表删除行数之和；异常时返回已累计的数量。
    """
    # 保留期由配置项 soft_delete_retention_days 控制（默认 1095 天 ≈ 3 年）
    retention = settings.SOFT_DELETE_RETENTION_DAYS
    total = 0
    try:
        with session_scope() as session:
            # 表名来自模块内白名单常量，非外部输入，可安全 format 进 SQL
            for table in _SOFT_DELETE_TABLES:
                r = safe_execute(session,
                    text(
                        """
                        DELETE FROM {t}
                        WHERE is_deleted = 1 AND deleted_at <= DATE_SUB(NOW(), INTERVAL :days DAY)
                        """.format(t=table)
                    ),
                    {"days": retention},
                )
                count = r.rowcount
                if count:
                    logger.info("Purged %d old soft-deleted rows from %s", count, table)
                    total += count
    except Exception as e:
        logger.error("Purge old soft-deleted records failed: %s", e)
    return total


def _purge_expired_old_versions() -> int:
    """清理被替换满保留期的旧版本知识库向量块及其物理文件。

    功能（任务 3）：
      1. 分批扫描向量库中 is_latest=false 的块，取 metadata 的
         superseded_at（退化为 updated_at）判断是否超过
         settings.OLD_VERSION_RETENTION_DAYS；
      2. 持持久库锁批量删除过期块并 flush 索引；
      3. 对被删块按 source 分组，若某物理文件已无任何向量块引用则删除文件。
    仅清理旧版本块；最新版本（is_latest=true）与从未被替换的文件不受影响。
    被谁调用：PurgeScheduler._run_cycle；
      tests/phase/test_dedup_version.py 也直接调用本函数做验证。
    返回：int，本轮删除的向量块数量。
    """
    import time
    from pathlib import Path

    from service.vector_store import (
        _flush_collection_index,
        get_persistent_db,
        persistent_lock,
    )

    # 保留期换算为秒并算出截止时间戳：替换时间早于 cutoff 的块可清理
    retention_seconds = settings.OLD_VERSION_RETENTION_DAYS * 24 * 3600
    cutoff = time.time() - retention_seconds
    db = get_persistent_db()
    col = db._collection

    # 分批拉取 is_latest=false 的块，检查时间是否过期
    to_delete = []  # [(chunk_id, source)]
    offset = 0
    batch = 500
    while True:
        data = col.get(
            where={"is_latest": False},
            include=["metadatas"],
            limit=batch,
            offset=offset,
        )
        ids = data.get("ids") or []
        metas = data.get("metadatas") or []
        if not ids:
            break
        for cid, meta in zip(ids, metas):
            meta = meta or {}
            # 优先取替换时间；历史数据无该字段时退化为更新时间，再缺则按 0（不删）
            superseded = meta.get("superseded_at") or meta.get("updated_at") or 0
            if superseded <= cutoff:
                to_delete.append((cid, meta.get("source")))
        # 不足一批说明已扫完
        if len(ids) < batch:
            break
        offset += batch

    if not to_delete:
        return 0

    delete_ids = [cid for cid, _ in to_delete]
    # 按 source 分组，便于后续物理文件清理
    sources = {}
    for cid, src in to_delete:
        if src:
            sources.setdefault(src, []).append(cid)

    # 加锁删除向量块 + flush，与在线写入互斥，避免索引与落盘不一致
    with persistent_lock():
        col.delete(ids=delete_ids)
        _flush_collection_index(db, deleted_ids=delete_ids)

    # 清理已无任何向量块的物理文件（最新版本仍引用的文件会保留）
    removed_files = 0
    for src in sources:
        remaining = col.get(where={"source": src}, include=[])
        if not (remaining.get("ids") or []):
            p = Path(src)
            if p.exists():
                try:
                    p.unlink()
                    removed_files += 1
                except Exception as e:
                    logger.warning("remove stale old-version file failed: %s, error=%s", src, e)

    purged = len(delete_ids)
    logger.info(
        "Purged %d expired old-version chunks (%d files removed, retention=%ddays)",
        purged, removed_files, settings.OLD_VERSION_RETENTION_DAYS,
    )
    return purged


class PurgeScheduler:
    """后台定时清理调度器（应用级单例 + daemon 线程）。

    作用：持有启动标记并创建名为 purge-scheduler 的守护线程，线程内按固定
    间隔先抢 Redis 周期锁再执行一轮三类清理。
    实例化位置：仅由 get_purge_scheduler() 工厂以双重检查锁创建全局唯一实例；
    业务侧不直接构造。control/app.py 的 lifespan 通过工厂取得单例并启动。
    """

    def __init__(self):
        # 启动幂等标记：True 后重复调用 start_background_scheduler 直接返回
        self._started = False

    def start_background_scheduler(self, interval_seconds: int = None) -> None:
        """启动后台清理守护线程（幂等，可安全重复调用）。

        被谁调用：control/app.py 的 FastAPI lifespan 启动阶段
            （get_purge_scheduler().start_background_scheduler()）。
        参数：
            interval_seconds: 扫描周期秒数；为 None 时取配置项
                settings.PURGE_INTERVAL_HOURS * 3600（purge_interval_hours）。
        返回：None。
        """
        if self._started:
            return
        self._started = True
        # 显式入参优先，否则用配置的小时间隔换算为秒
        interval = (
            interval_seconds
            if interval_seconds is not None
            else settings.PURGE_INTERVAL_HOURS * 3600
        )

        def _run():
            # 守护线程主循环：先睡一个周期再执行，避免与启动过程抢资源
            while True:
                time.sleep(interval)
                try:
                    # 抢不到周期锁说明其他副本正在清理，本轮直接跳过
                    if not try_acquire_cycle_lock(_PURGE_LOCK_KEY, interval):
                        continue
                    self._run_cycle()
                except Exception as e:
                    # 单轮异常不允许杀死守护线程，记录后进入下一周期
                    logger.error("purge scheduler cycle error: %s", e)

        t = threading.Thread(target=_run, name="purge-scheduler", daemon=True)
        t.start()
        logger.info("purge scheduler started, interval=%ss", interval)

    @staticmethod
    def _run_cycle() -> None:
        """执行一轮完整清理（任务 1/2/3 顺序执行）。

        被谁调用：start_background_scheduler 内守护线程抢到周期锁后调用。
        返回：None；仅在本轮有任意清理产出时记录汇总日志。
        """
        purged_accounts = _purge_expired_accounts()
        purged_rows = _purge_old_soft_deleted()
        purged_old_versions = _purge_expired_old_versions()
        if purged_accounts or purged_rows or purged_old_versions:
            logger.info(
                "purge cycle done: accounts=%d, soft_deleted_rows=%d, old_versions=%d",
                purged_accounts, purged_rows, purged_old_versions,
            )


# 进程级单例及其创建锁（双重检查锁定，避免并发首次调用创建多个调度器）
_purge_scheduler = None  # type: ignore
_purge_lock = threading.Lock()


def get_purge_scheduler() -> PurgeScheduler:
    """获取 PurgeScheduler 全局唯一实例（懒加载单例工厂）。

    被谁调用：control/app.py 的 lifespan 启动阶段。
    返回：PurgeScheduler 单例；首次调用时加锁创建，之后直接复用。
    """
    global _purge_scheduler
    if _purge_scheduler is None:
        with _purge_lock:
            if _purge_scheduler is None:
                _purge_scheduler = PurgeScheduler()
    return _purge_scheduler
