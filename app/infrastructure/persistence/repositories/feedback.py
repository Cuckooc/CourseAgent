"""
模块名：app.infrastructure.persistence.repositories.feedback

作用：
    用户对话反馈（点赞/点踩）的持久化，操作 MySQL 表 chat_feedback
    （字段：user_id / session_id / message_index / rating / comment）。

主要成员：
    - FeedbackDAO：反馈写入 DAO，仅提供 insert()。

被谁使用：
    - app/api/v1/chat.py 的 feedback() 接口（POST /chat/feedback）
      中以 FeedbackDAO() 临时实例化并调用 insert()。
"""
import logging

from sqlalchemy import text

from core.sql_guard import safe_execute
from app.infrastructure.persistence.session import session_scope

logger = logging.getLogger(__name__)


class FeedbackDAO:
    """用户反馈数据访问层，对应 MySQL 表 chat_feedback。

    仅承担反馈记录的新增（INSERT），不提供查询/修改/删除。
    实例化位置：app/api/v1/chat.py 的 feedback()（FeedbackDAO() 临时创建）。
    无 __init__ 形参、不持有连接；insert() 内部以 session_scope() 获取会话，
    随 with 块自动提交/回滚。
    """

    def insert(self, user_id, session_id, message_index, rating, comment=None):
        """新增一条用户反馈记录（INSERT INTO chat_feedback）。

        功能：将用户对某轮 AI 回答的评价写入 chat_feedback 表，单事务自动提交。
        SQL 安全：SQL 经 text() 构造，user_id/session_id/message_index/rating/comment
        全部以命名绑定参数传入并经 core.sql_guard.safe_execute 执行，杜绝 SQL 注入。

        被谁调用：app/api/v1/chat.py 的 feedback()
        （文件.函数：chat_control.feedback）。
        参数：
            user_id: 反馈用户 ID，来源登录态 current_user["user_id"]。
            session_id: 反馈所属会话 ID，来源请求体 FeedbackRequest.session_id。
            message_index: 会话内被评价消息（轮次）的下标，来源请求体。
            rating: 评价分值，来源请求体（control 层已校验仅允许 1=点赞 / -1=点踩）。
            comment: 可选文字评论，来源请求体 FeedbackRequest.comment，缺省为 None。
        返回：bool。True 写入成功并已提交；False 表示发生异常
              （已记录 exception 日志，不向上抛出，control 层据此返回“反馈保存失败”）。
        异常：捕获全部 Exception 并记日志后返回 False，不向调用方抛出。
        """
        try:
            with session_scope() as session:
                safe_execute(
                    session,
                    text(
                        "INSERT INTO chat_feedback"
                        " (user_id, session_id, message_index, rating, comment)"
                        " VALUES (:user_id, :session_id, :message_index, :rating, :comment)"
                    ),
                    {
                        "user_id": user_id,
                        "session_id": session_id,
                        "message_index": message_index,
                        "rating": rating,
                        "comment": comment,
                    },
                )
            logger.info(
                "Feedback saved: user_id=%s, session_id=%s, index=%s, rating=%s",
                user_id, session_id, message_index, rating,
            )
            return True
        except Exception:
            logger.exception("Failed to save feedback")
            return False
