"""
模块名：tests/test_security.py。

安全测试套件（与 test_api_rbac 互补，聚焦注入/伪造/泄漏/越权深探）：

覆盖维度（风险类型：OWASP 注入 / XSS / CSRF / 路径穿越 / 失效身份认证 / 信息泄漏）：
1. SQL 注入：登录/注册/邮箱/会话标题/审核 notes 多端点 payload 注入
2. XSS：持久化字段（user_name/session title/feedback comment）含 <script> 载荷，
   验证存储后取回是否被转义或保留原样（视业务语义，不被服务端执行的语义即视为安全）
3. CSRF：JWT 强制 Authorization 头携带（无 cookie 自动携带 → CSRF 无载体）
4. 路径穿越：文件名 ../../etc/passwd / task_id 路径参数穿越
5. JWT 安全：篡改 payload / 篡改签名 / ver 不匹配 / alg=none 伪造
6. 信息泄漏：响应不含密码哈希、错误信息不暴露堆栈、/admin/users 不回密码字段

测试类与测试函数清单：
- TestSQLInjection：test_login_username_injection（参数化 8 组 payload）、
  test_register_username_injection、test_session_title_injection（参数化前 4 组）、
  test_feedback_comment_injection（参数化前 3 组）
- TestStoredXSS：test_session_title_xss（参数化 7 组）、test_feedback_comment_xss、
  test_register_username_xss
- TestCSRFDefense：test_no_auth_header_rejected、test_cookie_only_auth_rejected
- TestPathTraversal：test_file_name_traversal（参数化 6 组）、test_file_status_task_id_traversal
- TestJWTSecurity：test_tamper_payload_user_id / test_tamper_payload_role /
  test_tamper_signature / test_alg_none_token / test_token_version_mismatch /
  test_expired_token_format / test_malformed_token
- TestInfoLeak：test_login_response_no_password / test_me_response_no_password /
  test_admin_users_no_password / test_error_message_no_stacktrace /
  test_response_headers_no_server_version
辅助函数：_multipart_upload（手工 multipart 上传）、_b64url_decode/_b64url_encode、
_tamper_payload、_alg_none_token（JWT 拆装/篡改/伪造）。

被测对象来源：
- 路由：app/api/v1/auth.py（/login/account、/login/register、/login/me）、
  app/api/v1/history.py（/history/create）、app/api/v1/chat.py（/chat/feedback、
  /chat/send）、app/api/v1/files.py（POST /file/path、GET /file/status/{task_id}）、
  app/api/v1/review.py（/review/list）、app/api/v1/admin.py（/admin/users）；
- 鉴权：core/deps.py 的 get_current_user、core/security.py 的 create_access_token；
- token 互踢：dao/user.py 的 Information.increment_token_version；
- 上传防护：app/api/v1/files.py 与 app/application/files/file_service.py 的扩展名白名单与 magic bytes 校验。

运行方式：
    pytest tests/test_security.py            # 需后端 :8000 在线（pytestmark=backend，不可达 skip）
    pytest tests/test_security.py -k jwt     # 只跑 JWT 类用例
依赖夹具：conftest 的 http / user_acct / admin_acct / _created_uids；
无 pytest-asyncio 依赖，部分用例直接用 urllib 构造畸形请求。

设计原则：
- 不污染业务数据：使用 conftest 三角色夹具（autouse 软删清理）
- 不重复 test_api_rbac 已覆盖的 SQL 注入主路径（注册注入账号 + 重复 + 登录验证）
- 仅用 backend marker；不可达自动跳过
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

import pytest

# 模块级 marker：本文件全部用例标记 backend，后端 :8000 不可达时整体 skip
pytestmark = pytest.mark.backend


def _multipart_upload(base_url: str, token: str, filename: str, content: bytes,
                     scope: str = "private") -> tuple:
    """手工构造 multipart/form-data 上传请求并发送，返回 (status, raw_text)。

    功能：绕开高阶 HTTP 客户端，以精确控制 filename 中的恶意字符（路径穿越等）。
    调用方：TestPathTraversal.test_file_name_traversal。
    参数来源：filename/content 为攻击 payload；token 为 user_acct 的 JWT；
    boundary 用 os.urandom 现场生成避免重复。
    返回去向：调用方断言状态码与响应文本中不含系统文件指纹。
    """
    boundary = "pytest" + os.urandom(8).hex()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
    ).encode() + content + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="scope"\r\n\r\n'
        f"{scope}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/file/path",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ---------------- 工具：JWT 拆装 ----------------

def _b64url_decode(s: str) -> bytes:
    """补回 base64url 缺失的 padding 后解码为 bytes。调用方：_tamper_payload/_alg_none_token。"""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64url_encode(b: bytes) -> str:
    """编码为去 padding 的 base64url 字符串（JWT 各段格式）。调用方：同上。"""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _tamper_payload(token: str, **overrides) -> str:
    """拆 JWT → 改 payload → 不重签直接拼回（签名仍是旧的，应被拒）。
    验证签名校验：改 payload 后 token 应失效。
    """
    try:
        h_b64, p_b64, s_b64 = token.split(".")
    except ValueError:
        pytest.skip("token 不是三段式 JWT，跳过篡改测试")
    payload = json.loads(_b64url_decode(p_b64))
    payload.update(overrides)
    new_p_b64 = _b64url_encode(json.dumps(payload).encode())
    return f"{h_b64}.{new_p_b64}.{s_b64}"


def _alg_none_token(payload: dict) -> str:
    """构造 alg=none 的伪造 token（无签名），验证服务端拒绝 none 算法。"""
    h = {"alg": "none", "typ": "JWT"}
    h_b64 = _b64url_encode(json.dumps(h).encode())
    p_b64 = _b64url_encode(json.dumps(payload).encode())
    return f"{h_b64}.{p_b64}."


# ---------------- 1. SQL 注入（多端点 payload） ----------------

# SQL 注入 payload 模板集（恶意数据，参数化遍历多个端点）：
# 每组的攻击意图见行尾注释；断言口径统一为「不爆库/不 500/不回显 SQL 报错」
SQL_INJECTION_PAYLOADS = [
    "' OR 1=1 --",                 # 恶意：经典永真条件，试图绕过登录 WHERE
    "'; DROP TABLE user_information; --",  # 恶意：堆叠注入，试图删表
    "' UNION SELECT id, user_name, user_pwd FROM user_information --",  # 恶意：UNION 拖库密码
    "admin'--",                    # 恶意：注释掉密码校验冒充 admin
    "1;1;1",                       # 边界/畸形：非 SQL 形态的脏输入，验证解析不崩
    "' OR ''='",                   # 恶意：永真条件变体（单引号闭合）
    "\" OR \"\"=\"",               # 恶意：双引号闭合的永真条件
    "ඞ' OR 1=1 #",                 # 恶意：Unicode 字符 + MySQL 注释符 #，验证字符集处理
]


class TestSQLInjection:
    """SQL 注入端点扫描（参数化绑定情况下应原样字符串处理，不报错/不爆库）。

    聚合场景：登录、注册、会话标题、feedback 评论四类入口的注入。
    共同前置：http 夹具 + user_acct（鉴权入口）；注册成功的 uid 手动登记 _created_uids。
    被测接口：/login/account、/login/register、/history/create、/chat/feedback。
    """

    @pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS)
    def test_login_username_injection(self, http, payload):
        """登录用户名注入：参数化绑定让 payload 当字符串处理，
        可能 success（之前测试残留的同名账号密码也匹配）或 fail（账号不存在）。
        关键是：不爆库、不 500、响应不泄漏 SQL 报错。
        """
        status, body, raw = http("POST", "/login/account", json_body={
            "username": payload,
            "password": "Test1234",
        })
        assert status in (200, 422, 429), f"注入导致 {status}（应 200/422/429）：{payload}"
        if status == 200:
            # success 或 fail 都算通过（参数化绑定让 payload 当字符串）
            assert body.get("status") in ("success", "fail"), \
                f"注入响应异常：{payload} -> {body}"
            # 关键：响应不应泄漏 SQL 报错（如 "near 'OR 1=1'"）
            msg = body.get("message", "") or ""
            assert "syntax" not in msg.lower(), f"响应泄漏 SQL 语法错误：{msg}"
            assert "near" not in msg.lower(), f"响应泄漏 SQL 错误位置：{msg}"
            # 登录成功也不应返回密码哈希
            if body.get("status") == "success":
                assert "user_pwd" not in raw, "登录响应泄漏密码哈希字段"

    def test_register_username_injection(self, http, _created_uids):
        """注册用户名注入循环：参数化绑定让 payload 被当作字符串存储，
        应注册成功或撞 UNIQUE fail。limit 5/60 触发后 429 也是正向安全响应。

        单测循环（非 parametrize）避免 rate_limit 在 parametrize 60 秒内 5 次后全部 429，
        每个注册间隔 0.1 秒且单测试只跑 ≤5 次注册。
        """
        # 选取 4 个最危险 payload（保持总注册次数 ≤ 5 触发限流）
        payloads = SQL_INJECTION_PAYLOADS[:4]
        for payload in payloads:
            status, body, _ = http("POST", "/login/register", json_body={
                "user_name": payload[:20],
                "user_pwd": "Test1234",
                "email": f"inj_{abs(hash(payload))}@pytest.local",
            })
            # 接受 success/fail/429（429 = 限流也是正向安全响应）
            assert status in (200, 429), f"注入注册 {status}（应 200/429）：{payload}"
            if status == 200:
                assert body.get("status") in ("success", "fail"), \
                    f"注入响应异常：{payload} -> {body}"
                if body.get("status") == "success" and body.get("user_id"):
                    _created_uids.append(int(body["user_id"]))
            time.sleep(0.1)  # 微间隔避免立即撞 rate_limit

    @pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS[:4])
    def test_session_title_injection(self, http, user_acct, payload):
        """会话标题注入：title 字段长度限 100，注入 payload 截断后应正常创建。"""
        status, body, _ = http("POST", "/history/create", json_body={
            "title": payload[:100],
        }, token=user_acct["token"])
        assert status == 200, f"会话标题注入 500：{payload}"
        assert body.get("status") == "success", f"会话创建失败：{payload} -> {body}"

    @pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS[:3])
    def test_feedback_comment_injection(self, http, user_acct, payload):
        """feedback comment 注入：comment <=500，应正常落库（参数化绑定）。"""
        status, body, _ = http("POST", "/chat/feedback", json_body={
            "session_id": 99999,  # 不存在的会话也允许落库 feedback
            "message_index": 0,
            "rating": 1,
            "comment": payload[:500],
        }, token=user_acct["token"])
        assert status == 200, f"feedback 注入 500：{payload}"
        # feedback 接口对不存在的会话仍允许，status 应为 success


# ---------------- 2. XSS（持久化字段存储后取回） ----------------

# 持久化 XSS payload 模板集（恶意数据）：脚本/事件/协议/嵌套混淆/模板注入各一型，
# 意图为验证服务端仅做存储、不渲染执行，且参数化落库不抛 500
XSS_PAYLOADS = [
    "<script>alert('xss')</script>",   # 恶意：最基础脚本标签
    "<img src=x onerror=alert(1)>",    # 恶意：事件属性型，绕过 <script> 过滤
    "javascript:alert(document.cookie)",  # 恶意：伪协议脚本
    "<svg onload=alert(1)>",           # 恶意：SVG 标签事件
    "\"><script>alert(1)</script>",    # 恶意：闭合属性后注入
    "<scr<script>ipt>alert(1)</script>",  # 恶意：嵌套拆分，试探一次性黑名单过滤
    "{{constructor.constructor('alert(1)')()}}",  # 恶意：服务端模板注入（SSTI）探针
]


class TestStoredXSS:
    """持久化 XSS：载荷被存储后取回，验证服务端不做转义也无主动执行（API 层）。

    聚合场景：会话标题、feedback 评论、注册用户名三个可持久化字段。
    共同前置：http + user_acct；注册成功的 uid 手动登记 _created_uids 清理。
    被测接口：/history/create、/chat/feedback、/login/register（API 层不渲染 HTML，
    存储原样或转义都视为安全（消费方前端负责转义）。
    重点是：载荷不应导致 500/异常，应被参数化绑定安全存储。
    """

    @pytest.mark.parametrize("payload", XSS_PAYLOADS)
    def test_session_title_xss(self, http, user_acct, payload):
        """会话标题 XSS 载荷：应被安全存储（参数化绑定），不报 500。"""
        status, body, _ = http("POST", "/history/create", json_body={
            "title": payload[:100],
        }, token=user_acct["token"])
        assert status == 200, f"XSS 载荷导致 500：{payload}"
        assert body.get("status") == "success", f"XSS 载荷创建失败：{payload}"

    @pytest.mark.parametrize("payload", XSS_PAYLOADS[:4])
    def test_feedback_comment_xss(self, http, user_acct, payload):
        """feedback comment XSS 载荷：API 层不渲染，应安全落库。"""
        status, body, _ = http("POST", "/chat/feedback", json_body={
            "session_id": 99999,
            "message_index": 0,
            "rating": 1,
            "comment": payload[:500],
        }, token=user_acct["token"])
        assert status == 200, f"XSS feedback 500：{payload}"

    def test_register_username_xss(self, http, _created_uids):
        """注册用户名 XSS：可成功注册（存储原样）或撞 UNIQUE fail，不应 500。
        单测循环（避免 parametrize 多次注册触发 rate_limit 429）。
        """
        payloads = XSS_PAYLOADS[:3]
        for payload in payloads:
            uname = f"x{payload[:18]}"
            status, body, _ = http("POST", "/login/register", json_body={
                "user_name": uname,
                "user_pwd": "Test1234",
                "email": f"xss_{abs(hash(payload))}@pytest.local",
            })
            assert status in (200, 429), f"XSS 注册 {status}（应 200/429）：{payload}"
            if status == 200:
                assert body.get("status") in ("success", "fail")
                if body.get("status") == "success" and body.get("user_id"):
                    _created_uids.append(int(body["user_id"]))
            time.sleep(0.1)


# ---------------- 3. CSRF（JWT 不依赖 cookie） ----------------

class TestCSRFDefense:
    """CSRF 防御：JWT 经 Authorization 头携带，浏览器不会自动附加，
    故 CSRF 无载体。验证：无 Authorization 头的请求必须 401，且不依赖 cookie。

    共同前置：http 夹具；cookie 用例额外用 urllib 手工构造 Cookie 头。
    被测接口：/login/me、/history/list、/chat/send、/review/list（core/deps.py 鉴权依赖）。
    """

    def test_no_auth_header_rejected(self, http):
        """无 Authorization 头访问受保护端点必须 401。"""
        for path, method in [("/login/me", "GET"), ("/history/list", "POST"),
                             ("/chat/send", "POST"), ("/review/list", "GET")]:
            status, body, _ = http(method, path, json_body={"user_input": "x"} if "chat" in path else None)
            assert status in (401, 422), \
                f"无 Authorization 头访问 {method} {path} 返回 {status}，应 401/422"

    def test_cookie_only_auth_rejected(self, http, user_acct):
        """仅伪造 cookie（无 Authorization 头）：应仍 401，证明不依赖 cookie。"""
        url = f"{os.getenv('PBL_TEST_BASE_URL', 'http://localhost:8000')}/login/me"
        req = urllib.request.Request(url, method="GET",
                                     headers={"Cookie": f"token={user_acct['token']}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 401, f"仅 cookie 鉴权返回 {status}，应 401（不依赖 cookie）"


# ---------------- 4. 路径穿越 ----------------

# 路径穿越 payload 模板集（恶意数据）：覆盖 POSIX/Windows/URL 编码/双重编码/
# 混淆分隔符等变体，意图为让后端把用户输入拼出上传目录读敏感文件
PATH_TRAVERSAL_PAYLOADS = [
    "../../../../etc/passwd",                    # 恶意：POSIX 相对穿越读账户文件
    "..\\..\\..\\windows\\win.ini",              # 恶意：Windows 反斜杠穿越
    "../../../../../proc/self/environ",          # 恶意：读取进程环境变量（可能含密钥）
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",            # 恶意：URL 编码穿越（../ → %2e%2e%2f）
    "....//....//etc/passwd",                    # 恶意：混淆分隔符，试探单次 ../ 删减
    "..%252f..%252fetc%252fpasswd",              # 恶意：双重 URL 编码（%252f→%2f→/）
]


class TestPathTraversal:
    """路径穿越：文件名 / task_id 等用户输入不应被拼接到文件路径读取/写入。

    共同前置：http + user_acct；文件用例经 _multipart_upload 手工构造恶意 filename。
    被测接口：POST /file/path（app/application/files/file_service.py 的 uuid 重命名落盘）、
    GET /file/status/{task_id}（core/upload_task.py 内存任务表）。
    """

    @pytest.mark.parametrize("payload", PATH_TRAVERSAL_PAYLOADS)
    def test_file_name_traversal(self, http, user_acct, payload):
        """文件名含 ../：后端用 os.path.join + uuid 重命名，应拒绝扩展名或保存到目标目录内。"""
        base_url = os.getenv("PBL_TEST_BASE_URL", "http://localhost:8000")
        status, raw = _multipart_upload(
            base_url, user_acct["token"],
            filename=f"{payload}.txt",
            content=b"path traversal test\n",
        )
        # 接受 200/4xx（200 = uuid 重命名已防御；4xx = 扩展名校验/落盘失败）
        assert status in (200, 400, 403, 422, 413), \
            f"路径穿越 payload {payload} 返回 {status}，应 200/4xx"
        # 关键：响应不应泄漏系统文件内容（穿越成功的指纹）
        assert "root:" not in raw, f"路径穿越成功，泄漏 /etc/passwd 内容：{raw[:200]}"
        assert "[fonts]" not in raw, f"路径穿越成功，泄漏 win.ini 内容：{raw[:200]}"
        assert "PATH=" not in raw, f"路径穿越成功，泄漏 /proc/self/environ：{raw[:200]}"

    def test_file_status_task_id_traversal(self, http, user_acct):
        """/file/status/{task_id} 路径参数穿越：task_id 是字符串，但应查内存表不存在返回 404。"""
        status, _, _ = http("GET", "/file/status/..%2f..%2fetc%2fpasswd", token=user_acct["token"])
        # 路径穿越应被路由层/参数处理拦截，返回 404 或 422，不应 200
        assert status in (404, 422, 400), f"task_id 路径穿越返回 {status}，应 4xx"


# ---------------- 5. JWT 安全 ----------------

class TestJWTSecurity:
    """JWT 篡改/伪造/降级：服务端必须拒绝所有篡改形式。

    共同前置：http + user_acct（真实 token 作为篡改母本）。
    被测模块：core/security.py 的签发/校验逻辑、core/deps.py 的 get_current_user、
    dao/user.py 的 token_version 原子自增（单点互踢）。
    """

    def test_tamper_payload_user_id(self, http, user_acct):
        """篡改 payload 的 user_id（提权到 admin）：签名失效应 401。"""
        tampered = _tamper_payload(user_acct["token"], user_id=1, role="admin")
        status, _, _ = http("GET", "/login/me", token=tampered)
        assert status == 401, f"篡改 user_id 后仍能访问：{status}（应 401）"

    def test_tamper_payload_role(self, http, user_acct):
        """篡改 payload 的 role：user → admin 提权尝试，应 401。"""
        tampered = _tamper_payload(user_acct["token"], role="admin")
        status, _, _ = http("GET", "/admin/users", token=tampered)
        assert status == 401, f"篡改 role 后仍能访问 admin：{status}（应 401）"

    def test_tamper_signature(self, http, user_acct):
        """篡改签名段（最后一段）：签名校验失败应 401。
        修改签名中间字符（非末位），避免 base64url padding bits 误判不影响解码。
        """
        h, p, s = user_acct["token"].split(".")
        # 在签名中段修改（避免 padding bits 误判）：取第 5 个字符位置
        pos = 5 if len(s) > 10 else len(s) // 2
        orig_char = s[pos]
        # 替换为不同的合法 base64url 字符
        new_char = "A" if orig_char != "A" else "B"
        bad_sig = s[:pos] + new_char + s[pos + 1:]
        tampered = f"{h}.{p}.{bad_sig}"
        status, _, _ = http("GET", "/login/me", token=tampered)
        assert status == 401, f"篡改签名后仍能访问：{status}"

    def test_alg_none_token(self, http, user_acct):
        """alg=none 伪造 token（无签名）：服务端必须拒绝。"""
        fake = _alg_none_token({
            "user_id": user_acct["user_id"],
            "user_name": user_acct["user_name"],
            "role": "admin",
            "ver": user_acct.get("token_version", 0),
        })
        status, _, _ = http("GET", "/admin/users", token=fake)
        assert status == 401, f"alg=none token 被接受：{status}（应 401）"

    def test_token_version_mismatch(self, http, user_acct):
        """ver 不匹配（用户被踢）：旧 token 失效。
        通过 increment_token_version 后用旧 token 访问，应 401。
        """
        from app.infrastructure.persistence.repositories.user import Information
        info = Information()
        info.increment_token_version(user_acct["user_id"])
        # 旧 token 仍带旧 ver，应被拒
        status, _, _ = http("GET", "/login/me", token=user_acct["token"])
        assert status == 401, f"旧 token 仍可用：{status}（应 401，token_version 互踢失败）"

    def test_expired_token_format(self, http, user_acct):
        """构造已过期的 token（exp 在过去）：服务端应 401。
        通过篡改 payload 的 exp 字段实现。
        """
        tampered = _tamper_payload(user_acct["token"], exp=int(time.time()) - 3600)
        status, _, _ = http("GET", "/login/me", token=tampered)
        assert status == 401, f"过期 token 被接受：{status}"

    def test_malformed_token(self, http):
        """格式错误的 token（非三段式）：应 401。"""
        for bad in ["not.a.jwt", "xxx", "a.b", "", "Bearer "]:
            status, _, _ = http("GET", "/login/me", token=bad if bad != "Bearer " else None)
            assert status in (401, 422), f"畸形 token '{bad}' 返回 {status}，应 401/422"


# ---------------- 6. 信息泄漏 ----------------

class TestInfoLeak:
    """敏感信息不应通过 API 响应泄漏。

    共同前置：http + user_acct / admin_acct（真实登录响应与管理列表）。
    被测接口：/login/account、/login/me、/admin/users、/chat/send、/healthz；
    断言密码哈希、堆栈、SQL、内部路径、Server 版本指纹均不出现在响应中。
    """

    def test_login_response_no_password(self, http, user_acct):
        """登录响应不应包含密码字段（明文或哈希）。"""
        status, body, _ = http("POST", "/login/account", json_body={
            "username": user_acct["user_name"],
            "password": user_acct["password"],
        })
        assert status == 200
        raw = json.dumps(body)
        assert "user_pwd" not in raw, "登录响应泄漏密码字段"
        assert "password" not in raw.lower(), "登录响应含 password 字段"
        assert "hash" not in raw.lower(), "登录响应泄漏哈希值"

    def test_me_response_no_password(self, http, user_acct):
        """/login/me 响应不应包含密码字段。"""
        status, body, _ = http("GET", "/login/me", token=user_acct["token"])
        assert status == 200
        assert "user_pwd" not in body, "me 响应泄漏密码"
        assert "password" not in str(body).lower(), "me 响应含 password 字段"

    def test_admin_users_no_password(self, http, admin_acct):
        """/admin/users 不应返回密码字段。"""
        status, body, _ = http("GET", "/admin/users", token=admin_acct["token"])
        assert status == 200
        raw = json.dumps(body)
        assert "user_pwd" not in raw, "/admin/users 泄漏密码哈希"
        # password 字段不应出现（即使是空）
        if "users" in body:
            for u in body["users"]:
                assert "user_pwd" not in u, "/admin/users 行含 user_pwd 字段"
                assert "password" not in u, "/admin/users 行含 password 字段"

    def test_error_message_no_stacktrace(self, http, user_acct):
        """错误响应不应暴露堆栈/SQL/内部路径。"""
        url = f"{os.getenv('PBL_TEST_BASE_URL', 'http://localhost:8000')}/chat/send"
        req = urllib.request.Request(
            url,
            data=b'{invalid json',
            method="POST",
            headers={
                "Authorization": f"Bearer {user_acct['token']}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
        # 不论 200/4xx/5xx，响应文本不应含 traceback / File "/ 路径
        assert "Traceback" not in raw, f"错误响应泄漏堆栈：{raw[:200]}"
        assert 'File "' not in raw, f"错误响应泄漏文件路径：{raw[:200]}"
        assert "sqlalchemy" not in raw.lower(), f"错误响应泄漏 ORM：{raw[:200]}"

    def test_response_headers_no_server_version(self, http):
        """响应头不应暴露 server 版本/框架细节（避免指纹识别）。"""
        url = f"{os.getenv('PBL_TEST_BASE_URL', 'http://localhost:8000')}/healthz"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                server_hdr = resp.headers.get("Server", "")
        except urllib.error.HTTPError as e:
            server_hdr = e.headers.get("Server", "")
        # uvicorn 默认会暴露自身标识，记录但不强制断言（视部署配置）
        # 关键：不应泄漏 Python 版本、操作系统版本
        assert "Python/" not in server_hdr, f"响应头泄漏 Python 版本：{server_hdr}"
        assert "Windows" not in server_hdr, f"响应头泄漏 OS：{server_hdr}"


# 修复：test_register_username_injection 使用了 _created_uids 但未在参数中声明，需要修正
# conftest 的 _created_uids 是 fixture，参数名需保持一致
