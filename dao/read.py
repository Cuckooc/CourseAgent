"""
模块名：dao.read

作用：
    用户与会话信息的只读访问（另有一个密码升级的 UPDATE），查询两张 MySQL 表：
    - user_information（用户账号：user_name/user_pwd/email/role/token_version 等）；
    - session_information（会话消息：session_id/role/content/create_time 等）。

主要成员：
    - Information_Read：只读 DAO，方法包括 read_information / user_information /
      check_information / get_by_id / get_by_ids / get_all_users /
      get_by_username / get_by_email / upgrade_password / save_information（空实现）。

被谁使用：
    - util/user.py 的 UserInformation.__init__ 实例化（注册查重、登录、密码升级）；
    - core/deps.py 的 get_current_user 中局部实例化（get_by_id/get_by_ids 鉴权）；
    - control/admin_control.py、service/admin_user_service.py（管理端用户查询）；
    - control/history_control.py（撤销删除预览时 read_information 读最近消息）；
    - control/login_control.py（登录后用户信息组装）。

所有 SQL 均使用 text() + 绑定参数，杜绝 SQL 注入；SELECT 经
core.sql_guard.safe_execute 执行，无 LIMIT 时由 sql_guard 自动钳制到
SQL_MAX_ROWS（默认 10000），并在 SQL 内显式带 is_deleted = 0 软删除过滤。
"""
from .base_information import BaseInformation
from db.session import session_scope
from sqlalchemy import text
from core.sql_guard import safe_execute
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class Information_Read(BaseInformation):
    """用户/会话只读数据访问层，查询 user_information 与 session_information 表。

    承担账号存在性检查、登录凭据查询、鉴权用户信息查询、管理端用户列表、
    会话最近消息读取；写操作仅 upgrade_password 一个历史密码升级 UPDATE。
    实例化位置：util/user.py 的 UserInformation.__init__
    （self.information_read = Information_Read()）、core/deps.py 鉴权依赖、
    control/admin_control.py、control/history_control.py、control/login_control.py、
    service/admin_user_service.py。__init__ 无形参，仅调用父类 ABC 构造；
    不持有数据库连接，会话在各方法内通过 session_scope() 获取。
    """

    def __init__(self):
        super().__init__()

    def read_information(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """读取某会话最近 6 条消息（SELECT role/content，LIMIT 6 行数上限）。

        功能：按 (user_id, session_id) 从 session_information 取最近 6 条
        未软删除消息，按 create_time 倒序（调用方按需自行反转得到时间正序）。
        SQL 安全：user_id/session_id 为命名绑定参数；显式 is_deleted = 0
        软删除过滤；LIMIT 6 固定行数上限，防止大结果集拖库；经 safe_execute 执行。

        被谁调用：control/history_control.py 的撤销删除预览逻辑
        （文件.函数：history_control 中 read_dao.read_information(...)）。
        参数：
            data: {"user_id": 登录态用户 ID, "session_id": 目标会话 ID}，
                  双键由调用方保证归属（展示刚删除会话的残留消息预览）。
        返回：List[Dict[str, Any]]，每项 {"role", "content"}；无数据或异常时
              返回空列表（异常已记日志，不向上抛出）。
        异常：捕获全部 Exception，记日志后返回 []。
        """
        try:
            with session_scope() as session:
                rows = safe_execute(session,
                    text(
                        """
                        SELECT role, content
                        FROM session_information
                        WHERE user_id = :user_id AND session_id = :session_id AND is_deleted = 0
                        ORDER BY create_time DESC
                        LIMIT 6
                        """
                    ),
                    {"user_id": data["user_id"], "session_id": data["session_id"]},
                ).mappings().all()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error reading session messages: %s", e)
            return []

    def user_information(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按 user_id 或 (user_name, email) 查询用户及其最近一个会话（LEFT JOIN）。

        功能：user_information 左联 session_information，取该用户未软删除消息中
        session_id 最大的一条（即最近会话），用户无会话时经 LEFT JOIN 仍返回
        用户行、session_id 为 NULL。
        SQL 安全：user_id / user_name / email 均为命名绑定参数；两表都带
        is_deleted = 0 过滤；LIMIT 1 限定行数；经 safe_execute 执行。
        现状：当前 util/user.py 的 UserInformation.user_information() 已改为直接
        调用 get_by_id()，本方法在代码库中暂无生产调用方，保留兼容早期聊天
        身份解析链路。
        参数：
            data: 二选一查询条件：{"user_id": ...} 或
                  {"user_name": ..., "email": ...}（两者必须同时存在）；
                  都不满足时直接返回 []。
        返回：List[Dict[str, Any]]，元素为 {"id", "user_name", "session_id"}；
              查询异常时返回 []。
        异常：捕获全部 Exception，记日志后返回 []。
        """
        try:
            with session_scope() as session:
                if "user_id" in data:
                    # 联表意图：以用户为主表 LEFT JOIN 消息表，取该用户最近会话号；
                    # JOIN 条件内带 s.is_deleted=0，避免软删消息影响最大会话号
                    rows = safe_execute(session,
                        text(
                            """
                            SELECT u.id AS id, u.user_name AS user_name,
                                   s.session_id AS session_id
                            FROM user_information u
                            LEFT JOIN session_information s ON u.id = s.user_id AND s.is_deleted = 0
                            WHERE u.id = :user_id AND u.is_deleted = 0
                            ORDER BY s.session_id DESC
                            LIMIT 1
                            """
                        ),
                        {"user_id": data["user_id"]},
                    ).mappings().all()
                elif "user_name" in data and "email" in data:
                    # 同上联表意图；WHERE 用 OR 同时匹配用户名或邮箱
                    rows = safe_execute(session,
                        text(
                            """
                            SELECT u.id AS id, u.user_name AS user_name,
                                   s.session_id AS session_id
                            FROM user_information u
                            LEFT JOIN session_information s ON u.id = s.user_id AND s.is_deleted = 0
                            WHERE (u.user_name = :user_name OR u.email = :email) AND u.is_deleted = 0
                            ORDER BY s.session_id DESC
                            LIMIT 1
                            """
                        ),
                        {"user_name": data["user_name"], "email": data["email"]},
                    ).mappings().all()
                else:
                    return []
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error reading user information: %s", e)
            return []

    def check_information(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """检查用户名或邮箱是否已被注册（SELECT id/user_name/email）。

        功能：在未软删除账号中按 user_name 或 email 任一命中即返回冲突行，
        供注册流程做唯一性预检。
        SQL 安全：user_name/email 为命名绑定参数（OR 条件在 SQL 内固定，
        不拼接外部输入）；is_deleted = 0 过滤；经 safe_execute 执行，
        返回行数还受 SQL_MAX_ROWS 钳制。

        被谁调用：util/user.py 的 UserInformation.check_user()
        （文件.函数：user.UserInformation.check_user），注册接口据此返回
        “用户名/邮箱已存在”。
        参数：
            data: {"user_name": 注册表单用户名, "email": 注册表单邮箱}。
        返回：List[Dict[str, Any]]，命中的账号行列表（非空即表示已注册）；
              无冲突返回 []，异常时也返回 []（注册侧按保守策略处理）。
        异常：捕获全部 Exception，记日志后返回 []。
        """
        try:
            with session_scope() as session:
                rows = safe_execute(session,
                    text(
                        """
                        SELECT id, user_name, email
                        FROM user_information
                        WHERE (user_name = :user_name OR email = :email) AND is_deleted = 0
                        """
                    ),
                    {"user_name": data["user_name"], "email": data["email"]},
                ).mappings().all()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error checking user: %s", e)
            return []

    def get_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """按主键查询单个用户的鉴权信息（SELECT id/user_name/email/role/token_version）。

        功能：每个请求的登录态解析都依赖本方法实时回库，带 is_deleted = 0
        过滤——用户被软注销后，其未过期 token 的下一个请求即查不到行而 401。
        SQL 安全：user_id 命名绑定参数 + 软删除过滤，经 safe_execute 执行。

        被谁调用：
        - core/deps.py 的 get_current_user()（文件.函数：deps.get_current_user）；
        - util/user.py 的 UserInformation.user_information()；
        - control/admin_control.py 多个管理端点（用户操作前确认目标存在）；
        - control/login_control.py 登录态信息组装。
        参数：
            user_id: 用户 ID，来源 JWT 解析结果/登录态/管理端请求目标。
        返回：Optional[Dict[str, Any]]。命中返回含 id/user_name/email/role/
              token_version 的字典（交给鉴权依赖比对 token 版本与角色）；
              不存在返回 None；异常记日志后也返回 None（fail-closed，鉴权拒绝）。
        异常：捕获全部 Exception，记日志后返回 None。
        """
        try:
            with session_scope() as session:
                row = safe_execute(session,
                    text(
                        "SELECT id, user_name, email, role, token_version FROM user_information WHERE id = :user_id AND is_deleted = 0"
                    ),
                    {"user_id": user_id},
                ).mappings().first()
            return dict(row) if row else None
        except Exception as e:
            logger.error("Error get user by id: %s", e)
            return None

    def get_by_ids(self, user_ids) -> Dict[int, Dict[str, Any]]:
        """批量按 id 查询用户基础信息，返回 {user_id: {id, user_name, email, role}}。

        功能：一条 IN 查询取回多个用户的展示/鉴权信息，避免管理端列表 N+1 查询；
        只取未软删除账号。
        SQL 安全：id 列表先在 Python 侧 int() 强转（非数字直接抛错被上层感知），
        再以 tuple 形式绑定到 IN :ids（SQLAlchemy 自动展开为占位符列表），
        不做字符串拼接；经 safe_execute 执行。
        被谁调用：
        - core/deps.py 的 get_current_user()（文件.函数：deps.get_current_user，
          单元素批量查询，异常返回空 → 鉴权 fail-closed）；
        - control/admin_control.py 用量统计后批量补用户信息。
        参数：
            user_ids: 可迭代的用户 ID 集合（int 或可转 int 的值），来源
                      JWT/管理端聚合出的用户 id 列表；空/None 直接返回 {}。
        返回：Dict[int, Dict[str, Any]]，键为 int 型 user_id，值为
              {id, user_name, email, role}；查不到的 id 不在字典中；异常返回 {}。
        异常：捕获查询阶段 Exception，记日志后返回 {}；int() 转换异常属于
              调用方传参错误，不在此吞掉。
        """
        ids = [int(i) for i in (user_ids or [])]
        if not ids:
            return {}
        try:
            with session_scope() as session:
                rows = safe_execute(session,
                    text(
                        "SELECT id, user_name, email, role FROM user_information WHERE id IN :ids AND is_deleted = 0"
                    ),
                    {"ids": tuple(ids)},
                ).mappings().all()
            return {int(row["id"]): dict(row) for row in rows}
        except Exception as e:
            logger.error("Error get users by ids: %s", e)
            return {}

    def get_all_users(self) -> List[Dict[str, Any]]:
        """管理端用户列表：全部未注销账号基础信息（不含密码哈希），按 id 升序。

        功能：SELECT id/user_name/email/role，固定不带 user_pwd，避免密码哈希
        进入管理端响应；is_deleted = 0 排除已注销账号；ORDER BY id ASC 稳定排序。
        SQL 安全：无外部入参，无注入面；经 safe_execute 执行，返回行数由
        sql_guard 钳制到 SQL_MAX_ROWS（默认 10000），防止账号总量异常时全表拉爆。
        被谁调用：service/admin_user_service.py 的用户列表函数
        （文件.函数：admin_user_service.list_users，内部
        Information_Read().get_all_users()）。
        参数：无。
        返回：List[Dict[str, Any]]，每项 {id, user_name, email, role}，
              交给 service/control 组装管理端用户列表；异常返回 []。
        异常：捕获全部 Exception，记日志后返回 []。
        """
        try:
            with session_scope() as session:
                rows = safe_execute(session,
                    text(
                        "SELECT id, user_name, email, role FROM user_information WHERE is_deleted = 0 ORDER BY id ASC"
                    ),
                ).mappings().all()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Error get all users: %s", e)
            return []

    def get_by_username(self, user_name: str) -> Optional[Dict[str, Any]]:
        """账号密码登录用：按用户名查询账号（SELECT，含密码哈希 user_pwd）。

        功能：供登录校验取回凭据与角色；查询列包含 user_pwd（bcrypt 哈希或
        历史明文），仅用于服务端密码校验，不得原样返回前端。
        SQL 安全：user_name 命名绑定参数；is_deleted = 0 软删除过滤；
        经 safe_execute 执行。
        被谁调用：util/user.py 的 UserInformation.login_by_username()
        （文件.函数：user.UserInformation.login_by_username）；测试夹具亦用。
        参数：
            user_name: 登录表单用户名。
        返回：Optional[Dict[str, Any]]。命中返回
              {id, user_name, user_pwd, email, role}，交给 UserInformation
              做 bcrypt/历史明文校验并签发 JWT；不存在返回 None；异常返回 None。
        异常：捕获全部 Exception，记日志后返回 None。
        """
        try:
            with session_scope() as session:
                row = safe_execute(session,
                    text(
                        """
                        SELECT id, user_name, user_pwd, email, role
                        FROM user_information WHERE user_name = :user_name AND is_deleted = 0
                        """
                    ),
                    {"user_name": user_name},
                ).mappings().first()
            return dict(row) if row else None
        except Exception as e:
            logger.error("Error get user by username: %s", e)
            return None

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """邮箱验证码登录用：按邮箱查询账号（SELECT，含密码哈希列）。

        功能：邮箱 + 一次性验证码校验通过后，用本方法取回账号以签发 JWT；
        同样带 is_deleted = 0，已注销邮箱无法登录。
        SQL 安全：email 命名绑定参数；经 safe_execute 执行。
        被谁调用：util/user.py 的 UserInformation.login_by_email_code()
        （文件.函数：user.UserInformation.login_by_email_code）。
        参数：
            email: 登录表单邮箱（验证码已在 service 层校验通过）。
        返回：Optional[Dict[str, Any]]。命中返回
              {id, user_name, user_pwd, email, role}（本路径不校验密码，
              user_pwd 仅随行带出）；未注册返回 None；异常返回 None。
        异常：捕获全部 Exception，记日志后返回 None。
        """
        try:
            with session_scope() as session:
                row = safe_execute(session,
                    text(
                        """
                        SELECT id, user_name, user_pwd, email, role
                        FROM user_information WHERE email = :email AND is_deleted = 0
                        """
                    ),
                    {"email": email},
                ).mappings().first()
            return dict(row) if row else None
        except Exception as e:
            logger.error("Error get user by email: %s", e)
            return None

    def upgrade_password(self, user_id: int, new_hashed_password: str) -> bool:
        """历史明文密码登录成功后，将该用户口令升级为 bcrypt 哈希（UPDATE 单列）。

        功能：兼容旧数据——登录时若发现 user_pwd 不是 bcrypt 形态（不以 $2 开头）
        且明文比对成功，随即用新 bcrypt 哈希覆盖，完成一次性无感升级。
        SQL 安全：user_id 与新哈希均为命名绑定参数，经 safe_execute 执行；
        注意 UPDATE 语句不追加 is_deleted 条件（行存在即升级，调用方来自成功
        登录路径，账号必然有效），不改变其他列。
        被谁调用：util/user.py 的 UserInformation.login_by_username()
        （文件.函数：user.UserInformation.login_by_username，明文校验成功分支）。
        参数：
            user_id: 用户 ID，来源登录查询命中的账号行。
            new_hashed_password: service 层用 hash_password() 生成的 bcrypt 哈希，
                                 DAO 不接触明文密码。
        返回：bool。True 升级成功并提交；False 表示异常（记日志，不阻断登录，
              下次登录会再次尝试升级）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text("UPDATE user_information SET user_pwd = :pwd WHERE id = :user_id"),
                    {"pwd": new_hashed_password, "user_id": user_id},
                )
            logger.info("Password upgraded to bcrypt for user_id=%s", user_id)
            return True
        except Exception as e:
            logger.error("Error upgrading password: %s", e)
            return False

    def save_information(self, data: Dict[str, Any]) -> None:
        """抽象方法的空实现：只读 DAO 不承担任何写入。

        功能：BaseInformation 要求 save_information 接口，而本类职责为只读
        （写入由 dao/user.py 的 Information 承担），故以 no-op 满足契约，
        被误调用时不产生任何数据库操作。
        参数：
            data: 任意字段字典（忽略不处理）。
        返回：None。
        """
        pass
