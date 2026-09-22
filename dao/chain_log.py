"""
模块名：dao.chain_log

作用：
    Agent 多智能体链路日志的持久化，操作 MySQL 表 chain_log
    （字段：user_id / session_id / task_id / log_data，log_data 为 JSON 文本）。

主要成员：
    - ChainLogDAO：链路日志写入 DAO，目前仅提供 insert() 一个方法。

被谁使用：
    - service/agent_service.py 的 AgentService._persist_chain_log()
      （通过线程池异步调用 ChainLogDAO().insert(...)，写库失败不阻塞聊天响应）。
"""
import json
import logging

from sqlalchemy import text

from core.sql_guard import safe_execute
from db.session import session_scope

logger = logging.getLogger(__name__)


class ChainLogDAO:
    """Agent 链路日志数据访问层，对应 MySQL 表 chain_log。

    仅承担链路日志的新增（INSERT）操作，不提供查询/修改/删除。
    实例化位置：service/agent_service.py 的 AgentService._persist_chain_log()
    中以 ChainLogDAO() 临时创建（无 __init__ 形参，不持有连接，
    会话在 insert() 内通过 session_scope() 获取并随 with 块自动提交/回滚）。
    """

    def insert(self, user_id, session_id, task_id, chain_log):
        """新增一条链路日志记录（INSERT INTO chain_log）。

        功能：将一次 Agent 执行的完整链路（各智能体调用顺序/耗时/状态等）
        序列化为 JSON 后写入 chain_log 表；session_scope 单事务，
        正常退出自动提交，异常自动回滚。
        SQL 安全：INSERT 文本经 sqlalchemy text() 构造，全部入参以命名绑定参数
        （:user_id 等）传入并经 core.sql_guard.safe_execute 执行，杜绝 SQL 注入。

        被谁调用：service/agent_service.py 的 AgentService._persist_chain_log()
        （文件.函数：agent_service.AgentService._persist_chain_log）。
        参数：
            user_id: 发起对话的用户 ID，来源为会话状态对象 SessionManager.user_id
                     （登录态用户 id）。
            session_id: 会话 ID（per-user 会话序号），来源 SessionManager.session_id。
            task_id: 本次 Agent 执行的任务 ID，来源 AgentService 调用链生成。
            chain_log: 链路日志结构化对象（list/dict），来源 SessionManager.chain_log；
                       内部以 json.dumps(..., ensure_ascii=False, default=str)
                       序列化，非 ASCII 不转义、不可序列化对象退化为字符串。
        返回：bool。True 表示写入成功并已提交；False 表示发生任意异常
              （已记录 exception 日志，不向上抛出，调用方按“日志尽力持久化”处理）。
        异常：本方法捕获全部 Exception 并记日志后返回 False，不向调用方抛出。
        """
        try:
            log_json = json.dumps(chain_log, ensure_ascii=False, default=str)
            with session_scope() as session:
                safe_execute(
                    session,
                    text(
                        "INSERT INTO chain_log (user_id, session_id, task_id, log_data)"
                        " VALUES (:user_id, :session_id, :task_id, :log_data)"
                    ),
                    {
                        "user_id": user_id,
                        "session_id": session_id,
                        "task_id": task_id,
                        "log_data": log_json,
                    },
                )
            logger.info(
                "Chain log persisted: user_id=%s, session_id=%s, task_id=%s, entries=%d",
                user_id, session_id, task_id, len(chain_log),
            )
            return True
        except Exception:
            logger.exception("Failed to persist chain log")
            return False
