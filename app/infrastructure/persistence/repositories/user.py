"""
模块名：app.infrastructure.persistence.repositories.user

作用：
    用户账号写入与账号生命周期操作，操作 MySQL 表 user_information
    （字段：id / user_name / user_pwd / email / role / token_version /
    is_deleted / deleted_at 等）。
    提供注册插入、角色调整、token 版本自增（单点互踢）、账号注销（软删 +
    注销计划）。

主要成员：
    - Information：继承 BaseInformation，方法包括 save_information /
      update_role / increment_token_version / deactivate_user。
    注意：查询类操作在 dao/read.py 的 Information_Read；软删除 SQL 在
    dao/soft_delete.py，本模块通过函数内延迟导入调用以避免循环依赖。

被谁使用：
    - util/user.py 的 UserInformation.__init__ 实例化
      （self.information = Information()）：注册、登录后 token_version 自增；
    - service/admin_user_service.py 管理端角色调整与注销（Information() 临时实例化）；
    - tests/ 夹具与安全/并发/RBAC 测试直接实例化。

注意：写入前密码必须已由 service 层完成 bcrypt 哈希，DAO 不接触明文密码逻辑。
"""
from .base_information import BaseInformation
from app.infrastructure.persistence.session import session_scope
from sqlalchemy import text
from core.sql_guard import safe_execute
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)


class Information(BaseInformation):
    """用户账号写入数据访问层，对应 MySQL 表 user_information。

    承担账号行的新增（save_information）、角色修改（update_role）、
    token 版本自增（increment_token_version）、注销编排（deactivate_user）。
    实例化位置：util/user.py 的 UserInformation.__init__（self.information）、
    service/admin_user_service.py 的 update_role/deactivate_user、
    tests 测试夹具与安全用例。__init__ 无形参，仅调用父类 ABC 构造；
    不持有数据库连接，会话在各方法内通过 session_scope() 获取。
    """

    def __init__(self):
        super().__init__()

    def save_information(self, data: Dict[str, Any]):
        """注册新用户（INSERT INTO user_information，单行插入）。

        功能：写入用户名/密码哈希/邮箱；role、token_version 等取数据库默认值。
        SQL 安全：user_name/user_pwd/email 均为命名绑定参数，经
        core.sql_guard.safe_execute 执行，杜绝 SQL 注入；唯一键冲突
        （用户名/邮箱重复）会抛异常被本方法捕获并返回 "false"。
        安全约定：data["user_pwd"] 必须已是 service 层 bcrypt 哈希结果，
        DAO 不接触明文密码。
        被谁调用：util/user.py 的 UserInformation.register_user()
        （文件.函数：user.UserInformation.register_user）；
        tests 夹具创建账号时亦调用。
        参数：
            data: {"user_name": 注册用户名, "user_pwd": bcrypt 哈希,
                   "email": 注册邮箱}，由 UserInformation.register_user 组装。
        返回：str。"success" 插入成功并提交；"false" 发生异常
              （含唯一键冲突，记 error 日志，不向上抛出；服务层据此提示
              “用户名或邮箱可能已存在”）。
        异常：捕获全部 Exception，记日志后返回 "false"。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text(
                        """
                        INSERT INTO user_information(user_name, user_pwd, email)
                        VALUES (:user_name, :user_pwd, :email)
                        """
                    ),
                    {
                        "user_name": data["user_name"],
                        "user_pwd": data["user_pwd"],
                        "email": data["email"],
                    },
                )
            logger.info("User saved successfully: user_name=%s", data.get("user_name"))
            return "success"
        except Exception as e:
            logger.error("Error saving user: %s", e)
            return "false"

    def update_role(self, user_id: int, role: str) -> bool:
        """管理员调整用户角色（UPDATE user_information SET role，仅改 role 列）。

        角色即时生效：get_current_user 每请求实时回库以库内 role 为准，
        该用户未过期 token 的下一个请求即按新角色鉴权。
        SQL 安全：role 与 uid 均为命名绑定参数（role 取值合法性由
        control/service 层白名单约束），经 safe_execute 执行。
        被谁调用：service/admin_user_service.py 的 update_role()
        （文件.函数：admin_user_service.update_role），上层为
        control/admin_control.py 的角色调整端点。
        参数：
            user_id: 目标用户 ID，来源管理端请求（经管理员权限与二次确认校验）。
            role: 新角色，当前业务取值 user/teacher（admin 角色由其他途径授予）。
        返回：bool。True 命中并更新（rowcount>0）；目标不存在或异常返回 False
              （异常记日志）。
        异常：捕获全部 Exception，记日志后返回 False。
        """
        try:
            with session_scope() as session:
                r = safe_execute(session,
                    text("UPDATE user_information SET role = :role WHERE id = :uid"),
                    {"role": role, "uid": user_id},
                )
            ok = r.rowcount > 0
            if ok:
                logger.info("User role updated: user_id=%s role=%s", user_id, role)
            return ok
        except Exception as e:
            logger.error("Error updating role for user %s: %s", user_id, e)
            return False

    def increment_token_version(self, user_id: int) -> int:
        """单点互踢：登录成功后把 token_version 自增 1（UPDATE + SELECT 同事务取新值）。

        语义：JWT 内携带签发时的版本号，get_current_user 回库比对——自增后
        该用户所有旧 token 立即失效（强制单点登录）。
        必须在密码/验证码校验通过后调用，避免攻击者借失败登录使受害者 token 失效。
        SQL 安全：uid 命名绑定参数；自增在数据库侧 token_version = token_version + 1
        完成（非读改写，避免并发丢更新），经 safe_execute 执行。
        被谁调用：util/user.py 的 UserInformation.login_by_username() 与
        login_by_email_code()（文件.函数：user.UserInformation.login_by_username、
        login_by_email_code，校验通过、签发 JWT 前调用）；tests 安全/并发用例。
        参数：
            user_id: 登录用户 ID，来源凭据/验证码校验命中的账号行。
        返回：int。正常返回自增后的新版本号，交给 create_access_token 写入 JWT；
              异常时返回 0（fail-open：版本刷新失败不阻断登录，新 token 按 0 处理，
              由鉴权侧兼容；详见调用方逻辑）并记 error 日志。
        异常：捕获全部 Exception，记日志后返回 0。
        """
        try:
            with session_scope() as session:
                safe_execute(session,
                    text("UPDATE user_information SET token_version = token_version + 1 WHERE id = :uid"),
                    {"uid": user_id},
                )
                row = safe_execute(session,
                    text("SELECT token_version FROM user_information WHERE id = :uid"),
                    {"uid": user_id},
                ).mappings().first()
            new_ver = int(row["token_version"]) if row else 0
            logger.info("Token version bumped: user_id=%s new_ver=%s", user_id, new_ver)
            return new_ver
        except Exception as e:
            logger.error("Error incrementing token_version for user %s: %s", user_id, e)
            return 0

    def deactivate_user(self, user_id: int) -> bool:
        """注销用户：软删除全部业务数据 + 写入注销计划表（宽限期到期后硬删除）。

        编排（本方法不含 SQL，跨两个模块协作）：
        1) soft_delete_user(user_id)：同事务软删 user_information/user_profile
           并级联软删其 history_information/session_information；
        2) schedule_account_deletion(user_id, grace_days)：登记硬删除时间，
           天数取 settings.ACCOUNT_DELETION_GRACE_DAYS（默认 7）。
        仅当软删成功才登记计划；计划登记结果不影响返回（尽力登记，到期清理由
        core/purge_scheduler.py 兜底扫描）。
        依赖 get_current_user 的实时回库 is_deleted 校验，注销后该用户未过期的
        token 立即失效（下一个请求即 401）。
        被谁调用：service/admin_user_service.py 的 deactivate_user()
        （文件.函数：admin_user_service.deactivate_user），上层为
        control/admin_control.py 的注销确认端点（二次确认令牌校验后）。
        参数：
            user_id: 待注销用户 ID，来源管理端请求（非当前登录用户自删）。
        返回：bool。True 软删除成功（注销计划已尽力登记）；软删失败返回 False。
        """
        # 函数内延迟导入，避免 dao.user ↔ dao.soft_delete 的模块级循环导入
        from app.infrastructure.persistence.repositories.soft_delete import soft_delete_user, schedule_account_deletion
        from core.config import settings

        ok = soft_delete_user(user_id)
        if not ok:
            return False
        schedule_account_deletion(user_id, grace_days=settings.ACCOUNT_DELETION_GRACE_DAYS)
        logger.info("User deactivated (soft-delete + scheduled): user_id=%s", user_id)
        return True
