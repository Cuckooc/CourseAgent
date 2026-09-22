"""
模块名：core.mailer

作用：
    邮件发送工具。基于 Python 标准库 smtplib，无需额外依赖；当前用于邮箱验证码登录。
    SMTP 未配置（smtp_host 留空）时不尝试连接，由调用方降级为日志输出验证码（仅开发环境）。

    配置（env/config.env，均见 core.config.Settings）：
        smtp_host       SMTP 服务器地址，留空则降级为日志输出（仅开发环境）
        smtp_port       端口，SSL 一般 465，STARTTLS 一般 587
        smtp_user       发件邮箱账号
        smtp_password   发件邮箱授权码（非登录密码，密钥类字段，禁止入库）
        smtp_use_ssl    true=SSL(465)，false=STARTTLS(587)
        smtp_sender_name 发件人显示名称

主要成员：
    - send_email(to_addr, subject, body_html)：发送 HTML 邮件，返回 (是否成功, 消息)；
    - is_configured()：判断 SMTP 三件套（host/user/password）是否齐全；
    - build_verification_code_email(code)：构造登录验证码邮件的主题与 HTML 正文。

被谁使用：
    - util/user.py 的邮箱验证码发送流程：先 is_configured 判断，再
      build_verification_code_email 构造内容、send_email 发出（未配置时走日志降级）。
"""
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from typing import Tuple

from core.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """SMTP 是否已配置（host/user/password 均非空）。

    功能：作为邮件功能的开关判断，未配置时调用方降级（开发环境把验证码打到日志）。
    被谁调用：util/user.py 发送验证码前；本模块 send_email 内部也再次校验。
    参数：无。
    返回：bool：True=可连接 SMTP 发信；False=未配置（settings 对应字段为空）。
    """
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def send_email(to_addr: str, subject: str, body_html: str) -> Tuple[bool, str]:
    """
    发送 HTML 邮件。

    功能：组装 MIME 邮件，按 settings.SMTP_USE_SSL 选择 SMTP_SSL(465) 直连或
    SMTP+STARTTLS(587) 升级，登录后发送；连接/认证超时 15 秒。
    被谁调用：util/user.py 的邮箱验证码发送流程。

    参数：
        to_addr: 收件人邮箱，来源为 HTTP 请求（SendCodeRequest/LoginByEmailRequest 的 email）；
        subject: 邮件主题，来源为 build_verification_code_email 返回值；
        body_html: HTML 正文，来源为 build_verification_code_email 返回值（内含验证码）。
    返回：
        Tuple[bool, str]：(success, message)。成功为 (True, "ok")；
        未配置 SMTP 时返回 (False, "smtp not configured")，由调用方决定降级策略；
        认证失败返回「邮箱账号或授权码错误」，其他 SMTP/网络异常返回对应中文错误信息
        （去向：登录控制层转成对用户友好的提示）。
    """
    # SMTP 降级：未配置 host/user/password 时不做任何连接尝试，交由调用方走日志验证码兜底
    if not is_configured():
        return False, "smtp not configured"

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((settings.SMTP_SENDER_NAME, settings.SMTP_USER))
    msg["To"] = to_addr

    try:
        if settings.SMTP_USE_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=context, timeout=15) as s:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                s.sendmail(settings.SMTP_USER, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                s.sendmail(settings.SMTP_USER, [to_addr], msg.as_string())
        logger.info("Email sent to %s, subject=%s", to_addr, subject)
        return True, "ok"
    except smtplib.SMTPAuthenticationError:
        logger.warning("SMTP auth failed: user=%s", settings.SMTP_USER)
        return False, "邮箱账号或授权码错误"
    except smtplib.SMTPException as e:
        logger.warning("SMTP send failed to %s: %s", to_addr, e)
        return False, f"邮件发送失败：{e}"
    except Exception as e:
        logger.warning("Email send exception to %s: %s", to_addr, e)
        return False, f"邮件发送异常：{e}"


def build_verification_code_email(code: str) -> Tuple[str, str]:
    """构造验证码邮件的主题与 HTML 正文。

    功能：生成固定品牌样式的登录验证码邮件模板，正文明示验证码 5 分钟内有效。
    被谁调用：util/user.py 的发送验证码流程，产物直接传给 send_email。
    参数：
        code: 6 位验证码字符串，来源为 util/user.py 生成并存入 Redis 的验证码。
    返回：
        Tuple[str, str]：(subject 邮件主题, body HTML 正文)。
    """
    subject = "【智能课程咨询】登录验证码"
    body = f"""
    <div style="font-family: 'Microsoft YaHei', Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #333;">
        <h2 style="color: #1677ff; margin-bottom: 16px;">智能课程咨询服务</h2>
        <p>您好，</p>
        <p>您正在使用邮箱验证码登录，验证码如下：</p>
        <div style="background: #f5f5f5; border-radius: 8px; padding: 16px; text-align: center; margin: 16px 0;">
            <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #1677ff;">{code}</span>
        </div>
        <p>验证码 <strong>5 分钟内有效</strong>，请勿泄露给他人。</p>
        <p>如非本人操作，请忽略此邮件。</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;" />
        <p style="color: #999; font-size: 12px;">此邮件由系统自动发送，请勿直接回复。</p>
    </div>
    """
    return subject, body
