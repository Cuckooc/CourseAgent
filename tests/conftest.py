"""
模块名：tests/conftest.py（pytest 全局共享引导与夹具，pytest 自动发现，无需 import）。

测试套件作用：
为 tests/ 根目录全部后端类测试套件提供统一的 sys.path 引导、HTTP 调用封装、
三角色测试账号工厂与测后清理，覆盖安全（test_security）、对抗（test_adversarial）、
并发（test_concurrency）、边界（test_boundary）、RBAC 权限矩阵（test_api_rbac）、
端到端对话（test_chat_e2e）、数据库完整性（test_db_integrity）、文件上传
（test_file_upload）等风险维度。

提供内容清单：
1. 引导：将仓库根目录加入 sys.path，使 tests/ 下用例可直接 import 顶层业务包
   （service / multi_agent / tools / dao / core / db 等）；
2. 模块级工具函数：_build_request（构造 urllib.Request）、http_call（发 HTTP 并归一化返回）；
3. 基础设施夹具：base_url / db_engine / http / require_backend；
4. 测试账号工厂夹具：_created_uids（uid 收集器）、user_acct / teacher_acct / admin_acct
   （三角色账号，直接走 DAO 创建，绕开 register/login 的 rate_limit 与失败计数，
   用 core.security.create_access_token 签带真实 ver 的 JWT）；
5. autouse 清理夹具 _cleanup_test_accounts：每个用例结束走 dao.soft_delete.soft_delete_user
   软删除本用例创建的全部账号（级联软删业务数据 + 写注销计划表）；
6. pytest_configure 注册 marker：backend / slow / db。

被测对象来源：
- HTTP 层：control/app.py 注册的全部路由（control/login_control.py、chat_control.py、
  history_control.py、file_control.py、review_control.py、knowledge_control.py、
  admin_control.py、profile_control.py），默认监听 http://localhost:8000；
- 账号链路：dao/user.py 的 Information（save_information/update_role/increment_token_version）、
  dao/read.py 的 Information_Read、core/security.py 的 hash_password/create_access_token、
  dao/soft_delete.py 的 soft_delete_user；
- 数据库：db/session.py 的 engine；限流：core/deps.py 的 reset_rate_limit_store。

运行方式：
    pytest tests/                       # 运行全部套件（后端/DB 不可达的用例自行 skip 或失败）
    pytest tests/ -m backend            # 仅需要后端 :8000 在线的用例
    pytest tests/ -m "slow and db"      # 真实 LLM/向量库/MySQL 的端到端用例（默认不跑 slow）
无需 pytest-asyncio：本仓并发用例全部使用 ThreadPoolExecutor 线程池，无 async 用例。

安全声明：所有测试账号使用 @pytest.local 域名 + 弱密码 Test1234，仅由本夹具创建，
账号名带 pu_/pt_/pa_ 前缀与毫秒时间戳，测后自动软删，不接触 env/ 配置目录。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 后端固定地址（与 control.app 启动端口一致）；可用环境变量 PBL_TEST_BASE_URL 覆盖
BASE_URL = os.getenv("PBL_TEST_BASE_URL", "http://localhost:8000")

# 测试账号统一密码常量：满足业务复杂度要求（8 位以上，含字母与数字），仅测试夹具使用
TEST_PASSWORD = "Test1234"


# ---------------- HTTP 工具 ----------------

def _build_request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> urllib.request.Request:
    """构造一次 urllib.request.Request（http_call 的底层辅助）。

    功能：统一注入 Accept: application/json；传入 token 时附加 Authorization: Bearer 头；
    传入 json_body 时序列化 JSON 并设置 Content-Type。
    调用方：仅本模块 http_call 使用。参数来源：http_call 透传的测试请求参数。
    返回去向：交给 urllib.request.urlopen 发送。
    """
    hdrs: Dict[str, str] = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, method=method, headers=hdrs)


def http_call(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    base_url: str = BASE_URL,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, Any], str]:
    """发起一次 HTTP 请求，返回 (status_code, json_dict_or_empty, raw_text)。

    网络异常（连接拒绝/超时）返回 (0, {}, "CONN_ERROR:<原因>"），用于 skip 判定。
    """
    url = path if path.startswith("http") else f"{base_url}{path}"
    req = _build_request(method, url, token=token, json_body=json_body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, {}, f"CONN_ERROR:{type(e).__name__}:{e}"

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    return status, body, raw


# ---------------- 夹具：基础设施 ----------------

@pytest.fixture(scope="session")
def base_url() -> str:
    """后端基础 URL（session 级常量夹具）。

    数据来源：模块常量 BASE_URL（环境变量 PBL_TEST_BASE_URL，默认 http://localhost:8000）。
    使用方：目前根目录用例多经 http 夹具间接使用；保留此夹具供需要显式拼接 URL
    （如手工 multipart/SSE 请求）的用例注入。
    """
    return BASE_URL


@pytest.fixture(scope="session")
def db_engine():
    """复用业务 Engine（pool_pre_ping 已开启），测试不重复建池。

    数据来源：db/session.py 的全局 engine（真实 MySQL 连接池）。
    使用方：test_db_integrity.py（全部结构/完整性断言）、test_chat_e2e.py 与
    test_file_upload.py（落库副作用校验）。仅执行 SELECT 的用例无清理；
    写入由用例自行回滚或软删。
    """
    from db.session import engine  # noqa: WPS433 (测试夹具延迟导入)
    return engine


@pytest.fixture
def http():
    """返回 http_call 函数，便于用例直接调用 http("GET", "/healthz")。

    被替身的外部依赖：无 mock，全部发真实 HTTP；网络异常被归一化为
    (0, {}, "CONN_ERROR:...") 而非抛出，供用例做后端不可达 skip 判定。
    使用方：除 test_db_integrity.py 外的根目录全部 test_* 文件。
    """
    return http_call


@pytest.fixture
def require_backend(http):
    """会话级后端可达性检查：不可达则跳过所有标记 `backend` 的用例。

    放在 autouse 之前：每个用例独立检查以避免长测试中途后端掉线仍强行运行。
    使用方式：需要硬前置的用例可在参数中显式声明本夹具；当前根目录用例主要通过
    http() 返回的状态码自行 skip，本夹具作为显式前置备选。探测目标：GET /healthz。
    """
    status, body, raw = http("GET", "/healthz")
    if status != 200:
        pytest.skip(f"后端不可达 (status={status}, raw={raw[:80]})，跳过 backend 用例")
    return BASE_URL


# ---------------- 夹具：测试账号 ----------------

def _make_test_user(role: str, suffix: str) -> Dict[str, Any]:
    """工厂函数：直接走 DAO 创建一个测试账号并返回其身份字典。

    绕开 register/login 的 rate_limit 与失败计数。
    流程：hash_password → save_information → update_role → increment_token_version
    → create_access_token（带真实 ver，避免 token_version 互踢导致 401）。

    参数来源：
    - role：角色名，取值 "user" / "teacher" / "admin"，由三个账号夹具传入；
    - suffix：毫秒时间戳字符串，用于拼出唯一账号名（调用方以 time.sleep 错开）。
    返回去向：user_acct / teacher_acct / admin_acct 夹具，字典字段被用例用于
    发 Bearer 请求（token）、查库副作用（user_id）与 DAO 操作（role/token_version）。

    user_name 列限 VARCHAR(20)，前缀 `pu_/pt_/pa_` + 13 位毫秒 = 16 字符（安全）。
    """
    from core.security import create_access_token, hash_password  # noqa: WPS433
    from dao.read import Information_Read  # noqa: WPS433
    from dao.user import Information  # noqa: WPS433

    prefix = {"user": "pu", "teacher": "pt", "admin": "pa"}[role]
    uname = f"{prefix}_{suffix}"  # ≤ 16 字符，< VARCHAR(20)
    email = f"{uname}@pytest.local"

    info = Information()
    save = info.save_information({
        "user_name": uname,
        "user_pwd": hash_password(TEST_PASSWORD),
        "email": email,
    })
    if save != "success":
        raise RuntimeError(f"创建测试账号失败：{uname} (save_result={save})")

    row = Information_Read().get_by_username(uname)
    assert row, f"账号创建后查询失败：{uname}"
    uid = int(row["id"])

    # role 默认就是 user（DB server_default），仅 teacher/admin 需提升
    if role != "user":
        ok = info.update_role(uid, role)
        assert ok, f"update_role({uid}, {role}) 失败"

    # 必须先 bump token_version 再签 token，否则旧 ver=0 与新库内 ver 不等会 401
    new_ver = info.increment_token_version(uid)
    token = create_access_token(uid, uname, role, ver=new_ver)

    return {
        "user_id": uid,
        "user_name": uname,
        "email": email,
        "password": TEST_PASSWORD,
        "role": role,
        "token": token,
        "token_version": new_ver,
    }


@pytest.fixture
def _created_uids() -> List[int]:
    """uid 收集器夹具：函数级空列表，汇集本用例经任何途径创建的账号 uid。

    数据来源：三角色夹具与部分用例（test_security/test_concurrency/test_boundary
    中注册接口成功返回 user_id 时）主动 append。
    清理去向：yield 后的 autouse 夹具 _cleanup_test_accounts 读取本列表逐个软删。
    """
    return []


@pytest.fixture
def user_acct(_created_uids) -> Dict[str, Any]:
    """普通用户账号（role=user）。

    创建来源：_make_test_user("user", 毫秒时间戳)，真实 MySQL 行 + 真实 JWT；
    返回去向：几乎全部 backend 用例（安全/对抗/并发/边界/端到端/文件上传）；
    清理：uid 登记到 _created_uids，用例结束由 autouse 夹具软删。
    """
    acct = _make_test_user("user", str(int(time.time() * 1000)))
    _created_uids.append(acct["user_id"])
    return acct


@pytest.fixture
def teacher_acct(_created_uids) -> Dict[str, Any]:
    """教师账号（role=teacher，审核员角色）。

    使用方：test_api_rbac / test_adversarial / test_concurrency 中跨角色与
    /review/* 特权接口用例。创建/清理方式同 user_acct。
    """
    time.sleep(0.01)  # 避免毫秒级 suffix 撞名
    acct = _make_test_user("teacher", str(int(time.time() * 1000)))
    _created_uids.append(acct["user_id"])
    return acct


@pytest.fixture
def admin_acct(_created_uids) -> Dict[str, Any]:
    """管理员账号（role=admin）。

    使用方：test_api_rbac / test_adversarial 的管理接口矩阵与 test_security 的
    /admin/users 信息泄漏用例。创建/清理方式同 user_acct。
    """
    time.sleep(0.02)
    acct = _make_test_user("admin", str(int(time.time() * 1000)))
    _created_uids.append(acct["user_id"])
    return acct


@pytest.fixture(autouse=True)
def _cleanup_test_accounts(_created_uids):
    """autouse 后置清理：每个用例结束后软删除所有创建的测试账号。

    yield 前：无前置动作（账号由各用例/夹具按需创建）。
    yield 后：走 dao/soft_delete.py 的 soft_delete_user（业务注销链路）：
    标记 is_deleted=1 + 级联软删业务数据 + 写注销计划表。
    get_current_user 实时回库校验 is_deleted，注销后该账号 token 立即失效。
    异常不抛出（仅打印），避免清理失败掩盖用例真实结果。
    """
    yield
    if not _created_uids:
        return
    from dao.soft_delete import soft_delete_user  # noqa: WPS433
    for uid in _created_uids:
        try:
            soft_delete_user(uid)
        except Exception as e:  # noqa: BLE001
            print(f"\n[conftest cleanup] soft_delete_user({uid}) 失败：{e}")


# ---------------- marker 注册 ----------------

def pytest_configure(config: "pytest.Config") -> None:
    config.addinivalue_line(
        "markers", "backend: 需要后端在 :8000 运行的用例（不可达时自动 skip）"
    )
    config.addinivalue_line(
        "markers", "slow: 真实 LLM/向量库调用，耗时较长（默认不跑，需 -m slow 显式启用）"
    )
    config.addinivalue_line(
        "markers", "db: 需要真实 MySQL/Redis 连通的用例"
    )
