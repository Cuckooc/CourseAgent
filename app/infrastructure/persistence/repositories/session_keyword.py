"""
模块名：app.infrastructure.persistence.repositories.session_keyword

作用：
    会话级关键词的 MySQL 持久化，操作表 session_keywords
    （one row per user+session；字段：user_id / session_id / keywords /
    is_deleted，(user_id, session_id) 上有唯一键支撑 upsert）。
    关键词严格按 (user_id, session_id) 二元组隔离，所有 SQL 同时带两个条件，
    防止会话号为 per-user 序列时发生跨用户串读。

主要成员：
    - SessionKeywordDAO：get() 读取、upsert() 幂等写入、delete() 软删除。

被谁使用：
    - memory/session_keyword_service.py 的 SessionKeywordService.__init__
      实例化（self._dao = dao or SessionKeywordDAO()）：get() 用于关键词缓存
      未命中时回库，upsert() 用于关键词积累后落库，delete() 用于会话删除/
      关键词清空场景（clear() 内调用）。
"""
import logging
from typing import Optional

from sqlalchemy import text

from core.sql_guard import safe_execute
from app.infrastructure.persistence.session import session_scope

logger = logging.getLogger(__name__)


class SessionKeywordDAO:
    """会话关键词数据访问层，对应 MySQL 表 session_keywords。

    承担单行关键词的查（get）、幂等写（upsert）、软删（delete）；
    不做物理删除与跨会话查询。
    实例化位置：memory/session_keyword_service.py 的
    SessionKeywordService.__init__（self._dao）。无 __init__ 形参、
    不持有连接；会话在各方法内通过 session_scope() 获取并自动提交/回滚。
    """

    def get(self, user_id: int, session_id: int) -> Optional[str]:
        """读取指定会话的关键词串（SELECT keywords，双键隔离 + 软删除过滤）。

        SQL 安全：user_id/session_id 以绑定参数 :uid/:sid 传入；
        is_deleted = 0 排除已删行；LIMIT 1；经 safe_execute 执行。
        被谁调用：memory/session_keyword_service.py 的 SessionKeywordService
        缓存未命中回库逻辑（文件.函数：
        session_keyword_service.SessionKeywordService 内 self._dao.get(...)）。
        参数：
            user_id: 登录态用户 ID（缓存键解析得到）。
            session_id: 会话 ID（per-user 序号）。
        返回：Optional[str]。命中返回 keywords 文本（NULL 兜底空串）；
              无记录返回 None（区分“空串关键词”与“无行”）；异常记日志返回 None。
        异常：捕获全部 Exception，记日志后返回 None。
        """
        try:
            with session_scope() as session:
                row = safe_execute(session,
                    text(
                        """
                        SELECT keywords FROM session_keywords
                        WHERE user_id = :uid AND session_id = :sid AND is_deleted = 0
                        LIMIT 1
                        """
                    ),
                    {"uid": user_id, "sid": session_id},
                ).mappings().first()
            if row is None:
                return None
            return row["keywords"] or ""
        except Exception as e:
            logger.error("SessionKeywordDAO.get failed: %s", e)
            return None

    def upsert(self, user_id: int, session_id: int, keywords: str) -> bool:
        """插入或整串覆盖会话关键词（INSERT ... ON DUPLICATE KEY UPDATE，按唯一键幂等）。

        功能：(user_id, session_id) 无有效行则插入；命中唯一键则用 VALUES(keywords)
        整串覆盖旧关键词（不是追加，追加合并在 service 层完成）。
        SQL 安全：uid/sid/kw 全部命名绑定参数，经 safe_execute 执行，杜绝注入。
        现状语义：ON DUPLICATE 子句不重置 is_deleted，已软删行即使冲突更新也
        仍保持 is_deleted=1（不会因重新落库而“复活”）。
        被谁调用：memory/session_keyword_service.py 的关键词落库逻辑
        （文件.函数：session_keyword_service.SessionKeywordService 内
        self._dao.upsert(...)，关键词积累节流后触发）。
        参数：
            user_id: 用户 ID，来源关键词服务缓存键。
            session_id: 会话 ID（per-user 序号）。
            keywords: 已在 service 层去重/截断/拼接好的关键词字符串。
        返回：bool。True 已提交；False 发生异常（记日志，由服务层保留缓存待重试）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text(
                        """
                        INSERT INTO session_keywords (user_id, session_id, keywords)
                        VALUES (:uid, :sid, :kw)
                        ON DUPLICATE KEY UPDATE keywords = VALUES(keywords)
                        """
                    ),
                    {"uid": user_id, "sid": session_id, "kw": keywords},
                )
            return True
        except Exception as e:
            logger.error("SessionKeywordDAO.upsert failed: %s", e)
            return False

    def delete(self, user_id: int, session_id: int) -> bool:
        """软删除指定会话的关键词（UPDATE is_deleted=1，不物理删行）。

        功能：按双键把关键词行标记删除（本语句未追加 is_deleted=0 条件，
        重复删除为幂等无副作用更新）；之后 get() 因 is_deleted=0 过滤读不到。
        SQL 安全：uid/sid 命名绑定参数，经 safe_execute 执行。
        被谁调用：memory/session_keyword_service.py 的 SessionKeywordService.clear()
        （文件.函数：session_keyword_service.SessionKeywordService.clear），
        而 clear() 在会话被软删除时由 dao/soft_delete.py 联动调用。
        参数：
            user_id: 用户 ID。
            session_id: 会话 ID（per-user 序号）。
        返回：bool。True 语句执行成功并提交（不区分是否实际有行变更）；
              False 发生异常（记日志）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text(
                        """
                        UPDATE session_keywords SET is_deleted = 1
                        WHERE user_id = :uid AND session_id = :sid
                        """
                    ),
                    {"uid": user_id, "sid": session_id},
                )
            return True
        except Exception as e:
            logger.error("SessionKeywordDAO.delete failed: %s", e)
            return False
