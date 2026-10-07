"""
模块名：app.infrastructure.persistence.repositories.session

作用：
    会话生命周期管理，操作两张 MySQL 表：
    - history_information：会话列表条目（user_id / session_id / title / 时间戳），
      session_id 为每用户独立递增序号；
    - session_information：会话内聊天消息（user_id / session_id / role / content）。
    提供会话创建（含并发取号）、列表分页、详情、归属校验、改名、软删除转发，
    以及一条单消息 INSERT 兼容方法。

主要成员：
    - 模块级：_user_seq_locks / _seq_locks_guard（per-user 取号锁注册表）、
      _get_user_lock()；
    - SessionDAO：save_information / get_session_list / get_session_list_paged /
      get_session_detail / is_session_owner / create_session /
      update_session_title / delete_session。

被谁使用：
    - service/chat_service.py 的 ChatService.__init__ 实例化
      （self.session_dao = SessionDAO()），聊天收发时建会话/校验归属；
    - control/history_control.py 各历史会话端点（列表/详情/新建/改名/删除/撤销）；
    - control/login_control.py 登录成功后拼装会话列表；
    - control/file_control.py 上传临时文档前校验会话归属；
    - app/domain/memory/context_app.domain.memory.py（ContextMemory 持有 _session_dao 读详情）、
      app/domain/memory/session_rollover.py（会话翻转时 create_session）。
"""
import logging
import random
import threading
import time
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .base_information import BaseInformation
from app.infrastructure.redis.locks import distributed_lock
from core.sql_guard import safe_execute
from app.infrastructure.persistence.session import session_scope

logger = logging.getLogger(__name__)

# 应用内 per-user 取号锁注册表：{user_id: threading.Lock}。
# 单进程内同用户的会话创建完全串行，彻底消除 MAX(session_id)+1 的并发竞态；
# 多副本部署时仍有 FOR UPDATE + 唯一键 + 重试兜底。
_user_seq_locks: Dict[int, threading.Lock] = {}
# 保护 _user_seq_locks 字典本身增删的短临界区守卫锁（不保护取号过程）
_seq_locks_guard = threading.Lock()


def _get_user_lock(user_id: int) -> threading.Lock:
    """获取（不存在则注册）指定用户的进程内取号锁。

    功能：在 _seq_locks_guard 短临界区内按 user_id 取/建 threading.Lock，
    保证同一用户在本进程内始终拿到同一把锁实例；不同用户各持各锁、互不阻塞。
    被谁调用：SessionDAO.create_session()。
    参数：
        user_id: 用户 ID（锁粒度键）。
    返回：threading.Lock，该用户专属的锁对象（非可重入锁，禁止同线程嵌套获取）。
    """
    with _seq_locks_guard:
        lock = _user_seq_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_seq_locks[user_id] = lock
        return lock


class SessionDAO(BaseInformation):
    """会话管理数据访问层，操作 history_information 与 session_information 两张表。

    承担会话的增（create_session / save_information）、查（get_session_list /
    get_session_list_paged / get_session_detail / is_session_owner）、
    改（update_session_title）、软删除（delete_session 转发 dao.soft_delete）。
    实例化位置：service/chat_service.py 的 ChatService.__init__、
    control/history_control.py 各端点函数、control/login_control.py、
    control/file_control.py、app/domain/memory/context_app.domain.memory.py 的 ContextMemory.__init__、
    app/domain/memory/session_rollover.py。__init__ 无形参，仅调用父类 ABC 构造；
    不持有数据库连接，会话在各方法内通过 session_scope() 获取。
    """

    def __init__(self):
        super().__init__()

    def save_information(self, data: Dict[str, Any]):
        """保存一条聊天消息（INSERT INTO session_information，显式写入 create_time）。

        功能：插入单条 role/content 消息并以 NOW() 记录消息时间；单事务自动提交。
        SQL 安全：user_id/session_id/role/content 均为命名绑定参数，经
        core.sql_guard.safe_execute 执行，杜绝 SQL 注入。
        现状：当前主链路对话期间只写 Redis 短期记忆，由
        app/domain/memory/long_term.py 批量落库（dao/information.py），本方法为早期
        单条写入接口，代码库中暂无生产调用，保留兼容。
        参数：
            data: {"user_id": 用户 ID, "session_id": 会话 ID,
                   "role": "user"/"assistant", "content": 消息文本}，
                  由调用方（早期聊天链路）组装。
        返回：str。"success" 已提交；"false" 发生异常（记日志，不向上抛出）。
        异常：捕获全部 Exception，记日志后返回 "false"。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text(
                        """
                        INSERT INTO session_information(user_id, session_id, role, content, create_time)
                        VALUES (:user_id, :session_id, :role, :content, NOW())
                        """
                    ),
                    {
                        "user_id": data.get("user_id"),
                        "session_id": data.get("session_id"),
                        "role": data.get("role"),
                        "content": data.get("content"),
                    },
                )
            logger.info(
                "Session message saved: user_id=%s, session_id=%s",
                data.get("user_id"),
                data.get("session_id"),
            )
            return "success"
        except Exception as e:
            logger.error("Error saving session message: %s", e)
            return "false"

    def get_session_list(self, user_id: int) -> List[Dict[str, Any]]:
        """获取用户的全部会话列表（不分页，按最近消息时间倒序）。

        功能：get_session_list_paged(range_="all", limit=None) 的便捷封装，
        仅取 data 部分。
        被谁调用：control/login_control.py 登录/注册成功后拼装返回会话列表
        （文件.函数：login_control 登录处理函数）。
        参数：
            user_id: 登录态用户 ID（登录成功后回拉该用户全部会话）。
        返回：List[Dict[str, Any]]，元素结构同 get_session_list_paged 的 data；
              异常时底层返回 {"total": 0, "data": []}，本方法得 []。
        """
        return self.get_session_list_paged(user_id, range_="all", limit=None, offset=0)["data"]

    # 长期记忆时间范围 -> SQL INTERVAL 表达式白名单（仅此处的固定字面量会被
    # 拼进 SQL，外部 range_ 只作字典键查表，杜绝通过时间范围参数注入 SQL）
    _RANGE_INTERVAL = {"day": "1 DAY", "week": "7 DAY"}

    def get_session_list_paged(
        self, user_id: int, range_: str = "all", limit: int = None, offset: int = 0
    ) -> Dict[str, Any]:
        """按时间范围分页获取会话列表（长期记忆/历史信息页，history 左联消息聚合）。

        range_: day（最近一天）/ week（最近一星期）/ all（全部）；
        以会话最后一条消息时间（无消息时为会话创建时间）排序与过滤。
        返回 {"total": 范围总数, "data": 当前页行}。

        被谁调用：
        - 本类 get_session_list()（登录后全量列表）；
        - control/history_control.py 的会话列表端点
          （文件.函数：history_control 列表处理函数，带 day/week/分页参数）。
        参数：
            user_id: 登录态用户 ID，WHERE 强制按用户隔离，只能列出自己的会话。
            range_: 时间范围键，仅允许 day/week/all；非法值等价 all。
            limit: 每页行数；None/0 表示不分页（此时仍受 sql_guard
                   SQL_MAX_ROWS 上限保护）；非空时经 int 强转并下限钳制。
            offset: 分页偏移量，非负（int 强转、下限 0）。
        返回：Dict[str, Any]：{"total": int 范围内总数, "data": [
              {"session_id", "title"(空标题兜底“未命名会话”),
               "created_at", "last_message_time"}, ...]}；
              异常时返回 {"total": 0, "data": []}。
        SQL 安全：where_extra 中拼接的 INTERVAL 片段只能取自
              _RANGE_INTERVAL 白名单常量，range_ 本身从不进 SQL；
              user_id/limit/offset 全部命名绑定参数；两表均带
              is_deleted = 0；经 safe_execute 执行。
        异常：捕获全部 Exception，记日志后返回空结构。
        """
        interval = self._RANGE_INTERVAL.get(range_)
        where_extra = ""
        params: Dict[str, Any] = {"user_id": user_id}
        if interval:
            # interval 来自类常量白名单（"1 DAY"/"7 DAY"），非外部输入，可安全拼接
            where_extra = (
                " AND COALESCE(s.last_message_time, h.create_time) >= DATE_SUB(NOW(), INTERVAL "
                + interval
                + ")"
            )
        try:
            with session_scope() as session:
                # 联表意图：子查询 s 先按 session_id 聚合出每个会话最后消息时间，
                # 再与会话主表 h 左联，供 COUNT 与列表排序共用同一口径
                total_row = safe_execute(session,
                    text(
                        f"""
                        SELECT COUNT(*) AS cnt
                        FROM history_information h
                        LEFT JOIN (
                            SELECT session_id, MAX(create_time) AS last_message_time
                            FROM session_information
                            WHERE user_id = :user_id AND is_deleted = 0
                            GROUP BY session_id
                        ) s ON h.session_id = s.session_id
                        WHERE h.user_id = :user_id AND h.is_deleted = 0{where_extra}
                        """
                    ),
                    params,
                ).mappings().first()

                sql = f"""
                    SELECT
                        h.session_id AS session_id,
                        h.title AS title,
                        h.create_time AS create_time,
                        s.last_message_time AS last_message_time
                    FROM history_information h
                    LEFT JOIN (
                        SELECT session_id, MAX(create_time) AS last_message_time
                        FROM session_information
                        WHERE user_id = :user_id AND is_deleted = 0
                        GROUP BY session_id
                    ) s ON h.session_id = s.session_id
                    WHERE h.user_id = :user_id AND h.is_deleted = 0{where_extra}
                    ORDER BY COALESCE(s.last_message_time, h.create_time) DESC,
                             h.create_time DESC,
                             h.session_id DESC
                    """
                if limit:
                    # 分页值以绑定参数传入（int 强转 + 下限钳制），不把外部数字拼进 SQL 文本
                    sql += " LIMIT :limit OFFSET :offset"
                    params["limit"] = max(1, int(limit))
                    params["offset"] = max(0, int(offset))
                rows = safe_execute(session,text(sql), params).mappings().all()
            data = [
                {
                    "session_id": row["session_id"],
                    "title": row["title"] if row["title"] else "未命名会话",
                    "created_at": row["create_time"],
                    "last_message_time": row["last_message_time"],
                }
                for row in rows
            ]
            return {"total": int(total_row["cnt"]) if total_row else 0, "data": data}
        except Exception as e:
            logger.error("Error getting session list paged: %s", e)
            return {"total": 0, "data": []}

    def get_session_detail(self, user_id: int, session_id: int) -> List[Dict[str, Any]]:
        """获取会话详情（聊天记录，按时间正序）。user_id 强制来自登录态，防止越权读取他人会话。

        功能：从 session_information 取指定 (user_id, session_id) 的全部未软删
        消息，ORDER BY create_time ASC 还原对话顺序。
        SQL 安全：user_id/session_id 双键命名绑定参数（用户隔离 + 会话定位），
        is_deleted = 0 软删除过滤；经 safe_execute 执行，无显式 LIMIT 时
        SELECT 受 sql_guard 的 SQL_MAX_ROWS 钳制。
        被谁调用：
        - control/history_control.py 的会话详情端点
          （文件.函数：history_control 详情处理函数）；
        - app/domain/memory/context_app.domain.memory.py 的 ContextMemory 读取历史消息重建上下文
          （文件.函数：context_app.domain.memory.ContextMemory 内 self._session_dao.get_session_detail(...)）。
        参数：
            user_id: 登录态用户 ID（不得取自请求体）。
            session_id: 目标会话 ID，来源前端请求（须先经归属校验）。
        返回：List[Dict[str, Any]]，每项 {"role", "content", "created_at"}；
              无记录或异常返回 []（异常已记日志）。
        异常：捕获全部 Exception，记日志后返回 []。
        """
        try:
            with session_scope() as session:
                rows = safe_execute(session,
                    text(
                        """
                        SELECT role, content, create_time
                        FROM session_information
                        WHERE user_id = :user_id AND session_id = :session_id AND is_deleted = 0
                        ORDER BY create_time ASC
                        """
                    ),
                    {"user_id": user_id, "session_id": session_id},
                ).mappings().all()
            return [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["create_time"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("Error getting session detail: %s", e)
            return []

    def is_session_owner(self, user_id: int, session_id: int) -> bool:
        """会话归属校验：该会话必须属于当前用户（SELECT 1 FROM history_information）。

        会话号为 per-user 序列（各用户独立从 1 开始编号），任何按 session_id
        的写入/上传必须先经此校验防止串会话。
        SQL 安全：user_id/session_id 双键命名绑定参数 + is_deleted = 0；
        LIMIT 1 找到即止；经 safe_execute 执行。
        被谁调用：
        - service/chat_service.py 的聊天收发（文件.函数：
          chat_service.ChatService 流式/非流式入口，传入已有 session_id 时校验）；
        - control/file_control.py 上传会话临时文档前
          （SessionDAO().is_session_owner(...)）；
        - control/history_control.py 删除/改名等端点。
        参数：
            user_id: 登录态用户 ID。
            session_id: 请求声称要操作的会话 ID。
        返回：bool。history_information 中存在该未软删 (user_id, session_id)
              行即 True；任一参数为空、不存在或查询异常均 False
              （异常 fail-closed，记日志，宁可不放行）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        if not user_id or not session_id:
            return False
        try:
            with session_scope() as session:
                row = safe_execute(session,
                    text(
                        """
                        SELECT 1 FROM history_information
                        WHERE user_id = :user_id AND session_id = :session_id AND is_deleted = 0
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id, "session_id": session_id},
                ).first()
            return row is not None
        except Exception as e:
            logger.error("is_session_owner check failed: %s", e)
            return False

    def create_session(self, user_id: int, title: str = "新会话") -> int:
        """创建新会话（INSERT history_information），返回新会话 ID。

        取号语义：session_id 为 per-user 递增序号，新号 = 该用户现有
        MAX(session_id) + 1（无历史记录时从 1 开始）。

        并发安全（四层）：
        1. Redis 分布式锁（多副本互斥；Redis 不可用时自动降级为 no-op）；
        2. 应用内 per-user 锁：单进程内同用户取号串行化；
        3. 事务内 SELECT ... FOR UPDATE 锁定该用户的会话序列（多副本兜底）；
        4. (user_id, session_id) 唯一键 + 指数退避重试（最多 5 次）。
        （修复旧实现重试仅 1 次，5 并发下成功率仅 40% 的问题）

        SQL 安全：user_id/session_id/title 均为命名绑定参数，经
        safe_execute 执行；FOR UPDATE 行锁与唯一键冲突均不改变参数化方式。
        被谁调用：
        - service/chat_service.py 的聊天入口（文件.函数：
          chat_service.ChatService 流式/非流式入口，首条消息时建会话）；
        - control/history_control.py 的新建会话端点；
        - app/domain/memory/session_rollover.py 会话数量翻转时建新会话。
        参数：
            user_id: 登录态用户 ID。
            title: 初始标题，缺省“新会话”；翻转场景由调用方传入派生标题。
        返回：int。成功返回新会话 ID（>=1）；5 次重试均冲突或发生其他异常时
              返回 0，调用方按建会话失败处理。
        异常：IntegrityError（唯一键冲突）内部退避重试；其他异常记日志返回 0。
        """
        # 第 1+2 层：先取进程内 per-user 锁，再尝试 Redis 分布式锁（多副本互斥）
        with _get_user_lock(user_id):
            with distributed_lock(f"seq:{user_id}"):
                for attempt in range(5):
                    try:
                        with session_scope() as session:
                            # 第 3 层：FOR UPDATE 锁定该用户会话序列相关行，
                            # 阻塞多副本下的并发 MAX+1 取号，与后续 INSERT 同事务
                            row = safe_execute(session,
                                text(
                                    """
                                    SELECT MAX(session_id) AS max_session_id
                                    FROM history_information
                                    WHERE user_id = :user_id
                                    FOR UPDATE
                                    """
                                ),
                                {"user_id": user_id},
                            ).mappings().first()
                            new_session_id = (row["max_session_id"] or 0) + 1
                            safe_execute(session,
                                text(
                                    """
                                    INSERT INTO history_information (user_id, session_id, title)
                                    VALUES (:user_id, :session_id, :title)
                                    """
                                ),
                                {
                                    "user_id": user_id,
                                    "session_id": new_session_id,
                                    "title": title,
                                },
                            )
                        logger.info(
                            "Session created: user_id=%s, session_id=%s",
                            user_id,
                            new_session_id,
                        )
                        return new_session_id
                    except IntegrityError:
                        # 唯一键冲突：并发下另一请求已插入，指数退避后重试
                        if attempt < 4:
                            logger.warning(
                                "create_session conflict, retry %d: user_id=%s",
                                attempt + 1,
                                user_id,
                            )
                            time.sleep(0.05 * (2 ** attempt) + random.uniform(0, 0.05))
                            continue
                        logger.error("Error creating session (integrity): user_id=%s", user_id)
                        return 0
                    except Exception as e:
                        logger.error("Error creating session: %s", e)
                        return 0
        return 0

    def update_session_title(self, user_id: int, session_id: int, title: str) -> bool:
        """更新会话标题（UPDATE history_information，WHERE 强制 user_id 校验归属）。

        功能：仅改 title 列；WHERE 同时带 user_id + session_id + is_deleted=0，
        他人会话或已删会话改不到任何行（rowcount=0）。
        SQL 安全：title/user_id/session_id 均为命名绑定参数，经 safe_execute
        执行，杜绝标题内容中的 SQL 注入。
        被谁调用：control/history_control.py 的会话改名端点
        （文件.函数：history_control 改名处理函数，调用前通常已做
        is_session_owner 校验，WHERE 双键为纵深防护）。
        参数：
            user_id: 登录态用户 ID。
            session_id: 待改名会话 ID，来源请求体。
            title: 新标题，来源请求体（用户自定义名）。
        返回：bool。True 命中并更新；False 表示无匹配行或异常（异常记日志）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        try:
            with session_scope() as session:
                result = safe_execute(session,
                    text(
                        """
                        UPDATE history_information
                        SET title = :title
                        WHERE user_id = :user_id AND session_id = :session_id AND is_deleted = 0
                        """
                    ),
                    {"title": title, "user_id": user_id, "session_id": session_id},
                )
                return result.rowcount > 0
        except Exception as e:
            logger.error("Error updating session title: %s", e)
            return False

    def delete_session(self, user_id: int, session_id: int) -> bool:
        """软删除会话及其全部聊天记录（转发 dao.soft_delete.soft_delete_session）。

        功能：本方法不含 SQL，仅做模块内转发，实际逻辑为同一事务内把
        history_information 与 session_information 的对应行置 is_deleted=1、
        deleted_at=NOW()，并写撤销快照、清关键词缓存；归属由 UPDATE 的
        WHERE user_id 条件与调用前的 is_session_owner 双重保证。
        被谁调用：control/history_control.py 的删除会话端点
        （文件.函数：history_control 删除处理函数）。
        参数：
            user_id: 登录态用户 ID。
            session_id: 待删会话 ID，来源二次确认后的请求体。
        返回：bool，透传 soft_delete_session 的结果（True 至少会话主表有行被标记；
              False 无匹配或异常）。
        """
        # 函数内延迟导入，避免 dao.session ↔ dao.soft_delete 的循环导入
        from app.infrastructure.persistence.repositories.soft_delete import soft_delete_session
        return soft_delete_session(user_id, session_id)
