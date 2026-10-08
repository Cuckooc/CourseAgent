"""
模块名：tests/phase/test_phase5_boundary.py。

阶段五：边界测试脚本（脚本式用例：模块导入即顺序执行，全局 PASS/FAIL + check()
汇总；pytest 收集时同样按脚本执行）。风险类型：边界值与输入校验，防护点为
Pydantic 约束、扩展名/magic 校验与 JWT 鉴权。

测试场景清单（数据意图见正文行内注释）：
TC-6.1 空文件（边界：0 字节）→ 4xx 或 status=fail
TC-6.2 超长文件名（边界：300 字符）→ 200/400/422，不 500
TC-6.3 特殊字符文件名（边界：中文文件名）→ 成功或业务 fail，用例尾做删除清理
TC-6.4 伪装 exe（恶意：MZ 魔数改名 .txt）→ 拒绝
TC-6.5 伪装 PDF（恶意：%PDF- 头放进 .txt）→ 拒绝
TC-6.7 对话超长（边界：4001 字 user_input）→ 422
TC-6.8 空对话（边界：空字符串）→ 422
TC-6.9 无效 JWT（恶意：伪造 token）→ 401

被测对象来源：
- 路由：app/api/v1/files.py（POST /file/path，空内容/超长名/魔数校验）、
  app/api/v1/chat.py（POST /chat/stream，user_input 长度与 JWT 校验）；
- 清理：app/api/v1/knowledge.py 的 /knowledge/delete/preview|confirm 两步删除。

运行方式：
    python tests/phase/test_phase5_boundary.py
    # 需后端 :8000；固定账号 opttest（密码 test1234）预先存在；不需 LLM 真实返回
    # （6.7/6.8/6.9 在参数/鉴权层即被拒，不会进入 LLM 链路）
依赖说明：requests 直发 HTTP，不经 conftest。
清理：仅 TC-6.3 成功时删除该特殊字符文件；其余被拒请求无落盘无需清理。
"""
import requests
import io

BASE = "http://127.0.0.1:8000"  # 后端基址常量（脚本直连）
PASS = 0  # 全局通过断言计数
FAIL = 0  # 全局失败断言计数

def check(name, cond, extra=""):
    """断言辅助：累加全局 PASS/FAIL 并打印，不抛异常。调用方：本脚本全部 TC。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")

def login(u, p):
    """登录返回 access_token（固定账号 opttest/test1234，须预先存在）。"""
    r = requests.post(f"{BASE}/login/account", json={"username": u, "password": p})
    return r.json()["access_token"]

token = login("opttest", "test1234")
headers = {"Authorization": f"Bearer {token}"}

# === TC-6.1 空文件 ===
# 边界数据意图：0 字节内容，命中空文件/空内容校验
print("\n=== TC-6.1 空文件 ===")
files = {"files": ("empty.txt", io.BytesIO(b""), "text/plain")}
data = {"scope": "private"}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=60)
resp = r.json()
print(f"  HTTP {r.status_code}: {resp.get('message','')}")
check("TC-6.1: 拒绝空文件", r.status_code >= 400 or resp.get("status") == "fail")

# === TC-6.2 超长文件名 ===
# 边界数据意图：300 个 'a' + .txt，超出常见 255 文件名/字段长度，验证不 500
print("\n=== TC-6.2 超长文件名 ===")
long_name = "a" * 300 + ".txt"
files = {"files": (long_name, io.BytesIO(b"long name test content"), "text/plain")}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=60)
resp = r.json()
print(f"  HTTP {r.status_code}: {resp.get('message','')[:80]}")
check("TC-6.2: 超长名可处理(成功或合理拒绝)", r.status_code in (200, 400, 422))

# === TC-6.3 特殊字符文件名 ===
# 边界数据意图：中文文件名（非 ASCII），验证编码/落盘处理；成功时下方两步删除清理
print("\n=== TC-6.3 特殊字符文件名 ===")
special_name = "e2e_特殊字符_测试.txt"
files = {"files": (special_name, io.BytesIO("特殊字符文件名测试内容".encode()), "text/plain")}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=60)
resp = r.json()
print(f"  HTTP {r.status_code}: {resp.get('message','')[:80]}")
check("TC-6.3: 特殊字符处理", r.status_code == 200 or resp.get("status") == "fail")
# 清理
if resp.get("status") == "success":
    for f in resp.get("files", []):
        sn = f.get("stored_name")
        if sn:
            r2 = requests.post(f"{BASE}/knowledge/delete/preview", headers=headers, json={"stored_name": sn})
            if r2.json().get("status") == "success":
                ct = r2.json()["confirm_token"]
                requests.post(f"{BASE}/knowledge/delete/confirm", headers=headers, json={"confirm_token": ct, "stored_name": sn})

# === TC-6.4 伪装 exe ===
# 恶意数据意图：真实 PE 可执行文件头（MZ magic）+ .txt 扩展名，
# 仅靠扩展名白名单会放行，必须被 magic bytes 内容校验拒绝
print("\n=== TC-6.4 伪装exe (.exe→.txt) ===")
# MZ header = Windows PE executable magic bytes
exe_content = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"
files = {"files": ("disguised.txt", io.BytesIO(exe_content), "text/plain")}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=60)
resp = r.json()
print(f"  HTTP {r.status_code}: {resp.get('message','')[:80]}")
check("TC-6.4: 拒绝伪装exe", r.status_code >= 400 or resp.get("status") == "fail")

# === TC-6.5 伪装 PDF ===
# 恶意数据意图：伪造 PDF 头 %PDF- 但扩展名为 .txt，验证 magic 与扩展名不一致即拒
print("\n=== TC-6.5 伪装PDF (.pdf→.txt) ===")
pdf_content = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
files = {"files": ("disguised_pdf.txt", io.BytesIO(pdf_content), "text/plain")}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=60)
resp = r.json()
print(f"  HTTP {r.status_code}: {resp.get('message','')[:80]}")
check("TC-6.5: 拒绝伪装PDF", r.status_code >= 400 or resp.get("status") == "fail")

# === TC-6.7 对话超长输入 (4001字) ===
# 边界数据意图：4001 字符 user_input（上限 4000 +1），Pydantic Field 约束应返 422
print("\n=== TC-6.7 对话超长输入 (4001字) ===")
long_input = "A" * 4001
r = requests.post(
    f"{BASE}/chat/stream",
    headers={**headers, "Content-Type": "application/json"},
    json={"user_input": long_input, "session_id": 0},
    timeout=30,
)
resp = r.json() if r.status_code != 200 else "streamed"
print(f"  HTTP {r.status_code}")
check("TC-6.7: 4001字被拒", r.status_code == 422, f"(got {r.status_code})")

# === TC-6.8 空对话输入 ===
# 边界数据意图：空字符串 user_input（min_length 下限违例），期望 422
print("\n=== TC-6.8 空对话输入 ===")
r = requests.post(
    f"{BASE}/chat/stream",
    headers={**headers, "Content-Type": "application/json"},
    json={"user_input": "", "session_id": 0},
    timeout=30,
)
print(f"  HTTP {r.status_code}")
check("TC-6.8: 空输入被拒", r.status_code == 422, f"(got {r.status_code})")

# === TC-6.9 无效JWT ===
# 恶意数据意图：伪造的 Bearer token（非 JWT 字符串），JWT 解码失败应 401
print("\n=== TC-6.9 无效JWT ===")
bad_headers = {"Authorization": "Bearer invalidtoken123456"}
r = requests.post(
    f"{BASE}/chat/stream",
    headers={**bad_headers, "Content-Type": "application/json"},
    json={"user_input": "test", "session_id": 0},
    timeout=10,
)
print(f"  HTTP {r.status_code}")
check("TC-6.9: 无效JWT被拒", r.status_code == 401, f"(got {r.status_code})")

print(f"\n{'='*50}")
print(f"阶段五总计: {PASS} 通过, {FAIL} 失败")
print(f"{'='*50}")
