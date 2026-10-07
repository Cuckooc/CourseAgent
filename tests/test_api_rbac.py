"""
模块名：tests/test_api_rbac.py。

后端 API 深度测试套件（固化自《后端 API 深度测试报告.md》34 项断言），风险类型：
基于角色的访问控制（RBAC）矩阵缺口、失效身份认证、参数校验、注入与路由兜底。

覆盖：
1. 三角色权限矩阵（公共接口 4×3=12 + 管理接口 2×3=6 + 审核接口 1×3=3）
2. JWT 安全（无效/篡改/空/互踢 4 项）
3. 参数校验与边界（缺失必填/超长/SQL 注入/404/连续请求 5 项）

测试函数清单（本文件无测试类，全部为模块级函数）：
- 公共接口矩阵（参数化 4 端点 × 3 角色）：test_public_endpoint_user /
  _teacher / _admin
- 管理接口矩阵（参数化 2 端点 × 3 角色）：test_admin_endpoint_user_forbidden /
  _teacher_forbidden / _admin_allowed
- 审核接口：test_review_list_self_only（参数化三角色）、test_review_all_user_forbidden /
  test_review_all_teacher_allowed / test_review_all_admin_allowed /
  test_review_all_status_filter / test_review_all_invalid_status_rejected
- JWT：test_jwt_invalid_token_rejected / test_jwt_tampered_payload_rejected /
  test_jwt_missing_auth_header_rejected / test_token_version_kick
- 校验/兜底：test_register_missing_required_field / test_register_username_too_long /
  test_sql_injection_rejected / test_unknown_path_returns_404 /
  test_healthz_continuous_requests
模块常量：PUBLIC_ENDPOINTS（三角色皆可访问的 4 个公共端点）、
ADMIN_ENDPOINTS（仅 admin 的 2 个管理端点），均为参数化数据源。

被测对象来源：
- 路由守卫：app/api/v1/auth.py、profile_control.py、knowledge_control.py、
  history_control.py（公共接口）；app/api/v1/admin.py（/admin/* + require_admin）；
  app/api/v1/review.py（/review/list 自审、/review/all 审核员门槛、status 白名单）；
- JWT：core/security.py 签发、core/deps.py 校验；dao/user.py 的 increment_token_version、
  dao/read.py 的 get_by_id；
- 注册：app/api/v1/auth.py /login/register（Pydantic 校验 + 参数化 SQL）。

运行方式：
    pytest tests/test_api_rbac.py              # 需后端 :8000（pytestmark=backend）
    pytest tests/test_api_rbac.py -k admin     # 只跑管理接口矩阵
依赖夹具：conftest 的 http / user_acct / teacher_acct / admin_acct；
test_sql_injection_rejected 直连 db.session.engine 做硬删清理（UNIQUE 约束覆盖软删行）。

设计：
- 通过 conftest 三角色夹具创建临时账号 → 调用 API → 软删清理；
- 不 mock 任何层，全部走真实后端 + 真实 DB；
- 全部用例标 `backend` marker，后端不可达时自动 skip。
"""
import time

import pytest

# 模块级 marker：全部用例需后端在线
pytestmark = pytest.mark.backend

# 公共接口常量（正常用例数据源）：三角色访问均期望 200；元素为 (方法, 路径, JSON body)
PUBLIC_ENDPOINTS = [
    ("GET", "/login/me"),
    ("GET", "/profile"),
    ("GET", "/knowledge/list"),
    ("POST", "/history/list", {}),  # POST，无 body
]

# 管理接口常量（越权用例数据源）：仅 admin 期望 200，user/teacher 期望 403
ADMIN_ENDPOINTS = [
    ("GET", "/admin/users"),
    ("GET", "/admin/llm/usage"),
]


# ---------------- 公共接口权限矩阵 ----------------

# 参数化数据意图（正常用例）：4 个公共端点，分别用 user/teacher/admin 三种角色跑一遍，
# 每个角色一个测试函数，断言任意角色访问公共端点均为 200
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/login/me", None),
    ("GET", "/profile", None),
    ("GET", "/knowledge/list", None),
    ("POST", "/history/list", {}),
])
def test_public_endpoint_user(http, user_acct, method, path, body):
    """user 角色访问公共接口 → 200。"""
    status, _, _ = http(method, path, token=user_acct["token"], json_body=body)
    assert status == 200, f"user 访问 {path} 失败：{status}"


# 参数化数据意图（正常用例）：同上 4 个公共端点，换 teacher 角色
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/login/me", None),
    ("GET", "/profile", None),
    ("GET", "/knowledge/list", None),
    ("POST", "/history/list", {}),
])
def test_public_endpoint_teacher(http, teacher_acct, method, path, body):
    """teacher 角色访问公共接口 → 200。"""
    status, _, _ = http(method, path, token=teacher_acct["token"], json_body=body)
    assert status == 200, f"teacher 访问 {path} 失败：{status}"


# 参数化数据意图（正常用例）：同上 4 个公共端点，换 admin 角色
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/login/me", None),
    ("GET", "/profile", None),
    ("GET", "/knowledge/list", None),
    ("POST", "/history/list", {}),
])
def test_public_endpoint_admin(http, admin_acct, method, path, body):
    """admin 角色访问公共接口 → 200。

    注：/admin/* 走 require_admin 守卫，/profile /knowledge /history 公共接口
    对 admin 同样开放；admin 在 /chat 端点被 forbid_admin 阻止，但本组不测 /chat。
    """
    status, _, _ = http(method, path, token=admin_acct["token"], json_body=body)
    assert status == 200, f"admin 访问 {path} 失败：{status}"


# ---------------- 管理接口权限矩阵 ----------------

# 参数化数据意图（越权用例）：2 个管理端点，user 角色逐个访问，期望全部 403
@pytest.mark.parametrize("path", [p for _, p in ADMIN_ENDPOINTS])
def test_admin_endpoint_user_forbidden(http, user_acct, path):
    """user 访问管理接口 → 403。"""
    status, _, _ = http("GET", path, token=user_acct["token"])
    assert status == 403, f"user 访问 {path} 应 403，实际 {status}"


# 参数化数据意图（越权用例）：同上 2 个管理端点，teacher 角色逐个访问，期望 403
@pytest.mark.parametrize("path", [p for _, p in ADMIN_ENDPOINTS])
def test_admin_endpoint_teacher_forbidden(http, teacher_acct, path):
    """teacher 访问管理接口 → 403。"""
    status, _, _ = http("GET", path, token=teacher_acct["token"])
    assert status == 403, f"teacher 访问 {path} 应 403，实际 {status}"


# 参数化数据意图（正常用例）：同上 2 个管理端点，admin 角色逐个访问，期望 200
@pytest.mark.parametrize("path", [p for _, p in ADMIN_ENDPOINTS])
def test_admin_endpoint_admin_allowed(http, admin_acct, path):
    """admin 访问管理接口 → 200。"""
    status, _, _ = http("GET", path, token=admin_acct["token"])
    assert status == 200, f"admin 访问 {path} 应 200，实际 {status}"


# ---------------- 审核接口（设计为自审，三角色都 200） ----------------

# 参数化数据意图（正常用例）：以 fixture 名字符串驱动 request.getfixturevalue，
# 同一「自审列表」用例在 user/teacher/admin 三个角色下各跑一次
@pytest.mark.parametrize("acct_fixture", ["user_acct", "teacher_acct", "admin_acct"])
def test_review_list_self_only(http, request, acct_fixture):
    """GET /review/list 三角色均 200（按 user_id 过滤自己的审核记录）。"""
    acct = request.getfixturevalue(acct_fixture)
    status, body, _ = http("GET", "/review/list", token=acct["token"])
    assert status == 200, f"{acct_fixture} 访问 /review/list 应 200，实际 {status}"
    # 返回应是当前用户的记录列表（空也合法）
    assert body.get("status") == "success"


# ---------------- /review/all 审核员特权接口（仅 teacher/admin） ----------------

def test_review_all_user_forbidden(http, user_acct):
    """user 访问 /review/all → 403（无审核员权限）。"""
    status, _, _ = http("GET", "/review/all", token=user_acct["token"])
    assert status == 403, f"user 访问 /review/all 应 403，实际 {status}"


def test_review_all_teacher_allowed(http, teacher_acct):
    """teacher 访问 /review/all → 200（审核员角色）。"""
    status, body, _ = http("GET", "/review/all", token=teacher_acct["token"])
    assert status == 200, f"teacher 访问 /review/all 应 200，实际 {status}"
    assert body.get("status") == "success"
    # 返回所有用户的全量审核记录（空也合法）
    assert "data" in body
    assert "total" in body


def test_review_all_admin_allowed(http, admin_acct):
    """admin 访问 /review/all → 200（超管）。"""
    status, body, _ = http("GET", "/review/all", token=admin_acct["token"])
    assert status == 200, f"admin 访问 /review/all 应 200，实际 {status}"
    assert body.get("status") == "success"


def test_review_all_status_filter(http, admin_acct):
    """admin 访问 /review/all?status=pending → 200 + 仅返回 pending 记录。"""
    status, body, _ = http(
        "GET", "/review/all?status=pending", token=admin_acct["token"]
    )
    assert status == 200, f"admin status=pending 过滤应 200，实际 {status}"
    assert body.get("status") == "success"
    # 所有返回记录的 status 必须是 pending
    for r in (body.get("data") or []):
        assert r.get("status") == "pending", f"过滤失效：{r}"


def test_review_all_invalid_status_rejected(http, admin_acct):
    """非法 status 参数 → 400。"""
    status, _, _ = http(
        "GET", "/review/all?status=invalid", token=admin_acct["token"]
    )
    assert status == 400, f"非法 status 应 400，实际 {status}"


# ---------------- JWT 安全 ----------------

def test_jwt_invalid_token_rejected(http):
    """无效 JWT（Bearer invalid.token.here）→ 401。"""
    status, _, _ = http("GET", "/login/me", token="invalid.token.here")
    assert status == 401, f"无效 JWT 应 401，实际 {status}"


def test_jwt_tampered_payload_rejected(http, user_acct):
    """篡改 JWT payload → 签名校验失败 → 401。

    构造方式：把 token 中间段 base64 解码后改 sub 再编码回去，签名会失配。
    """
    import base64
    import json

    token = user_acct["token"]
    parts = token.split(".")
    assert len(parts) == 3, "JWT 应为三段式"

    # 解码 payload（中段），改 sub，重新编码（不带 padding）
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(pad))
    payload["sub"] = "99999"  # 篡改 user_id
    tampered = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    bad_token = f"{parts[0]}.{tampered}.{parts[2]}"
    status, _, _ = http("GET", "/login/me", token=bad_token)
    assert status == 401, f"篡改 JWT 应 401，实际 {status}"


def test_jwt_missing_auth_header_rejected(http):
    """空 Authorization 头 → 401。"""
    status, _, _ = http("GET", "/login/me", token=None)
    assert status == 401, f"空 Authorization 应 401，实际 {status}"


def test_token_version_kick(http, user_acct):
    """token_version 单点互踢：重新登录后旧 token 立即 401，新 token 200。"""
    from app.infrastructure.persistence.repositories.user import Information

    # 旧 token 先验证可用
    status, _, _ = http("GET", "/login/me", token=user_acct["token"])
    assert status == 200, "旧 token 初始应可用"

    # bump token_version → 旧 token 失效
    Information().increment_token_version(user_acct["user_id"])

    status_old, _, _ = http("GET", "/login/me", token=user_acct["token"])
    assert status_old == 401, f"互踢后旧 token 应 401，实际 {status_old}"

    # 用新 ver 签发新 token → 200
    from app.auth.authentication import create_access_token
    from app.infrastructure.persistence.repositories.read import Information_Read
    row = Information_Read().get_by_id(user_acct["user_id"])
    new_ver = int(row["token_version"])
    new_token = create_access_token(
        user_acct["user_id"], user_acct["user_name"], user_acct["role"], ver=new_ver
    )
    status_new, _, _ = http("GET", "/login/me", token=new_token)
    assert status_new == 200, f"新 token 应 200，实际 {status_new}"


# ---------------- 参数校验与边界 ----------------

def test_register_missing_required_field(http):
    """缺失必填字段注册 → 422（Pydantic 校验）。"""
    # 故意缺 email
    status, body, _ = http("POST", "/login/register", json_body={"user_name": "x", "user_pwd": "Test1234"})
    assert status == 422, f"缺失 email 应 422，实际 {status}"


def test_register_username_too_long(http):
    """超长 user_name（25 字符，列限 20）→ 422。"""
    status, _, _ = http(
        "POST", "/login/register",
        json_body={"user_name": "x" * 25, "user_pwd": "Test1234", "email": "long@x.com"},
    )
    assert status == 422, f"超长 user_name 应 422，实际 {status}"


def test_sql_injection_rejected(http):
    """SQL 注入防御验证：参数化查询使注入 payload 作为普通字符串字面值处理。

    正确行为：
    1. 用 `' OR 1=1 --` 作为 user_name 注册 → 注册成功（参数化绑定，无注入风险）；
    2. 再次用同 user_name 注册 → fail（参数化让 UNIQUE 约束生效，注入不能绕过）；
    3. 用注入 payload 作为密码登录 → fail（参数化绑定，注入不会绕过 bcrypt 校验）。

    关键：如果参数化查询失效，第 2/3 步可能绕过 UNIQUE/密码校验导致越权登录——
    实际全部走参数化绑定，注入字符串被当作普通字符串字面值处理。
    """
    sqli_user = "' OR 1=1 --"  # 12 字符，< VARCHAR(20)，长度合法
    sqli_pwd = "' OR '1'='1"  # 注入 payload 作密码

    # UNIQUE 约束覆盖软删行 → 必须硬删才能释放槽位
    from sqlalchemy import text as _t
    from app.infrastructure.persistence.session import engine as _engine

    def _hard_delete():
        with _engine.connect() as conn:
            conn.execute(_t("DELETE FROM user_information WHERE user_name = :n"), {"n": sqli_user})
            conn.commit()

    _hard_delete()
    try:
        # 1. 首次注册：注入字符串作为 user_name，参数化绑定 → 注册成功（无注入风险）
        s1, b1, _ = http(
            "POST", "/login/register",
            json_body={"user_name": sqli_user, "user_pwd": "Test1234", "email": "sqli1@x.com"},
        )
        assert s1 == 200, f"首次注册应 200（参数化绑定注入字符串），实际 {s1}"
        assert b1.get("status") == "success", f"首次注册应 success：{b1}"

        # 2. 重复注册同 user_name：参数化让 UNIQUE 约束生效 → fail
        s2, b2, _ = http(
            "POST", "/login/register",
            json_body={"user_name": sqli_user, "user_pwd": "Test1234", "email": "sqli2@x.com"},
        )
        assert s2 == 200, f"重复注册应 200（业务级 fail 响应），实际 {s2}"
        assert b2.get("status") == "fail", f"重复注册应 fail（UNIQUE 约束生效）：{b2}"

        # 3. 用注入 payload 作为密码登录该用户名 → fail（密码不匹配，无绕过）
        s3, b3, _ = http(
            "POST", "/login/account",
            json_body={"username": sqli_user, "password": sqli_pwd},
        )
        assert s3 == 200, f"登录应 200（业务级 fail 响应），实际 {s3}"
        assert b3.get("status") == "fail", f"注入密码登录应 fail（参数化防绕过）：{b3}"
    finally:
        _hard_delete()


def test_unknown_path_returns_404(http):
    """不存在路径 → 404。"""
    status, _, _ = http("GET", "/__nonexistent_path__")
    assert status == 404, f"未知路径应 404，实际 {status}"


def test_healthz_continuous_requests(http):
    """连续 5 次请求 /healthz → 全部 200（无崩溃/无连接耗尽）。"""
    for i in range(5):
        status, _, _ = http("GET", "/healthz")
        assert status == 200, f"第 {i+1} 次 /healthz 应 200，实际 {status}"
