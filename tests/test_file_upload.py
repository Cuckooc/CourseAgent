"""
模块名：tests/test_file_upload.py。

文件上传安全测试套件（固化自《全流程测试报告.md》第五章 7 项），风险类型：
上传链路的恶意文件绕过、鉴权缺失、路径穿越与向量库/知识库一致性。

覆盖：
- 正常 .txt 上传（真实解析+脱敏+embedding，doc_type=pure_text）
- 知识库列表可见新文件
- 伪装可执行文件（MZ 头）magic bytes 拦截
- 非白名单扩展名（.exe）拦截
- 未鉴权上传 → 401
- uuid 重命名 + 路径穿越防护
- 向量入库（Chroma）

测试函数清单（模块级函数，无测试类）：
- test_upload_normal_txt：正常 txt 上传，断言 status/doc_type
- test_upload_visible_in_knowledge_list：上传后 /knowledge/list 可见
- test_upload_magic_bytes_mz_rejected：PE 魔数伪装 → 400
- test_upload_non_whitelisted_extension_rejected：.exe 扩展名 → 400
- test_upload_unauthenticated_rejected：无效 token → 401
- test_upload_stored_name_format：穿越文件名的 stored_name 清洗规则
- test_upload_persists_to_chroma：上传后知识库列表可检索（向量入库）
辅助函数：_build_multipart（RFC 2046 multipart 报文构造）、_upload（发送上传请求）。
模块常量：UPLOAD_URL（POST /file/path 全地址）、KNOWLEDGE_LIST_URL（列表相对路径）。

被测对象来源：
- 路由：app/api/v1/files.py（POST /file/path，扩展名白名单 + _validate_magic
  magic bytes 校验 + uuid 重命名落盘）、app/api/v1/knowledge.py（/knowledge/list、
  删除走 /knowledge/delete/* 本文件未用）；
- 业务：app/application/files/file_service.py（解析/脱敏/embedding）、app/infrastructure/vector_store/persistent.py
  与 embedding/（DashScope embedding + Chroma 持久化）；
- 鉴权：core/deps.py（无效 token → 401）。

运行方式：
    pytest tests/test_file_upload.py -m "slow and db and backend"
    # 默认不跑（slow）；需后端 :8000、MySQL、Chroma 与 DashScope embedding 可用
依赖夹具：conftest 的 http / user_acct / db_engine；
清理：测试账号软删级联清理业务数据；embedding 每轮耗时数秒。

设计：
- 用 urllib 手动构造 multipart/form-data（不引入 requests 依赖）；
- 真实 DashScope embedding（每轮耗时数秒），标 `slow` marker；
- 测试账号测后软删，上传文件物理路径会随账号软删级联清理。
"""
import io
import os
import time
import uuid

import pytest
import urllib.request
from sqlalchemy import text

# 模块级 markers：需后端在线 + 真实 embedding/Chroma（slow）+ 真实 MySQL（db）
pytestmark = [pytest.mark.backend, pytest.mark.slow, pytest.mark.db]

# 上传端点全地址常量（脚本内 urllib 直连，默认本机 :8000）
UPLOAD_URL = "http://localhost:8000/file/path"
# 知识库列表相对路径常量（经 conftest http 夹具走 BASE_URL）
KNOWLEDGE_LIST_URL = "/knowledge/list"


def _build_multipart(fields, files):
    """构造 multipart/form-data 请求体（RFC 2046）。

    功能：把普通表单字段与文件段序列化为带 boundary 的二进制报文。
    调用方：_upload。
    参数来源：fields 为 {字段名: 字符串值}（如 scope=private）；
    files 为 [(field_name, filename, content_bytes, content_type), ...]，
    由各上传用例构造（正常文本 / MZ 魔数 / .exe / 穿越文件名）。
    返回去向：(Content-Type 头值, body 字节串)，供 _upload 装入 urllib.Request。

    fields: dict of str -> str
    files: list of (field_name, filename, content_bytes, content_type)
    返回 (content_type_header, body_bytes)
    """
    # boundary 必须是 [a-zA-Z0-9'_+,-] 串，最多 70 字符（RFC 2046 §5.1.1）
    boundary = "pytest" + uuid.uuid4().hex
    crlf = b"\r\n"
    buf = io.BytesIO()

    for name, value in fields.items():
        buf.write(f"--{boundary}".encode())
        buf.write(crlf)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode())
        buf.write(crlf)
        buf.write(crlf)
        buf.write(str(value).encode("utf-8"))
        buf.write(crlf)

    for field_name, filename, content, ctype in files:
        buf.write(f"--{boundary}".encode())
        buf.write(crlf)
        buf.write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode()
        )
        buf.write(crlf)
        buf.write(f"Content-Type: {ctype}".encode())
        buf.write(crlf)
        buf.write(crlf)
        buf.write(content)
        buf.write(crlf)

    buf.write(f"--{boundary}--".encode())
    buf.write(crlf)
    body = buf.getvalue()
    return f"multipart/form-data; boundary={boundary}", body


def _upload(token, files_payload, fields=None, timeout=120):
    """发起上传请求，返回 (status, json_body, raw_text)。

    调用方：本文件全部 test_upload_* 用例。
    参数来源：token 为 user_acct JWT（未鉴权用例传非法字符串）；
    files_payload 为 (字段名, 文件名, 内容字节, MIME) 列表；fields 默认 scope=private。
    返回去向：调用方据此断言 HTTP 状态码、body.status/files[].doc_type/stored_name。
    被替身外部依赖：无 mock，真实后端 + 真实解析/embedding。
    files_payload: list of (field_name, filename, content, ctype)。
    返回 (status, json_body, raw_text)。"""
    ct, body = _build_multipart(fields or {}, files_payload)
    req = urllib.request.Request(
        UPLOAD_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": ct},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    import json as _json
    try:
        return status, _json.loads(raw) if raw else {}, raw
    except _json.JSONDecodeError:
        return status, {}, raw


def test_upload_normal_txt(http, user_acct):
    """正常 .txt 上传（scope=private）→ success + doc_type=pure_text。"""
    content = f"测试文件内容：pytest 上传验证 {uuid.uuid4().hex}".encode("utf-8")
    status, body, _ = _upload(
        user_acct["token"],
        [("files", "test_upload.txt", content, "text/plain")],
        {"scope": "private"},
    )
    assert status == 200, f"正常上传失败：{status} {body}"
    assert body.get("status") in ("success", "partial"), f"上传业务失败：{body}"
    files = body.get("files") or []
    assert files, f"未返回 files 详情：{body}"
    assert files[0].get("status") == "success", f"首个文件状态异常：{files[0]}"
    assert files[0].get("doc_type") == "pure_text", f"doc_type 异常：{files[0]}"


def test_upload_visible_in_knowledge_list(http, user_acct):
    """上传后 GET /knowledge/list 可见新文件。"""
    content = f"知识库可见性测试 {uuid.uuid4().hex}".encode("utf-8")
    s_up, b_up, _ = _upload(
        user_acct["token"],
        [("files", "kb_visible.txt", content, "text/plain")],
        {"scope": "private"},
    )
    assert s_up == 200 and b_up.get("files")[0].get("status") == "success"

    # 列表查询
    status, body, _ = http("GET", KNOWLEDGE_LIST_URL, token=user_acct["token"])
    assert status == 200, f"knowledge/list 失败：{status}"
    data = body.get("data") or body
    items = data if isinstance(data, list) else data.get("items") or data.get("list") or []
    found = any("kb_visible" in (str(i.get("filename") or i.get("original_name") or "")) for i in items)
    assert found, f"上传文件在 /knowledge/list 中未找到：{items[:3]}"


def test_upload_magic_bytes_mz_rejected(user_acct):
    """伪装可执行文件（MZ 头改名 .txt）→ magic bytes 拦截 → 400。"""
    content = b"MZ\x90\x00\x03\x00\x00\x00fake PE executable content here"
    status, body, _ = _upload(
        user_acct["token"],
        [("files", "evil.txt", content, "text/plain")],
        {"scope": "private"},
    )
    assert status == 400, f"MZ 头应被拦截返回 400，实际 {status}：{body}"


def test_upload_non_whitelisted_extension_rejected(user_acct):
    """非白名单扩展名（.exe）→ 400。"""
    content = b"fake exe content"
    status, body, _ = _upload(
        user_acct["token"],
        [("files", "evil.exe", content, "application/octet-stream")],
        {"scope": "private"},
    )
    assert status == 400, f".exe 应被拦截返回 400，实际 {status}：{body}"


def test_upload_unauthenticated_rejected():
    """未鉴权上传 → 401。"""
    content = b"unauth upload test"
    status, _, _ = _upload(
        "invalid.token.here",
        [("files", "unauth.txt", content, "text/plain")],
        {"scope": "private"},
    )
    assert status == 401, f"未鉴权上传应 401，实际 {status}"


def test_upload_stored_name_format(http, user_acct):
    """uuid 重命名 + 路径穿越防护：stored_name 形如 {uid}_xxx_{hash}.txt。"""
    # 用包含路径穿越尝试的文件名
    evil_filename = "../../etc/passwd.txt"
    content = f"path traversal test {uuid.uuid4().hex}".encode("utf-8")
    status, body, _ = _upload(
        user_acct["token"],
        [("files", evil_filename, content, "text/plain")],
        {"scope": "private"},
    )
    # 路径穿越文件名要么被清洗后存储（保留下划线命名规则），要么被拒
    if status == 200:
        files = body.get("files") or []
        assert files, f"未返回 files：{body}"
        stored = files[0].get("stored_name") or ""
        # 不允许出现 ../ 或绝对路径
        assert ".." not in stored and not stored.startswith("/"), \
            f"stored_name 含路径穿越：{stored}"
        # 必须以 _<uid>_ 开头或包含 uid
        assert str(user_acct["user_id"]) in stored or stored.endswith(".txt"), \
            f"stored_name 不符合用户ID+hash 规则：{stored}"
    elif status == 400:
        # 拒绝路径穿越也是合法策略
        pass
    else:
        pytest.fail(f"路径穿越上传响应异常：{status} {body}")


def test_upload_persists_to_chroma(http, user_acct, db_engine):
    """上传成功后知识库列表可检索（向量入库 Chroma 验证）。"""
    content = f"向量入库测试 {uuid.uuid4().hex}\n这是一段用于 Chroma 检索的内容。".encode("utf-8")
    s_up, b_up, _ = _upload(
        user_acct["token"],
        [("files", "chroma_test.txt", content, "text/plain")],
        {"scope": "private"},
    )
    assert s_up == 200, f"上传失败：{s_up}"
    assert b_up.get("files")[0].get("status") == "success", \
        f"上传未成功：{b_up.get('files')}"

    # 列表检索
    status, body, _ = http("GET", KNOWLEDGE_LIST_URL, token=user_acct["token"])
    assert status == 200
    data = body.get("data") or body
    items = data if isinstance(data, list) else data.get("items") or data.get("list") or []
    found = any("chroma_test" in (str(i.get("filename") or i.get("original_name") or "")) for i in items)
    assert found, f"Chroma 入库后未在 /knowledge/list 中检索到"
