"""
模块名：user.py（用户身份与认证业务编排，service 层门面）

作用：
    UserInformation 是登录/注册能力对 control 层暴露的门面
    （facade/编排），自身不写 SQL，按业务流程组合下层组件：
- dao/read.Information_Read：用户信息只读查询（查重/按名/按邮箱/按 ID）；
- dao/user.Information：用户信息写入（注册入库、token 版本自增、密码升级）；
- core/security：bcrypt 密码哈希/校验、JWT 访问令牌签发；
- core/verification：邮箱一次性验证码生成与校验
  （60 秒发送冷却、5 分钟有效）；
- core/mailer：SMTP 验证码邮件真实发送（未配置时降级日志输出）。

    业务覆盖：注册（密码 bcrypt 哈希后入库）、账号密码登录
    （bcrypt 校验，兼容历史明文密码并在登录成功后自动升级哈希）、
    邮箱验证码登录（一次性验证码）、登录成功统一签发 JWT 并做
    token 版本单点互踢；另保留聊天路径只读解析用户身份的
    user_information 方法。

主要成员：
    - UserInformation：唯一业务类，方法见类 docstring。

被谁使用：
    - control/login_control.py：注册 /login/register、账号登录
      /login/account、发码 /login/email/code、邮箱登录 /login/email
      四个接口内实例化并调用（文件.函数：
      login_control.register / login_by_account / send_email_code /
      login_by_email）；control 层另外负责审计日志（audit）与会话
      列表装配，本模块只返回业务结果字典。
"""
from dao.read import Information_Read
from dao.user import Information
from core import verification
from core.security import create_access_token, hash_password, verify_password
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)


class UserInformation:
    """用户身份与认证业务的门面/编排类（登录、注册、验证码）。

    类作用：对 control 层提供单一入口，编排 dao（Information_Read /
    Information）与 core（security / verification / mailer）完成认证
    流程；类内无跨请求可变状态，每个登录/注册请求现用现实例化。

    实例化位置：control/login_control.py 的 register()（约 L123）、
    login_by_account()（约 L158）、send_email_code()（约 L187，
    一次性内联实例）、login_by_email()（约 L227）。

    关键属性去向：
    - self.information_read：dao.read.Information_Read 实例，承担
      注册查重、按用户名/邮箱/ID 查询、历史明文密码升级；
    - self.information：dao.user.Information 实例，承担注册写入与
      token_version 自增（单点互踢）。
    """

    def __init__(self):
        """无参构造：组装只读 DAO 与写入 DAO 两个成员，无外部形参。"""
        # 只读查询通道：注册查重、登录取用户、聊天身份解析、密码升级
        self.information_read = Information_Read()
        # 写入通道：注册入库、token 版本自增
        self.information = Information()

    # ---------------- 聊天路径：只读解析用户身份 ----------------
    def user_information(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """供聊天流程使用：根据已认证的 user_id 只读解析用户身份。

        功能：仅做身份信息解析，不再自动创建用户；user_id 缺失时
        回落匿名身份（id=0，user_name 默认 "anonymous"）。
        被谁调用：聊天路径的历史保留方法（当前全仓 Python 代码无
        直接调用方，现链路以 JWT 注入身份为主；dao/read.py 文档中
        亦注明该变化）。
        参数：
        - data (Dict[str, Any])：来源为聊天请求上下文（user_id /
          user_name 来自登录态，session_id 来自会话），session_id
          缺省取 0。
        返回：
        - Dict[str, Any]：{user_id, user_name, session_id}，库中查到
          用户时 user_name 以数据库为准；查不到时沿用入参 user_name。
        """
        user_id = data.get("user_id")
        session_id = data.get("session_id", 0) or 0
        if not user_id:
            return {
                "user_id": 0,
                "user_name": data.get("user_name", "anonymous"),
                "session_id": session_id,
            }
        user = self.information_read.get_by_id(user_id)
        if user:
            return {
                "user_id": user["id"],
                "user_name": user["user_name"],
                "session_id": session_id,
            }
        return {"user_id": user_id, "user_name": data.get("user_name", ""), "session_id": session_id}

    # ---------------- 注册 ----------------
    def check_user(self, data: Dict[str, Any]) -> bool:
        """注册前查重：用户名或邮箱是否已被注册。

        被谁调用：control/login_control.py 的 register()
        （文件.函数：login_control.register），查重命中时直接返回
        “用户名或邮箱已被注册”，不再进入 register_user。
        参数：
        - data (Dict[str, Any])：前端注册表单 RegisterRequest 的
          model_dump()，含 user_name / user_pwd / email。
        返回：
        - bool：True 表示已存在同名用户或同邮箱（应拒绝注册）；
          False 表示可注册。
        """
        result = self.information_read.check_information(data)
        return bool(result)

    def register_user(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """注册：密码 bcrypt 哈希后组装 payload 写入用户表。

        被谁调用：control/login_control.py 的 register()，前置已通过
        check_user 查重（文件.函数：login_control.register）；成功后
        由 control 层写 user_registered 审计日志。
        参数：
        - data (Dict[str, Any])：前端注册表单（user_name 用户名、
          user_pwd 明文密码、email 邮箱），明文密码不出本方法：
          入库前经 core.security.hash_password 做 bcrypt 哈希。
        返回：
        - Dict[str, Any]：{status, message}。DAO 返回 "success" 时
          为成功并记录 info 日志；用户名/邮箱冲突或任意异常均返回
          fail 结构（异常记 error 日志，不向 control 扩散）。
        """
        try:
            payload = {
                "user_name": data["user_name"],
                "user_pwd": hash_password(data["user_pwd"]),
                "email": data["email"],
            }
            save_result = self.information.save_information(payload)
            if save_result == "success":
                logger.info("User registered: user_name=%s", data.get("user_name"))
                return {"status": "success", "message": "注册成功"}
            return {"status": "fail", "message": "注册失败，用户名或邮箱可能已存在"}
        except Exception as e:
            logger.error("Error registering user: %s", e)
            return {"status": "fail", "message": "注册失败"}

    # ---------------- 账号密码登录 ----------------
    def login_by_username(self, username: str, password: str) -> Dict[str, Any]:
        """账号密码登录：校验通过后自增 token 版本并签发 JWT。

        被谁调用：control/login_control.py 的 login_by_account()
        POST /login/account（文件.函数：login_control.login_by_account）；
        成功结果回到 control 后追加 sessions 会话列表，失败由
        control 做失败计数/锁定与 login_failed 审计。
        参数：
        - username (str)：前端登录表单 LoginByUsernameRequest.username；
        - password (str)：前端登录表单明文密码，仅用于 bcrypt/明文
          比对，不写日志、不回传。
        返回：
        - Dict[str, Any]：失败（用户不存在/密码错）统一返回相同提示
          “用户名或密码错误”，避免账号枚举；成功返回 access_token
          （JWT bearer）及 user_id/user_name/email/role。
        安全机制：
        - 存量哈希以 $2 开头时走 bcrypt 校验；历史明文密码比对
          成功后自动升级为 bcrypt（升级失败仅 warning，不影响登录）；
        - 校验通过后 increment_token_version 单点互踢：版本号 +1
          使该用户所有旧 JWT 立即失效，新令牌携带新版本号 ver。
        """
        user = self.information_read.get_by_username(username)
        if not user:
            # 用户不存在也返回相同提示，避免账号枚举
            return {"status": "fail", "message": "用户名或密码错误"}

        stored_pwd = user.get("user_pwd") or ""
        is_valid = False

        if stored_pwd.startswith("$2"):
            # bcrypt 哈希密码
            is_valid = verify_password(password, stored_pwd)
        else:
            # 兼容历史明文密码：校验成功后自动升级为 bcrypt
            if stored_pwd == password:
                is_valid = True
                try:
                    self.information_read.upgrade_password(user["id"], hash_password(password))
                except Exception as e:
                    logger.warning("Password auto-upgrade failed for user_id=%s: %s", user["id"], e)

        if not is_valid:
            return {"status": "fail", "message": "用户名或密码错误"}

        # 单点互踢：密码校验通过后 +1，使该用户所有旧 token 立即失效
        new_ver = self.information.increment_token_version(user["id"])
        token = create_access_token(user["id"], user["user_name"], user.get("role") or "user", ver=new_ver)
        logger.info("User login success: user_id=%s", user["id"])
        return {
            "status": "success",
            "access_token": token,
            "token_type": "bearer",
            "user_id": user["id"],
            "user_name": user["user_name"],
            "email": user.get("email", ""),
            "role": user.get("role") or "user",
        }

    # ---------------- 邮箱验证码登录 ----------------
    def send_email_code(self, email: str) -> Dict[str, Any]:
        """发送邮箱登录一次性验证码（60s 冷却 / 5min 有效）。

        功能：先做邮箱格式粗校验，再经 core.verification 生成验证码
        （带 60 秒发送冷却）；core.mailer 已配置 SMTP 时真实发信，
        未配置时降级把验证码打到服务日志（仅开发环境）。SMTP 发送
        失败会立即作废刚生成的码，避免用户在冷却期内无法重发。
        被谁调用：control/login_control.py 的 send_email_code()
        POST /login/email/code（文件.函数：
        login_control.send_email_code），接口另有 rate_limit(5, 60)
        全局限流防轰炸。
        参数：
        - email (str)：前端 SendCodeRequest.email 表单邮箱。
        返回：
        - Dict[str, Any]：{status, message}，直接作为 HTTP JSON
          响应返回前端；格式错/冷却中/SMTP 失败均返回 fail 结构。
        """
        if not email or "@" not in email:
            return {"status": "fail", "message": "邮箱格式不正确"}
        code = verification.generate_code(email)
        if not code:
            return {"status": "fail", "message": "验证码发送过于频繁，请 60 秒后再试"}

        from core.mailer import is_configured, send_email, build_verification_code_email

        if is_configured():
            subject, body = build_verification_code_email(code)
            ok, msg = send_email(email, subject, body)
            if ok:
                return {"status": "success", "message": "验证码已发送，请查收邮箱"}
            # SMTP 失败：作废刚生成的验证码，避免冷却期内无法重发
            verification.verify_code(email, code)  # 校验成功即删除
            return {"status": "fail", "message": msg or "验证码发送失败，请稍后重试"}

        # 未配置 SMTP：降级为日志输出（仅开发环境）
        logger.info("[DEV] 邮箱验证码 email=%s code=%s", email, code)
        return {"status": "success", "message": "验证码已发送，请查收邮箱（开发环境见服务日志）"}

    def login_by_email_code(self, email: str, code: str) -> Dict[str, Any]:
        """邮箱 + 一次性验证码登录：校验通过后自增 token 版本并签发 JWT。

        被谁调用：control/login_control.py 的 login_by_email()
        POST /login/email（文件.函数：login_control.login_by_email）；
        成功结果回到 control 后追加 sessions 会话列表。
        参数：
        - email (str)：前端 LoginByEmailRequest.email 表单邮箱；
        - code (str)：前端表单填写的 6 位验证码，与
          send_email_code 生成并存于 core.verification 的码做一次性
          校验（成功即失效，5 分钟有效）。
        返回：
        - Dict[str, Any]：验证码错误/过期或邮箱未注册返回 fail 结构；
          成功返回 access_token（JWT bearer）及
          user_id/user_name/email/role，并先 increment_token_version
          使该用户所有旧 token 立即失效（单点互踢）。
        """
        if not verification.verify_code(email, code):
            return {"status": "fail", "message": "验证码错误或已过期"}
        user = self.information_read.get_by_email(email)
        if not user:
            return {"status": "fail", "message": "该邮箱未注册"}
        # 单点互踢：验证码校验通过后 +1，使该用户所有旧 token 立即失效
        new_ver = self.information.increment_token_version(user["id"])
        token = create_access_token(user["id"], user["user_name"], user.get("role") or "user", ver=new_ver)
        logger.info("User login by email success: user_id=%s", user["id"])
        return {
            "status": "success",
            "access_token": token,
            "token_type": "bearer",
            "user_id": user["id"],
            "user_name": user["user_name"],
            "email": user.get("email", ""),
            "role": user.get("role") or "user",
        }
