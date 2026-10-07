"""
模块名：app.infrastructure.persistence.repositories.history

作用：
    会话历史（会话列表条目）写入，操作 MySQL 表 history_information
    （每用户每会话一条，字段 user_id / session_id / title / update_time 等）。

主要成员：
    - Information_history：继承 BaseInformation，实现 save_information() 的 upsert 写入。

被谁使用：
    - service/chat_service.py 的 ChatService.__init__ 中实例化
      （self.history_dao = Information_history()），并在 _save_information()
      中即时持久化 AI 生成的会话标题；
    - memory/long_term.py 的长期记忆落库任务构造时实例化（_history_dao），
      短期记忆转长期记忆时写入/刷新会话历史条目。

使用 INSERT ... ON DUPLICATE KEY UPDATE 幂等写入：
- 同一 (user_id, session_id) 只保留一条历史记录（依赖唯一键 uk_user_session）；
- 默认标题（新会话/未命名会话）可被 AI 生成标题覆盖，自定义标题不被覆盖；
- 修复旧实现每轮对话插入一条重复历史记录的问题。
"""
from .base_information import BaseInformation
from app.infrastructure.persistence.session import session_scope
from sqlalchemy import text
from core.sql_guard import safe_execute
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)


class Information_history(BaseInformation):
    """会话历史写入 DAO，对应 MySQL 表 history_information。

    承担会话历史条目的幂等 upsert（INSERT ... ON DUPLICATE KEY UPDATE），
    不承担查询与删除（查询见 dao/session.py，软删除见 dao/soft_delete.py）。
    实例化位置：service/chat_service.py 的 ChatService.__init__
    （self.history_dao）、memory/long_term.py 的 LongTermMemory 构造
    （self._history_dao）。__init__ 无形参，仅调用父类 ABC 构造；
    不持有数据库连接，会话在 save_information() 内通过 session_scope() 获取。
    """

    def __init__(self):
        super().__init__()

    def save_information(self, data: Dict[str, Any]):
        """幂等写入/刷新一条会话历史记录（upsert history_information）。

        功能：按唯一键 (user_id, session_id) 插入会话条目；若已存在则只刷新
        update_time，并在旧标题为默认标题（“新会话”/“未命名会话”）时才用新标题
        覆盖——保护用户自定义标题不被 AI 默认标题回写覆盖。
        SQL 安全：SQL 经 text() 构造，user_id/session_id/title 全部使用命名绑定
        参数并经 core.sql_guard.safe_execute 执行，杜绝 SQL 注入；
        单会话内执行，随 with 块自动提交，异常自动回滚。

        被谁调用：
        - service/chat_service.py 的 ChatService._save_information()
          （文件.函数：chat_service.ChatService._save_information，标题即时持久化）；
        - memory/long_term.py 的长期记忆落库逻辑
          （文件.函数：long_term.LongTermMemory 内的落库方法）。
        参数：
            data: 字段字典，由 service 层传入：
                  - user_id：登录态用户 ID；
                  - session_id：会话 ID（per-user 会话序号）；
                  - title：会话标题，缺省/为空时落库为默认标题“新会话”。
        返回：str。"success" 表示 upsert 成功并提交；"false" 表示发生异常
              （已记录 error 日志，不向上抛出，调用方按尽力持久化处理）。
        异常：捕获全部 Exception，记日志后返回 "false"。
        """
        try:
            with session_scope() as session:
                # upsert：唯一键 uk_user_session 冲突时转 UPDATE；
                # IF(title IN (...)) 保证只有默认标题可被覆盖，自定义标题不动
                safe_execute(session,
                    text(
                        """
                        INSERT INTO history_information(user_id, session_id, title)
                        VALUES (:user_id, :session_id, :title)
                        ON DUPLICATE KEY UPDATE
                            title = IF(title IN ('新会话', '未命名会话'), VALUES(title), title),
                            update_time = NOW()
                        """
                    ),
                    {
                        "user_id": data["user_id"],
                        "session_id": data["session_id"],
                        "title": data.get("title") or "新会话",
                    },
                )
            logger.info(
                "History upserted: user_id=%s, session_id=%s",
                data.get("user_id"),
                data.get("session_id"),
            )
            return "success"
        except Exception as e:
            logger.error("Error saving history: %s", e)
            return "false"
