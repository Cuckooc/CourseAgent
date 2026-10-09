"""
模块名：app.infrastructure.persistence.repositories.information

作用：
    会话聊天消息写入，操作 MySQL 表 session_information
    （字段：session_id / user_id / role / content / create_time，
    role 取值 user / assistant）。

主要成员：
    - Information：继承 BaseInformation，提供
      save_information()（一轮对话写 user + assistant 两条消息）与
      save_messages_batch()（短期记忆转长期记忆时批量写入）。

被谁使用：
    - app/domain/memory/long_term.py 的 LongTermMemory 构造时实例化
      （self._information_dao = information_dao or Information()），
      落库任务调用 save_messages_batch() 把 Redis 短期记忆一次性转存 MySQL。
      注：save_information() 为早期逐轮双条写入接口，当前主链路对话期间只写
      Redis、由批量落库替代，方法保留以兼容旧调用与接口契约。
"""
from .base_information import BaseInformation
from app.infrastructure.persistence.session import session_scope
from sqlalchemy import text
from core.sql_guard import safe_execute
from typing import Any, Dict, List
import logging

logger = logging.getLogger(__name__)


class Information(BaseInformation):
    """会话消息写入 DAO，对应 MySQL 表 session_information。

    承担聊天消息的新增（单轮两条 INSERT 或批量 INSERT）；
    查询见 dao/read.py 与 dao/session.py，软删除见 dao/soft_delete.py。
    实例化位置：app/domain/memory/long_term.py 的 LongTermMemory.__init__
    （self._information_dao）。__init__ 无形参，仅调用父类 ABC 构造；
    不持有数据库连接，会话在各方法内通过 session_scope() 获取，
    随 with 块自动提交/回滚。
    """

    def __init__(self):
        super().__init__()

    def save_information(self, data: Dict[str, Any]):
        """保存一轮对话：同一事务内插入 user 与 assistant 两条消息。

        功能：向 session_information 依次 INSERT 一条 role='user'（用户提问）
        与一条 role='assistant'（AI 回答）；两条语句共用一个 session_scope
        事务，同时成功才提交，任一失败整体回滚，避免只存半轮对话。
        SQL 安全：SQL 经 text() 构造，session_id/user_id/content 均为命名绑定
        参数，role 为 SQL 内固定字面量（不接受外部传入），经
        core.sql_guard.safe_execute 执行，杜绝 SQL 注入。

        被谁调用：早期逐轮落库链路；当前生产主链路已由 Redis 短期记忆 +
        save_messages_batch() 批量落库替代（app/domain/memory/long_term.py），本方法保留兼容。
        参数：
            data: 字段字典，由 service 层传入：
                  - session_id：会话 ID（per-user 会话序号）；
                  - user_id：登录态用户 ID；
                  - user_input：本轮用户提问文本（写入 role='user' 行）；
                  - ai_output：本轮 AI 回答文本（写入 role='assistant' 行）。
        返回：str。"success" 表示两条消息均已提交；"false" 表示发生异常
              （已记录 error 日志，事务回滚，不向上抛出）。
        异常：捕获全部 Exception，记日志后返回 "false"。
        """
        try:
            with session_scope() as session:
                # 第一条：用户提问（role 在 SQL 中固定为 'user'，不走外部入参）
                safe_execute(session,
                    text(
                        """
                        INSERT INTO session_information(session_id, user_id, role, content)
                        VALUES (:session_id, :user_id, 'user', :content)
                        """
                    ),
                    {
                        "session_id": data["session_id"],
                        "user_id": data["user_id"],
                        "content": data["user_input"],
                    },
                )
                # 第二条：AI 回答（role 在 SQL 中固定为 'assistant'），
                # 与第一条处于同一事务，失败则两条一起回滚
                safe_execute(session,
                    text(
                        """
                        INSERT INTO session_information(session_id, user_id, role, content)
                        VALUES (:session_id, :user_id, 'assistant', :content)
                        """
                    ),
                    {
                        "session_id": data["session_id"],
                        "user_id": data["user_id"],
                        "content": data["ai_output"],
                    },
                )
            logger.info(
                "Session messages saved: user_id=%s, session_id=%s",
                data.get("user_id"),
                data.get("session_id"),
            )
            return "success"
        except Exception as e:
            logger.error("Error saving session messages: %s", e)
            return "false"

    def save_messages_batch(self, user_id: int, session_id: int, messages: List[Dict[str, str]]) -> bool:
        """批量写入消息（INSERT 多行，单事务 executemany）。

        功能：短期记忆（Redis）临近过期时，把该会话缓存的多轮消息一次性转存到
        session_information 表，替代逐轮两条 INSERT，显著减少数据库请求次数；
        整批共用一个 session_scope 事务，全部成功才提交，异常整体回滚。
        SQL 安全：SQL 经 text() 构造，role 虽来自消息字典但仅取
        m.get("role")（由内部短期记忆生成，取值只会是 user/assistant，缺省
        兜底为 "user"），session_id/user_id/content 均以绑定参数传入
        （safe_execute 收到 list 形参时按 executemany 批量执行），杜绝 SQL 注入。

        被谁调用：app/domain/memory/long_term.py 的落库逻辑
        （文件.函数：long_term.LongTermMemory 内的转存方法，调用点
        self._information_dao.save_messages_batch(...)）。
        参数：
            user_id: 登录态用户 ID，由长期记忆任务从缓存键解析得到。
            session_id: 会话 ID（per-user 会话序号），同上。
            messages: 时间升序的消息列表，元素为
                      {"role": "user"|"assistant", "content": str}，
                      来源 Redis 短期记忆中缓存的该会话历史轮次。
        返回：bool。True 表示整批已提交；入参为空（无用户/会话/消息）时
              直接返回 True（视为无操作成功）；异常时返回 False 并记录日志，
              由调用方决定是否保留缓存等待下次重试。
        异常：捕获全部 Exception，记日志后返回 False，不向上抛出。
        """
        if not user_id or not session_id or not messages:
            return True
        rows = [
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": (m.get("role") or "user"),
                "content": m.get("content", ""),
            }
            for m in messages
        ]
        try:
            with session_scope() as session:
                safe_execute(session,
                    text(
                        """
                        INSERT INTO session_information(session_id, user_id, role, content)
                        VALUES (:session_id, :user_id, :role, :content)
                        """
                    ),
                    rows,
                )
            logger.info(
                "Session messages batch saved: user_id=%s, session_id=%s, count=%s",
                user_id, session_id, len(rows),
            )
            return True
        except Exception as e:
            logger.error(
                "Error batch saving session messages (uid=%s,sid=%s): %s",
                user_id, session_id, e,
            )
            return False
