"""
模块名：app.application.files.mask_service
作用：数据脱敏服务。在知识库文本入库前，对用户个人敏感信息进行正则替换，
      核心原则：禁止用户个人信息（手机号/身份证/邮箱/银行卡）进入 LLM
      调用链路与向量库。

规则（保守优先，宁可多打码不可遗漏）：
- 手机号   1[3-9]\\d{9}             → 138****5678
- 身份证   18 位（末位可为 X/x）    → 110101********1234
- 银行卡   16-19 位连续数字          → 6222********1234
- 邮箱     name@domain              → n**@d**

注：中文姓名因正则识别准确率低、易误伤知识库语义，不做自动脱敏；
    若文本含姓名通常伴随手机号/邮箱等强标识符，已由上述规则覆盖。

主要成员：
- mask_text(text)：对文本执行四类全量脱敏（入库前统一入口）。
- has_sensitive_info(text)：检测是否命中任一敏感模式（告警/调试用）。
- _mask_phone/_mask_id_card/_mask_bank_card/_mask_email：各正则的替换函数。
- _PHONE_RE/_ID_CARD_RE/_BANK_CARD_RE/_EMAIL_RE：模块级预编译正则常量。

被谁使用：
- app/application/files/file_service.py：process_file/process_temp_file 入库前调用 mask_text；
- app/application/review/review_service.py：ReviewService.approve 用户编辑文本入库前再次脱敏；
- app/api/v1/files.py：临时文件上传后对提取文本脱敏再送偏好提取。
"""
import re

# 模块级常量：手机号——1 开头，第二位 3-9，共 11 位；前后负向断言防截断长数字
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 模块级常量：身份证——18 位，前 17 位数字，末位数字或 X/x
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
# 模块级常量：银行卡——16-19 位连续数字（须先于身份证/手机号匹配，避免被短模式破坏）
_BANK_CARD_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
# 模块级常量：邮箱——name@domain 形式
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _mask_phone(match: re.Match) -> str:
    """手机号正则替换函数：保留前 3 位与后 4 位，中间打码（138****5678）。

    被谁调用：mask_text() 作为 _PHONE_RE.sub 的替换回调。
    参数：match——手机号正则匹配对象，group(1) 为完整手机号。
    返回：str——打码后的手机号字符串。
    """
    s = match.group(1)
    return f"{s[:3]}****{s[-4:]}"


def _mask_id_card(match: re.Match) -> str:
    """身份证正则替换函数：保留前 6 位（地区码）与后 4 位，中间 8 位打码。

    被谁调用：mask_text() 作为 _ID_CARD_RE.sub 的替换回调。
    参数：match——身份证正则匹配对象，group(1) 为完整 18 位证件号。
    返回：str——形如 110101********1234 的打码字符串。
    """
    s = match.group(1)
    return f"{s[:6]}********{s[-4:]}"


def _mask_bank_card(match: re.Match) -> str:
    """银行卡正则替换函数：保留前 4 位与后 4 位，中间打码（6222********1234）。

    被谁调用：mask_text() 作为 _BANK_CARD_RE.sub 的替换回调。
    参数：match——银行卡正则匹配对象，group(1) 为 16-19 位连续卡号。
    返回：str——打码后的卡号字符串。
    """
    s = match.group(1)
    return f"{s[:4]}********{s[-4:]}"


def _mask_email(match: re.Match) -> str:
    """邮箱正则替换函数：用户名与域名各只保留首字符，其余打码（n**@d**）。

    被谁调用：mask_text() 作为 _EMAIL_RE.sub 的替换回调。
    参数：match——邮箱正则匹配对象，group(1) 为 @ 前用户名，
          group(2) 为域名部分。
    返回：str——打码后的邮箱字符串。
    """
    name, domain = match.group(1), match.group(2)
    masked_name = name[0] + "**" if len(name) > 1 else "**"
    masked_domain = domain[0] + "**" if len(domain) > 1 else "**"
    return f"{masked_name}@{masked_domain}"


def mask_text(text: str) -> str:
    """
    对文本执行全量脱敏。执行顺序：银行卡 → 身份证 → 手机号 → 邮箱。
    （先处理长数字模式，避免短模式提前匹配破坏长模式）

    功能：依次用四类预编译正则替换文本中的敏感信息，是所有文本入库/送
          LLM 前的统一脱敏入口。
    被谁调用：app/application/files/file_service.py（持久库与临时库入库前）、
              app/application/review/review_service.py（审核通过入库前）、
              app/api/v1/files.py（临时文件偏好提取前）。
    参数：text (str)——待脱敏原文（来源：上传文件提取文本或用户审核编辑文本）。
    返回：str——脱敏后的文本，去向：父子块切分 → embedding → 向量库；
          None/空串原样返回。
    """
    if not text:
        return text
    # 顺序敏感：长数字（银行卡16-19位）必须先替换，否则会被身份证/手机号规则截断
    text = _BANK_CARD_RE.sub(_mask_bank_card, text)
    text = _ID_CARD_RE.sub(_mask_id_card, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    text = _EMAIL_RE.sub(_mask_email, text)
    return text


def has_sensitive_info(text: str) -> bool:
    """检测文本是否包含敏感信息（用于日志告警/调试，不对外暴露）。

    功能：对四类正则各做一次 search，任一命中即返回 True；不做替换，
          不返回命中内容本身（避免敏感信息进入调用方日志）。
    被谁调用：当前作为工具函数预留（仓库内无业务调用点），供告警/调试场景使用。
    参数：text (str)——待检测文本。
    返回：bool——True 表示含至少一类敏感信息；空文本返回 False。
    """
    if not text:
        return False
    return bool(
        _PHONE_RE.search(text)
        or _ID_CARD_RE.search(text)
        or _BANK_CARD_RE.search(text)
        or _EMAIL_RE.search(text)
    )
