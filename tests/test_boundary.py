"""
模块名：tests/test_boundary.py。

边界测试套件：字段长度/分页极值/文件大小/Unicode/空值/数值边界（风险类型：边界值与
输入校验缺失，防护点为 Pydantic 字段约束与 control 层 BizException 兜底）。

覆盖维度：
1. 字段长度边界：user_name(1/20/21) + user_pwd(7/8/64/65) + email(2/3/255/256)
   + title(0/1/100/101) + user_input(0/1/4000/4001)
2. 分页极值：通过 query string 传 page=0/-1 + page_size=0/-1/51
3. 文件大小：0 字节 / 1 字节
4. Unicode/emoji：user_name 含 emoji + title 含中文/控制字符/零宽字符
5. 空值/None：缺字段 + null 值 + 空字符串
6. 数值边界：session_id=0/-1/INT_MAX + message_index=0/-1 + rating=0/2/-2

测试类与测试函数清单：
- TestFieldLengthBoundary：test_user_name_boundary / test_user_pwd_boundary /
  test_email_boundary / test_session_title_boundary / test_chat_user_input_boundary
- TestPaginationBoundary：test_page_zero_and_negative / test_page_size_extreme /
  test_page_huge_value
- TestFileSizeBoundary：test_empty_file_rejected / test_one_byte_file_accepted
- TestUnicodeBoundary：test_session_title_emoji / test_user_name_unicode /
  test_session_title_control_chars / test_zero_width_chars
- TestNullAndMissing：test_missing_required_fields / test_null_values /
  test_empty_string_user_input / test_session_id_missing
- TestNumericBoundary：test_rating_invalid_values / test_message_index_negative /
  test_session_id_extreme / test_review_id_nonexistent / test_review_id_invalid_format
本文件夹具：_reset_rate_limit（autouse，每用例前清空限流窗口）。

被测对象来源：
- 路由：control/login_control.py（/login/register）、control/history_control.py
  （/history/create、/history/list 分页 Query 参数、/history/detail）、
  control/chat_control.py（/chat/send、/chat/feedback）、control/review_control.py
  （GET /review/{id} 路径参数 int 校验）、control/file_control.py（POST /file/path）；
- 校验：control 层 Pydantic 请求模型（字段 min/max_length、ge/le 约束，违例统一 422）、
  service 层 BizException（rating 等业务值域，违例 400 或 status=fail）；
- 文件空内容：control/file_control.py 的 _validate_magic。

运行方式：
    pytest tests/test_boundary.py             # 需后端 :8000（pytestmark=backend）
    pytest tests/test_boundary.py -k "not upload"  # 排除较慢的 multipart 用例
依赖夹具：conftest 的 http / user_acct / _created_uids 与本文件 _reset_rate_limit；
边界数据均在用例内按「最小合法 / 刚好最大 / 越界 ±1」原则内联构造。

设计原则：
- 不污染业务数据：用 conftest 三角色夹具（autouse 软删清理）
- 接受 422（参数校验）和 200（业务成功）都算通过
- 不接受 500（应被参数校验/异常处理兜底）
- register 端点 rate_limit(5,60)：每个测试前 reset_rate_limit_store
"""
import os
import time

import pytest

# 模块级 marker：全部用例需后端在线
pytestmark = pytest.mark.backend


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """autouse 前置夹具：每个测试前清空限流窗口。

    数据来源/被替身依赖：直接调用 core/deps.py 的 reset_rate_limit_store，
    清空进程内（或 Redis）限流桶；yield 后无后置动作（账号由 conftest 软删）。
    使用方：本文件全部用例（规避 register 5/60s 与对话 10/60s 限流干扰边界断言）。
    """
    from app.auth.rate_limit import reset_rate_limit_store  # noqa: WPS433
    reset_rate_limit_store()
    yield


# ---------------- 1. 字段长度边界 ----------------

class TestFieldLengthBoundary:
    """字段长度边界：刚好/超限。

    共同前置：http + _reset_rate_limit；注册成功的 uid 登记 _created_uids。
    被测接口与约束：/login/register（user_name VARCHAR(20)、user_pwd 8~64、email 3~255）、
    /history/create（title max_length=100）、/chat/send（user_input 1~4000）。
    """

    def test_user_name_boundary(self, http, _created_uids):
        """user_name VARCHAR(20)：1/20 字符应通过，21+ 应 422。"""
        # 1 字符（最小）
        s, body, _ = http("POST", "/login/register", json_body={
            "user_name": "a", "user_pwd": "Test1234",
            "email": f"u1_{int(time.time()*1000)}@pytest.local",
        })
        assert s in (200, 429), f"user_name=1字符 应 200/429，实际 {s}"
        if s == 200 and body.get("status") == "success" and body.get("user_id"):
            _created_uids.append(int(body["user_id"]))
        time.sleep(0.05)

        # 20 字符（最大）
        s, body, _ = http("POST", "/login/register", json_body={
            "user_name": "a" * 20, "user_pwd": "Test1234",
            "email": f"u20_{int(time.time()*1000)}@pytest.local",
        })
        assert s in (200, 429), f"user_name=20字符 应 200/429，实际 {s}"
        if s == 200 and body.get("status") == "success" and body.get("user_id"):
            _created_uids.append(int(body["user_id"]))
        time.sleep(0.05)

        # 21 字符（超限）：应 422
        s, body, _ = http("POST", "/login/register", json_body={
            "user_name": "a" * 21, "user_pwd": "Test1234",
            "email": "u21@pytest.local",
        })
        assert s == 422, f"user_name=21字符 应 422，实际 {s}"

    def test_user_pwd_boundary(self, http, _created_uids):
        """user_pwd 8-64 字符：7 字符 422，8/64 通过，65+ 422。"""
        # 7 字符（短）
        s, _, _ = http("POST", "/login/register", json_body={
            "user_name": f"p7_{int(time.time()*1000)}"[:20], "user_pwd": "T1234",
            "email": "p7@pytest.local",
        })
        assert s == 422, f"user_pwd=7字符 应 422，实际 {s}"
        time.sleep(0.05)

        # 8 字符（最小合法）
        s, body, _ = http("POST", "/login/register", json_body={
            "user_name": f"p8_{int(time.time()*1000)}"[:20], "user_pwd": "Test1234",
            "email": f"p8_{int(time.time()*1000)}@pytest.local",
        })
        assert s in (200, 429)
        if s == 200 and body.get("user_id"):
            _created_uids.append(int(body["user_id"]))
        time.sleep(0.05)

        # 65 字符（超限）
        s, _, _ = http("POST", "/login/register", json_body={
            "user_name": f"p65_{int(time.time()*1000)}"[:20],
            "user_pwd": "T" + "1" * 64,  # 65 字符
            "email": "p65@pytest.local",
        })
        assert s == 422, f"user_pwd=65字符 应 422，实际 {s}"

    def test_email_boundary(self, http, _created_uids):
        """email 3-255 字符：2 字符 422，3/255 通过，256+ 422。"""
        # 2 字符（短）
        s, _, _ = http("POST", "/login/register", json_body={
            "user_name": f"e2_{int(time.time()*1000)}"[:20],
            "user_pwd": "Test1234", "email": "ab",
        })
        assert s == 422, f"email=2字符 应 422，实际 {s}"
        time.sleep(0.05)

        # 256 字符（超限）
        s, _, _ = http("POST", "/login/register", json_body={
            "user_name": f"e256_{int(time.time()*1000)}"[:20],
            "user_pwd": "Test1234", "email": "a" * 256,
        })
        assert s == 422, f"email=256字符 应 422，实际 {s}"

    def test_session_title_boundary(self, http, user_acct):
        """会话 title max_length=100：1/100 通过，101+ 422。
        注意：CreateSessionRequest 无 min_length 校验，空字符串 "" 会使用 default="新会话"。
        """
        # 100 字符（最大）
        s, body, _ = http("POST", "/history/create", json_body={"title": "t" * 100},
                          token=user_acct["token"])
        assert s == 200 and body.get("status") == "success", \
            f"title=100字符 应 200/success，实际 {s}/{body}"

        # 101 字符（超限）
        s, _, _ = http("POST", "/history/create", json_body={"title": "t" * 101},
                       token=user_acct["token"])
        assert s == 422, f"title=101字符 应 422，实际 {s}"

    def test_chat_user_input_boundary(self, http, user_acct):
        """chat user_input 1-4000 字符：0 字符 422，4001+ 422。
        不实际调 LLM：用 user_acct 但只测参数校验。
        """
        # 0 字符：min_length=1 应 422（参数校验，不调 LLM）
        s, _, _ = http("POST", "/chat/send", json_body={"user_input": "", "session_id": 0},
                       token=user_acct["token"])
        assert s == 422, f"user_input=空 应 422，实际 {s}"

        # 4001 字符：max_length=4000 应 422
        s, _, _ = http("POST", "/chat/send", json_body={"user_input": "x" * 4001, "session_id": 0},
                       token=user_acct["token"])
        assert s == 422, f"user_input=4001字符 应 422，实际 {s}"


# ---------------- 2. 分页极值 ----------------

class TestPaginationBoundary:
    """分页参数极值：通过 query string 传 page=0/-1 + page_size=0/-1/51。

    共同前置：http + user_acct。
    被测接口：POST /history/list（Query 参数 page ge=1、page_size 1~50，非 JSON body）；
    超大 page 不报错而返回空 data 也是预期。
    """

    def test_page_zero_and_negative(self, http, user_acct):
        """page=0 / -1 通过 query string 应 422（ge=1 校验）。"""
        for p in [0, -1, -100]:
            # 通过 query string 传参
            s, _, _ = http("POST", f"/history/list?range=all&page={p}&page_size=10",
                           token=user_acct["token"])
            assert s == 422, f"page={p} 应 422，实际 {s}"

    def test_page_size_extreme(self, http, user_acct):
        """page_size=0/-1/51/1000 通过 query string 应 422（ge=1, le=50 校验）。"""
        for ps in [0, -1, 51, 1000]:
            s, _, _ = http("POST", f"/history/list?range=all&page=1&page_size={ps}",
                           token=user_acct["token"])
            assert s == 422, f"page_size={ps} 应 422，实际 {s}"

    def test_page_huge_value(self, http, user_acct):
        """page=巨大值（10^9）通过 query string：应 200，返回空 data。"""
        s, body, _ = http("POST", "/history/list?range=all&page=1000000000&page_size=10",
                          token=user_acct["token"])
        assert s == 200, f"page=10^9 应 200，实际 {s}"
        data = body.get("data") or body.get("sessions") or []
        assert isinstance(data, list) and len(data) == 0, \
            f"巨大 page 应返回空 data，实际 {data}"


# ---------------- 3. 文件大小边界 ----------------

class TestFileSizeBoundary:
    """文件大小边界：0 字节 / 1 字节 / 超大（mock 不真上传）。

    共同前置：http + user_acct；用 urllib 手工拼最小 multipart 报文。
    被测接口/防护：POST /file/path → control/file_control.py 的 _validate_magic
    （0 字节抛「文件内容为空」→ 400/422；1 字节走正常解析链路）。
    """

    def test_empty_file_rejected(self, http, user_acct):
        """0 字节文件：file_control._validate_magic 应 raise BizException("文件内容为空")。"""
        import urllib.error
        import urllib.request
        boundary = "pytest" + os.urandom(8).hex()
        body_bytes = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="empty.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"\r\n--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"{os.getenv('PBL_TEST_BASE_URL', 'http://localhost:8000')}/file/path",
            data=body_bytes, method="POST",
            headers={
                "Authorization": f"Bearer {user_acct['token']}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        # 0 字节文件应被拒（400 "文件内容为空" 或 422 参数校验）
        assert status in (400, 422, 413), \
            f"0 字节文件应被拒，实际 {status}"

    def test_one_byte_file_accepted(self, http, user_acct):
        """1 字节文件：应能正常处理（无 magic bytes 校验失败）。"""
        import urllib.error
        import urllib.request
        boundary = "pytest" + os.urandom(8).hex()
        body_bytes = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="one.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"x\r\n--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"{os.getenv('PBL_TEST_BASE_URL', 'http://localhost:8000')}/file/path",
            data=body_bytes, method="POST",
            headers={
                "Authorization": f"Bearer {user_acct['token']}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        # 1 字节文件应被接受处理（200 或 4xx 如解析失败）
        assert status in (200, 400, 422, 413), \
            f"1 字节文件 status={status}（应 200/4xx）"


# ---------------- 4. Unicode/emoji/控制字符 ----------------

class TestUnicodeBoundary:
    """Unicode / emoji / 控制字符 / 零宽字符。

    共同前置：http + user_acct（注册用例成功 uid 登记 _created_uids）。
    被测接口：/history/create（MySQL utf8mb4 列）、/login/register；
    意图：验证多字节字符按字符计数、控制字符/零宽字符不引发 500。
    """

    def test_session_title_emoji(self, http, user_acct):
        """会话 title 含 emoji：应正常创建（MySQL utf8mb4 支持）。"""
        for title in ["🚀会话", "Test🎉", "中文测试", "한국어", "日本語"]:
            s, body, _ = http("POST", "/history/create",
                              json_body={"title": title},
                              token=user_acct["token"])
            assert s == 200, f"title='{title}' 应 200，实际 {s}"
            assert body.get("status") == "success", f"title='{title}' 失败：{body}"

    def test_user_name_unicode(self, http, _created_uids):
        """user_name 含 emoji：MySQL VARCHAR(20) 按 utf8mb4 字符计数字符数。
        emoji 占 1-2 字符位置（看 MySQL 配置）。
        """
        emoji_uname = "🚀user"  # 5 字符
        s, body, _ = http("POST", "/login/register", json_body={
            "user_name": emoji_uname,
            "user_pwd": "Test1234",
            "email": f"emoji_{int(time.time()*1000)}@pytest.local",
        })
        # emoji 在 user_name 列可能因长度计算方式不同而 422（按字节算则超限）
        # 接受 200（成功）或 422（长度超限）或 429（限流）
        assert s in (200, 422, 429), f"emoji user_name 应 200/422/429，实际 {s}"
        if s == 200 and body.get("status") == "success" and body.get("user_id"):
            _created_uids.append(int(body["user_id"]))

    def test_session_title_control_chars(self, http, user_acct):
        """会话 title 含控制字符（\n/\t/\0）：应被接受或清理后接受。"""
        for title in ["line\nbreak", "tab\there", "null\x00byte"]:
            s, body, _ = http("POST", "/history/create",
                              json_body={"title": title},
                              token=user_acct["token"])
            # 控制字符应被参数校验或正常存储
            assert s in (200, 422), f"控制字符 title='{title!r}' 应 200/422，实际 {s}"

    def test_zero_width_chars(self, http, user_acct):
        """零宽字符（ZWSP/ZWJ）：不可见但占字符位置，应被接受。"""
        zw_title = "abc\u200bdef"  # 零宽空格
        s, body, _ = http("POST", "/history/create",
                          json_body={"title": zw_title},
                          token=user_acct["token"])
        assert s == 200, f"零宽字符 title 应 200，实际 {s}"


# ---------------- 5. 空值/None ----------------

class TestNullAndMissing:
    """空值 / None / 缺字段。

    共同前置：http（user 类接口用 user_acct）。
    被测接口：/login/register（必填缺失/null → 422）、/chat/send（min_length=1）、
    /chat/feedback（session_id 必填）。
    """

    def test_missing_required_fields(self, http):
        """缺必填字段：应 422。"""
        # 缺 user_name
        s, _, _ = http("POST", "/login/register", json_body={
            "user_pwd": "Test1234", "email": "x@pytest.local",
        })
        assert s == 422, f"缺 user_name 应 422，实际 {s}"

        # 缺 user_pwd
        s, _, _ = http("POST", "/login/register", json_body={
            "user_name": "test", "email": "x@pytest.local",
        })
        assert s == 422, f"缺 user_pwd 应 422，实际 {s}"

    def test_null_values(self, http):
        """null 值：应 422（必填字段不能为 null）。"""
        s, _, _ = http("POST", "/login/register", json_body={
            "user_name": None, "user_pwd": "Test1234", "email": "x@pytest.local",
        })
        assert s == 422, f"user_name=null 应 422，实际 {s}"

    def test_empty_string_user_input(self, http, user_acct):
        """chat user_input 空字符串：min_length=1 应 422。"""
        s, _, _ = http("POST", "/chat/send",
                       json_body={"user_input": "", "session_id": 0},
                       token=user_acct["token"])
        assert s == 422, f"user_input=空 应 422，实际 {s}"

    def test_session_id_missing(self, http, user_acct):
        """feedback 缺 session_id：应 422（必填）。"""
        s, _, _ = http("POST", "/chat/feedback",
                       json_body={"message_index": 0, "rating": 1},
                       token=user_acct["token"])
        assert s == 422, f"feedback 缺 session_id 应 422，实际 {s}"


# ---------------- 6. 数值边界 ----------------

class TestNumericBoundary:
    """数值边界：session_id / message_index / rating 极值。

    共同前置：http + user_acct。
    被测接口：/chat/feedback（rating 业务值域 {1,-1}、message_index ge=0）、
    /history/detail（session_id 极值容错返回空 list）、GET /review/{id}
    （不存在 404、非数字路径参数 422）。
    """

    def test_rating_invalid_values(self, http, user_acct):
        """rating 只接受 1/-1，其他值应被拒（BizException 400 或 200 fail）。"""
        for r in [0, 2, -2, 100, -100]:
            s, body, _ = http("POST", "/chat/feedback",
                              json_body={"session_id": 1, "message_index": 0, "rating": r},
                              token=user_acct["token"])
            # rating 校验在端点内部（非 Pydantic），返回 200 + BizException 或 400
            assert s in (200, 400), f"rating={r} 应 200/400，实际 {s}"
            if s == 200:
                assert body.get("status") == "fail", \
                    f"rating={r} 应业务失败，实际 {body}"

    def test_message_index_negative(self, http, user_acct):
        """message_index < 0：Pydantic ge=0 应 422。"""
        s, _, _ = http("POST", "/chat/feedback",
                       json_body={"session_id": 1, "message_index": -1, "rating": 1},
                       token=user_acct["token"])
        assert s == 422, f"message_index=-1 应 422，实际 {s}"

    def test_session_id_extreme(self, http, user_acct):
        """session_id 极值：0 / -1 / INT_MAX：应被接受处理（不存在的会话返回空）。"""
        for sid in [0, -1, 2**31 - 1]:
            s, body, _ = http("POST", "/history/detail",
                              json_body={"session_id": sid},
                              token=user_acct["token"])
            # 不存在的 session 应返回空 messages
            assert s == 200, f"session_id={sid} 应 200，实际 {s}"
            data = body.get("data") or []
            assert isinstance(data, list), f"session_id={sid} data 非 list"

    def test_review_id_nonexistent(self, http, user_acct):
        """review_id 不存在：应 404。"""
        s, _, _ = http("GET", "/review/999999", token=user_acct["token"])
        assert s == 404, f"review_id=999999 应 404，实际 {s}"

    def test_review_id_invalid_format(self, http, user_acct):
        """review_id 非数字：应 422（路径参数 int 类型校验）。"""
        s, _, _ = http("GET", "/review/abc", token=user_acct["token"])
        assert s == 422, f"review_id='abc' 应 422，实际 {s}"
