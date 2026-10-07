"""
模块名：app.infrastructure.persistence.repositories.soft_delete

作用：
    统一处理业务表软删除、注销计划与撤销恢复，涉及 5 张 MySQL 表：
    session_information（会话消息）、history_information（会话条目）、
    user_profile（用户画像）、user_information（用户账号）、
    account_deletion_schedule（账号注销计划，宽限期到期后由
    core/purge_scheduler.py 物理删除）。

主要成员（全部为模块级函数，无类）：
    - soft_delete_session()：软删单个会话（消息 + 会话条目同事务）；
    - soft_delete_user()：软删用户并级联软删其会话/消息/画像；
    - schedule_account_deletion()：写入/刷新注销计划（宽限期）；
    - recover_last_deleted()：撤销恢复最近删除的会话或画像。

被谁使用：
    - dao/session.py 的 SessionDAO.delete_session() 转发 soft_delete_session()；
    - dao/user.py 的 Information.deactivate_user() 调用 soft_delete_user()
      与 schedule_account_deletion()；
    - control/history_control.py 的撤销端点调用 recover_last_deleted()；
    - tests/conftest.py 测试清库走 soft_delete_user()。

软删除语义：
- UPDATE is_deleted=1, deleted_at=NOW()（不物理删除行）；
- 所有 SELECT 查询均过滤 is_deleted=0（在各 DAO 中实现）；
- 恢复：将 is_deleted 置回 0，deleted_at 置 NULL。
所有 SQL 均 text() + 命名绑定参数并经 core.sql_guard.safe_execute 执行。
"""
import logging
import time
from typing import Optional

from sqlalchemy import text

from core.sql_guard import safe_execute
from app.infrastructure.persistence.session import session_scope

logger = logging.getLogger(__name__)


def soft_delete_session(user_id: int, session_id: int) -> bool:
    """软删除会话：history_information + session_information 同事务标记。

    功能：在一个 session_scope 事务内先后 UPDATE 两张表，把指定
    (user_id, session_id) 且未删除的行置 is_deleted=1、deleted_at=NOW()；
    两条语句同生共死，任一异常整体回滚。以会话主表（history）影响行数
    判断是否成功。成功后额外：写 UndoStore 撤销快照（供撤销端点找回）、
    经 SessionKeywordService.clear() 联动软删会话关键词（失败仅告警）。
    SQL 安全：uid/sid 命名绑定参数；WHERE 带 user_id 做归属隔离，
    is_deleted=0 避免重复更新；经 safe_execute 执行。
    被谁调用：dao/session.py 的 SessionDAO.delete_session()
    （文件.函数：session.SessionDAO.delete_session）。
    参数：
        user_id: 登录态用户 ID（归属隔离）。
        session_id: 待删会话 ID（per-user 序号）。
    返回：bool。True 会话主表确有行被标记；无匹配或异常返回 False
          （异常记日志，不向上抛出）。
    异常：捕获全部 Exception，记日志后返回 False。
    """
    try:
        with session_scope() as session:
            # 先标记消息行：仅该用户该会话且当前未删除的消息
            safe_execute(session,
                text(
                    """
                    UPDATE session_information
                    SET is_deleted = 1, deleted_at = NOW()
                    WHERE user_id = :uid AND session_id = :sid AND is_deleted = 0
                    """
                ),
                {"uid": user_id, "sid": session_id},
            )
            # 再标记会话主表行（同一事务）；以其 rowcount 判定会话是否真实存在
            r = safe_execute(session,
                text(
                    """
                    UPDATE history_information
                    SET is_deleted = 1, deleted_at = NOW()
                    WHERE user_id = :uid AND session_id = :sid AND is_deleted = 0
                    """
                ),
                {"uid": user_id, "sid": session_id},
            )
        ok = r.rowcount > 0
        if ok:
            logger.info("Session soft-deleted: user_id=%s, session_id=%s", user_id, session_id)
            from core.undo_store import UndoStore
            UndoStore.save(
                user_id=user_id,
                table="session",
                pk={"session_id": session_id},
                deleted_at=str(time.time()),
            )
            try:
                from app.domain.memory.session_keyword_service import get_session_keyword_service
                get_session_keyword_service().clear(user_id, session_id)
            except Exception as e:
                logger.warning("keyword clear on session delete failed: %s", e)
        return ok
    except Exception as e:
        logger.error("Error soft-deleting session: %s", e)
        return False


def soft_delete_user(user_id: int) -> bool:
    """软删除用户：标记 user_information + user_profile，并级联软删其全部会话与消息。

    功能：一个 session_scope 事务内按“子数据 → 主账号”顺序执行 4 条 UPDATE：
    session_information → history_information → user_profile → user_information，
    全部置 is_deleted=1、deleted_at=NOW()；任一失败整体回滚，保证级联一致性。
    以账号主表 user_information 的 rowcount 判定是否真的注销到用户，
    成功后写 UndoStore 撤销快照（table="user"）。
    SQL 安全：uid 命名绑定参数；各 UPDATE 带 is_deleted=0 幂等条件；
    经 safe_execute 执行。
    被谁调用：dao/user.py 的 Information.deactivate_user()
    （文件.函数：user.Information.deactivate_user，管理端注销端点链路）；
    tests/conftest.py 测试夹具清库亦调用。
    参数：
        user_id: 待注销用户 ID（管理员操作经二次确认令牌后传入）。
    返回：bool。True 账号主表确有行被标记；用户不存在或异常返回 False
          （异常记日志，不向上抛出）。
    异常：捕获全部 Exception，记日志后返回 False。
    """
    try:
        with session_scope() as session:
            # 级联第 1 步：软删该用户全部会话消息
            safe_execute(session,
                text(
                    """
                    UPDATE session_information
                    SET is_deleted = 1, deleted_at = NOW()
                    WHERE user_id = :uid AND is_deleted = 0
                    """
                ),
                {"uid": user_id},
            )
            # 级联第 2 步：软删该用户全部会话条目
            safe_execute(session,
                text(
                    """
                    UPDATE history_information
                    SET is_deleted = 1, deleted_at = NOW()
                    WHERE user_id = :uid AND is_deleted = 0
                    """
                ),
                {"uid": user_id},
            )
            # 级联第 3 步：软删用户画像（长期记忆基线）
            safe_execute(session,
                text(
                    """
                    UPDATE user_profile
                    SET is_deleted = 1, deleted_at = NOW()
                    WHERE user_id = :uid AND is_deleted = 0
                    """
                ),
                {"uid": user_id},
            )
            # 级联第 4 步（主表）：最后软删账号本身；以其 rowcount 判定注销成败
            r = safe_execute(session,
                text(
                    """
                    UPDATE user_information
                    SET is_deleted = 1, deleted_at = NOW()
                    WHERE id = :uid AND is_deleted = 0
                    """
                ),
                {"uid": user_id},
            )
        ok = r.rowcount > 0
        if ok:
            logger.info("User soft-deleted: user_id=%s", user_id)
            from core.undo_store import UndoStore
            UndoStore.save(
                user_id=user_id,
                table="user",
                pk={"user_id": user_id},
                deleted_at=str(time.time()),
            )
        return ok
    except Exception as e:
        logger.error("Error soft-deleting user: %s", e)
        return False


def schedule_account_deletion(user_id: int, grace_days: int = 7) -> bool:
    """写入/刷新账号注销计划（upsert account_deletion_schedule）。

    功能：登记计划硬删除时间 scheduled_at = NOW() + grace_days 天；
    INSERT ... ON DUPLICATE KEY UPDATE 保证同一用户重复登记时只刷新时间，
    不产生重复行。到期后由 core/purge_scheduler.py 扫描并物理删除账号数据。
    SQL 安全：uid 命名绑定；grace_days 以绑定参数 :days 传入 INTERVAL 运算
    （天数不拼进 SQL 文本），经 safe_execute 执行。
    被谁调用：dao/user.py 的 Information.deactivate_user()
    （文件.函数：user.Information.deactivate_user，grace_days 取自
    settings.ACCOUNT_DELETION_GRACE_DAYS，默认 7）。
    参数：
        user_id: 已软注销用户 ID。
        grace_days: 宽限天数（int），来源 core.config 配置；宽限期内可恢复。
    返回：bool。True 计划已写入/刷新并提交；异常返回 False（记日志）。
    异常：捕获全部 Exception，记日志后返回 False。
    """
    try:
        with session_scope() as session:
            safe_execute(session,
                text(
                    """
                    INSERT INTO account_deletion_schedule (user_id, scheduled_at)
                    VALUES (:uid, DATE_ADD(NOW(), INTERVAL :days DAY))
                    ON DUPLICATE KEY UPDATE scheduled_at = VALUES(scheduled_at)
                    """
                ),
                {"uid": user_id, "days": grace_days},
            )
        logger.info("Account deletion scheduled: user_id=%s, grace_days=%s", user_id, grace_days)
        return True
    except Exception as e:
        logger.error("Error scheduling account deletion: %s", e)
        return False


def recover_last_deleted(user_id: int) -> Optional[str]:
    """恢复用户最近一条软删除记录（会话级优先），返回恢复的类型名或 None。

    优先恢复最近 deleted_at 的会话（history_information 连同其消息一起恢复），
    其次恢复用户画像（user_profile）；账号本身的恢复不经此路径
    （账号级恢复由注销宽限期/管理员流程处理）。
    功能：单个 session_scope 事务内先查后改——查到最近被删会话则把
    session_information + history_information 对应行 is_deleted 置 0、
    deleted_at 置 NULL 并返回 "session"；否则查被删画像行并恢复，返回 "profile"；
    都没有则返回 None。
    SQL 安全：uid/sid 命名绑定参数；查询按 is_deleted=1 精确定位待恢复行，
    更新只作用于 is_deleted=1 的行，避免误复活从未删除的数据；
    ORDER BY deleted_at DESC LIMIT 1 保证“最近一条”且行数有界。
    被谁调用：control/history_control.py 的撤销删除端点
    （文件.函数：history_control 撤销处理函数，recover_last_deleted(user_id)）。
    参数：
        user_id: 登录态用户 ID（只能恢复自己名下的删除记录）。
    返回：Optional[str]。"session" 表示恢复了一个会话；"profile" 表示恢复了
          画像；None 表示近期无可恢复记录或发生异常（异常记日志，不向上抛出）。
    异常：捕获全部 Exception，记日志后返回 None。
    """
    try:
        with session_scope() as session:
            # 查找该用户最近被软删的会话（按删除时间倒序取一条）
            row = safe_execute(session,
                text(
                    """
                    SELECT session_id FROM history_information
                    WHERE user_id = :uid AND is_deleted = 1
                    ORDER BY deleted_at DESC
                    LIMIT 1
                    """
                ),
                {"uid": user_id},
            ).mappings().first()

            if row:
                sid = row["session_id"]
                # 恢复该会话消息：仅翻转当前处于软删状态的行
                safe_execute(session,
                    text(
                        """
                        UPDATE session_information
                        SET is_deleted = 0, deleted_at = NULL
                        WHERE user_id = :uid AND session_id = :sid AND is_deleted = 1
                        """
                    ),
                    {"uid": user_id, "sid": sid},
                )
                # 同事务恢复会话主表行，保证消息与列表条目一起回来
                safe_execute(session,
                    text(
                        """
                        UPDATE history_information
                        SET is_deleted = 0, deleted_at = NULL
                        WHERE user_id = :uid AND session_id = :sid AND is_deleted = 1
                        """
                    ),
                    {"uid": user_id, "sid": sid},
                )
                logger.info("Recovered soft-deleted session: user_id=%s, session_id=%s", user_id, sid)
                return "session"

            # 无已删会话时的次优恢复：查找该用户被软删的画像行
            profile_row = safe_execute(session,
                text(
                    """
                    SELECT user_id FROM user_profile
                    WHERE user_id = :uid AND is_deleted = 1
                    LIMIT 1
                    """
                ),
                {"uid": user_id},
            ).mappings().first()

            if profile_row:
                # 恢复画像：仅翻转 is_deleted=1 的行并清空删除时间
                safe_execute(session,
                    text(
                        """
                        UPDATE user_profile
                        SET is_deleted = 0, deleted_at = NULL
                        WHERE user_id = :uid AND is_deleted = 1
                        """
                    ),
                    {"uid": user_id},
                )
                logger.info("Recovered soft-deleted profile: user_id=%s", user_id)
                return "profile"

        return None
    except Exception as e:
        logger.error("Error recovering deleted record: %s", e)
        return None
