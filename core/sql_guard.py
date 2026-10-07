"""
模块名：core.sql_guard（SQL 安全层）。

作用：
    所有 DAO 统一通过 safe_execute() 执行 SQL（替代直接 session.execute()），
    在执行前施加两类防护规则：
    - 规则一【永真条件检测】：WHERE 子句出现 1=1、TRUE、OR 1=1、''=''
      等永真/恒等条件时，判定为 SQL 注入特征并阻止执行；
    - 规则二【行数上限】：SELECT 无 LIMIT 时自动注入 SQL_MAX_ROWS，
      LIMIT 超过上限时钳制到上限，防止一次性拖取全表。

拦截后去向：
    永真条件命中 -> check_tautology 抛出 TautologyDetectedError ->
    safe_execute 原样上抛（SQL 不会执行，事务随 with session_scope 回滚）->
    经 DAO/service 冒泡到 FastAPI -> control/app.py 的兜底 Exception 处理器
    返回 HTTP 500 统一失败体并记录异常栈（攻击载荷出现在日志 WHERE 片段中）。
    行数超限不报错：静默改写 SQL（注入/钳制 LIMIT）并记录 warning 后正常执行。

配置开关（env/config.env）：
    - sql_tautology_check -> settings.SQL_TAUTOLOGY_CHECK（默认 true）；
    - sql_max_rows -> settings.SQL_MAX_ROWS（默认 10000）。
    说明：本模块不做 DROP/DELETE 等“危险关键字”黑名单；写操作范围由
    DAO 固定语句 + 绑定参数控制，永真规则负责拦截注入绕过。

主要成员：
    TautologyDetectedError（异常类）、check_tautology()、enforce_limit()、
    safe_execute()。

被谁使用（Grep safe_execute / from core.sql_guard）：
    dao 下全部数据访问模块（user/read/information/session/history/profile/
    feedback/chain_log/soft_delete/session_keyword）、app/domain/memory/session_rollover、
    core/purge_scheduler。
"""
import logging
import re
from typing import Any, Dict, Optional

from sqlalchemy import text

from core.config import settings

logger = logging.getLogger(__name__)


class TautologyDetectedError(Exception):
    """WHERE 子句包含永真条件，操作被阻止。

    异常类（无需业务代码实例化）：由 check_tautology 在命中注入特征时
    raise，safe_execute 中 except TautologyDetectedError: raise 原样透传，
    最终被 control/app.py 兜底处理器转为 500 响应。
    """


# 永真条件特征正则表：仅对 WHERE 之后的子串匹配，避免误伤 SELECT 列表等位置。
# 共同攻击场景：攻击者把用户可控输入拼成 "... WHERE name='<输入>'"，
# 输入类似  x' OR 1=1 --  使过滤条件恒成立，从而绕过身份/归属校验，
# 实现越权查询、批量导出或配合 UPDATE/DELETE 扩大影响行数。
_TAUTOLOGY_PATTERNS = [
    # 1=1：最经典的注入永真式（如 WHERE id='1' OR 1=1），命中即拦
    re.compile(r"\b1\s*=\s*1\b"),
    # TRUE 布尔常量（如 OR TRUE），与 1=1 等价的绕过写法
    re.compile(r"\bTRUE\b", re.IGNORECASE),
    # 1>0：1=1 的比较运算变体，规避对 "1=1" 字面量的简单过滤
    re.compile(r"\b1\s*>\s*0\b"),
    # 0<1：同上，反向书写的恒真比较
    re.compile(r"\b0\s*<\s*1\b"),
    # 1>=1：大于等于形式的恒真式
    re.compile(r"\b1\s*>=\s*1\b"),
    # 1<=1：小于等于形式的恒真式
    re.compile(r"\b1\s*<=\s*1\b"),
    # OR 1=1：显式拼接“或永真”，直接令原 WHERE 条件失效
    re.compile(r"\bOR\s+1\s*=\s*1\b", re.IGNORECASE),
    # OR TRUE：布尔常量版的“或永真”拼接
    re.compile(r"\bOR\s+TRUE\b", re.IGNORECASE),
    # ''=''：空字符串恒等，登录框经典注入  ' OR ''='' --  的特征片段
    re.compile(r"'\s*'\s*=\s*'\s*'"),
    # 'a'='a'：任意相同字符串自比的恒真式（' OR 'x'='x）
    re.compile(r"'\w+'\s*=\s*'\w+'"),
]


def check_tautology(sql_text):
    # type: (str) -> None
    """检测 WHERE 子句中的永真条件（规则一）。

    功能：开关关闭或 SQL 无 WHERE 时直接放行；否则截取首个 WHERE 之后的
    片段逐条匹配 _TAUTOLOGY_PATTERNS，任一命中即阻止执行。
    被谁调用：safe_execute（core.sql_guard），每条 SQL 执行前必经。
    参数：
        sql_text: 待执行的 SQL 文本，来源为 DAO 中 text(...) 固定语句
            （正常业务均为绑定参数；拼接进用户输入时才可能出现注入特征）。
    返回：None（仅做放行/抛异常的哨兵作用）。
    异常：
        TautologyDetectedError：命中永真特征时抛出，SQL 不执行并向上冒泡
        至全局 500 处理器（拦截后去向见模块 docstring）。
    """
    if not settings.SQL_TAUTOLOGY_CHECK:
        return

    upper = sql_text.upper()
    if "WHERE" not in upper:
        # 无 WHERE 的语句不存在“条件恒真绕过”，无需检测
        return

    # 只检测 WHERE 之后的部分，避免 SELECT 字段名/表注释等位置误报
    where_idx = upper.index("WHERE")
    where_clause = sql_text[where_idx:]

    for pattern in _TAUTOLOGY_PATTERNS:
        if pattern.search(where_clause):
            raise TautologyDetectedError(
                "WHERE 子句包含永真条件，操作已被阻止: {}".format(
                    where_clause[:200]
                )
            )


def enforce_limit(sql_text, max_rows=None):
    # type: (str, Optional[int]) -> str
    """为 SELECT 语句强制行数上限（规则二）。

    攻击/风险场景：缺少 LIMIT 的大表查询（无论来自疏漏还是恶意构造的
    翻页参数）可能一次性返回百万行，造成数据批量泄露、应用内存膨胀与
    数据库抖动。本函数对无 LIMIT 的语句注入上限，对超限 LIMIT 做钳制。
    被谁调用：safe_execute（core.sql_guard），仅对 SELECT 生效。
    参数：
        sql_text: SELECT SQL 文本，来源为 DAO 查询语句；
        max_rows: 行数上限；None 时取 settings.SQL_MAX_ROWS
            （sql_max_rows，默认 10000）。
    返回：str，改写后的 SQL（注入或钳制了 LIMIT）；非 SELECT、
        FOR UPDATE 锁定读、绑定参数分页等无需处理的场景原样返回。
    """
    if max_rows is None:
        max_rows = settings.SQL_MAX_ROWS

    upper = sql_text.upper().strip()
    if not upper.startswith("SELECT"):
        # 写操作/DDL 不适用行数上限，原样返回
        return sql_text

    # FOR UPDATE 锁定读：MySQL 语法要求 LIMIT 位于 FOR UPDATE 之前，
    # 且锁定读（如取号 SELECT MAX(...) FOR UPDATE）不适用行数上限，直接放行
    if re.search(r"\bFOR\s+UPDATE\s*$", sql_text, re.IGNORECASE):
        return sql_text

    # 绑定参数形式的 LIMIT（如 LIMIT :limit OFFSET :offset）：
    # 分页大小由调用方控制，视为已有上限，直接放行。
    # （修复：数字正则匹配不到 :param，导致误在 OFFSET 之后追加 LIMIT 造成 1064）
    if re.search(
        r"\bLIMIT\s+(?::\w+|%\(?\w+\)?s)\s*(?:,\s*(?::\w+|%\(?\w+\)?s)\s*)?"
        r"(?:\s+OFFSET\s+(?::\w+|%\(?\w+\)?s)\s*)?$",
        sql_text,
        re.IGNORECASE,
    ):
        return sql_text

    # 数字字面量 LIMIT：区分 "LIMIT offset, count" 与 "LIMIT count" 两种形式
    limit_match = re.search(
        r"\bLIMIT\s+(\d+)\s*(?:,\s*(\d+)\s*)?$",
        sql_text,
        re.IGNORECASE,
    )

    if limit_match:
        offset = limit_match.group(1)
        count = limit_match.group(2)
        if count is not None:
            # LIMIT offset, count 形式：实际上限是第二个数字
            actual_limit = int(count)
        else:
            # 单个数字即上限
            actual_limit = int(offset)

        if actual_limit > max_rows:
            # 超限钳制：不改写业务语义，只把返回行数压到配置上限
            if count is not None:
                new_sql = sql_text[:limit_match.start()] + " LIMIT {}, {}".format(
                    offset, max_rows
                )
            else:
                new_sql = sql_text[:limit_match.start()] + " LIMIT {}".format(
                    max_rows
                )
            logger.warning(
                "SELECT LIMIT %d 超过上限 %d，已钳制", actual_limit, max_rows
            )
            return new_sql
        return sql_text

    # 无 LIMIT：去掉结尾分号后追加上限，防止无界查询
    stripped = sql_text.rstrip().rstrip(";")
    return "{} LIMIT {}".format(stripped, max_rows)


def safe_execute(session, statement, params=None):
    # type: (Any, Any, Optional[Dict[str, Any]]) -> Any
    """统一 SQL 执行入口：永真检测 → LIMIT 强制 → 执行。

    功能：替代 DAO 中的 session.execute(text(...), params)，把两条安全
    规则收敛到一个必经入口；SELECT 被改写后重新构造成 text() 语句。
    被谁调用：dao 包全部模块、app/domain/memory/session_rollover、core/purge_scheduler
        （Grep 结果见模块 docstring）。
    参数：
        session: SQLAlchemy 会话，来源为 db.session.session_scope()；
        statement: SQLAlchemy text() 语句或等价对象，来源为各 DAO；
        params: 绑定参数字典（来源为 service/HTTP 入参透传），
            使用绑定参数本身即避免了绝大多数注入；为空表示无参数 SQL。
    返回：SQLAlchemy CursorResult，去向由各 DAO 决定（fetchall/rowcount 等）。
    异常：
        TautologyDetectedError：命中永真规则时原样上抛（见 check_tautology）；
        其他数据库异常同样原样上抛，由调用方/全局处理器处理，本层不吞异常。
    """
    sql_text = str(statement)

    # 规则一：永真条件检测，命中即抛 TautologyDetectedError
    check_tautology(sql_text)

    # 规则二：仅 SELECT 需要行数上限；改写后重新包装为 text 语句
    if sql_text.upper().strip().startswith("SELECT"):
        sql_text = enforce_limit(sql_text)
        statement = text(sql_text)

    try:
        if params:
            return session.execute(statement, params)
        return session.execute(statement)
    except TautologyDetectedError:
        # 安全异常必须透传，禁止当作普通 DB 错误吞掉或降级执行
        raise
    except Exception:
        # 其余 DB 异常原样上抛，由上层决定回滚与响应
        raise
