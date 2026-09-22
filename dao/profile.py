"""
模块名：dao.profile

作用：
    用户画像（长期记忆的持久化基线）的读取与写入，操作 MySQL 表 user_profile
    （字段：user_id 主键 / profile_text / interests / topics /
    create_time / update_time / is_deleted）。

主要成员：
    - ProfileDAO：画像查询 get() 与幂等写入 upsert()。

被谁使用：
    - memory/profile_service.py 的 ProfileService.__init__ 中实例化
      （self._dao = profile_dao or ProfileDAO()）：
      get() 由画像加载逻辑调用（profile_service 内基线读取），
      upsert() 由 Redis 画像 flush 落库逻辑调用。

画像读取/暂存策略由 memory.profile_service 负责：
- 日常变更先写 Redis（7 天无更新才 flush）；
- 本 DAO 只负责 MySQL 的读取与 upsert。
"""
import logging
from typing import Any, Dict, Optional

from sqlalchemy import text

from core.sql_guard import safe_execute
from db.session import session_scope

logger = logging.getLogger(__name__)


class ProfileDAO:
    """用户画像数据访问层，对应 MySQL 表 user_profile。

    承担画像的单行查询（SELECT）与按主键幂等写入
    （INSERT ... ON DUPLICATE KEY UPDATE）；软删除/恢复由
    dao/soft_delete.py 统一处理。
    实例化位置：memory/profile_service.py 的 ProfileService.__init__
    （self._dao）。无 __init__ 形参、不持有连接；会话在各方法内通过
    session_scope() 获取并自动提交/回滚。
    """

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        """读取用户画像（SELECT user_profile，带 is_deleted=0 软删除过滤）。

        功能：按主键 user_id 查询未删除的画像行。
        SQL 安全：SQL 经 text() 构造，user_id 使用命名绑定参数并经
        core.sql_guard.safe_execute 执行（SELECT 同时受 SQL_MAX_ROWS 上限保护），
        杜绝 SQL 注入；软删除过滤保证已注销用户画像不被读出。

        被谁调用：memory/profile_service.py 的画像加载逻辑
        （文件.函数：profile_service.ProfileService 内 self._dao.get(...)）。
        参数：
            user_id: 用户 ID，来源登录态/画像服务缓存键。
        返回：Optional[Dict[str, Any]]。命中返回
              {user_id, profile_text, interests, topics, create_time, update_time}
              （文本字段 NULL 兜底为空串），作为画像合并的持久化基线交给
              ProfileService；无记录返回 None；查询异常记录日志后也返回 None
              （调用方按“无基线画像”处理）。
        异常：捕获全部 Exception，记日志后返回 None，不向上抛出。
        """
        try:
            with session_scope() as session:
                row = safe_execute(session,
                    text(
                        """
                        SELECT user_id, profile_text, interests, topics, create_time, update_time
                        FROM user_profile
                        WHERE user_id = :user_id AND is_deleted = 0
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().first()
            if not row:
                return None
            return {
                "user_id": int(row["user_id"]),
                "profile_text": row["profile_text"] or "",
                "interests": row["interests"] or "",
                "topics": row["topics"] or "",
                "create_time": row["create_time"],
                "update_time": row["update_time"],
            }
        except Exception as e:
            logger.error("Error getting user profile: %s", e)
            return None

    def upsert(
        self,
        user_id: int,
        profile_text: str,
        interests: str = "",
        topics: str = "",
    ) -> bool:
        """插入或更新用户画像（upsert：INSERT ... ON DUPLICATE KEY UPDATE，按主键幂等）。

        功能：user_id 已存在则整行覆盖 profile_text/interests/topics 并刷新
        update_time，不存在则插入新画像行；单事务自动提交。
        SQL 安全：SQL 经 text() 构造，user_id/profile_text/interests/topics
        全部使用命名绑定参数并经 core.sql_guard.safe_execute 执行，杜绝 SQL 注入。

        被谁调用：memory/profile_service.py 的 Redis 画像 flush 落库逻辑
        （文件.函数：profile_service.ProfileService 内 self._dao.upsert(...)，
        7 天无更新或服务关停等场景触发）。
        参数：
            user_id: 用户 ID，来源画像服务的用户维度缓存键。
            profile_text: 画像正文（LLM 归纳的用户特征描述），由 ProfileService
                          合并生成；None/空串落库为空串。
            interests: 兴趣点文本（服务层拼接后的字符串），缺省空串。
            topics: 关注话题文本（服务层拼接后的字符串），缺省空串。
        返回：bool。True 表示 upsert 成功并提交；False 表示发生异常
              （已记录 error 日志，不向上抛出，画像服务按落库失败保留 Redis 重试）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text(
                        """
                        INSERT INTO user_profile (user_id, profile_text, interests, topics)
                        VALUES (:user_id, :profile_text, :interests, :topics)
                        ON DUPLICATE KEY UPDATE
                            profile_text = VALUES(profile_text),
                            interests = VALUES(interests),
                            topics = VALUES(topics),
                            update_time = NOW()
                        """
                    ),
                    {
                        "user_id": user_id,
                        "profile_text": profile_text or "",
                        "interests": interests or "",
                        "topics": topics or "",
                    },
                )
            logger.info("User profile upserted: user_id=%s", user_id)
            return True
        except Exception as e:
            logger.error("Error upserting user profile: %s", e)
            return False
