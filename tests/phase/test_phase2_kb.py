"""
模块名：tests/phase/test_phase2_kb.py。

阶段二：知识库上传 / 去重 / 版本管理 API 端到端脚本测试（脚本式用例：无 pytest
test_ 函数，模块导入即顺序执行，通过全局 PASS/FAIL 计数器与 check() 汇总，
进程退出码反映成败；pytest 收集时亦按脚本执行）。

测试场景（TC 清单，输入/意图/预期见正文各段注释）：
TC-3.1 首次上传（正常）→ status=success、files[0].status=success、列表 +1
TC-3.2 同内容重复上传（去重/恶意重复）→ files[0].status=skipped、列表不变
TC-3.3 同名不同内容（边界：版本更新）→ success 且列表仅保留 1 个最新版本
TC-3.4 dedup_strategy=full（正常：全量相似度去重）→ skipped
TC-3.5 update_strategy=replace（正常：替换策略）→ success 且列表仍 1 行

被测对象来源：
- 路由：app/api/v1/files.py（POST /file/path，scope/dedup_strategy/update_strategy
  表单参数）、app/api/v1/knowledge.py（GET /knowledge/list）；
- 业务：app/application/files/file_service.py 与 app/application/knowledge/knowledge_service.py 的去重
  （full/content/filename 策略）与版本（version/replace）链路、embedding 向量化。

运行方式：
    python tests/phase/test_phase2_kb.py
    # 需后端 :8000、DashScope embedding 可用；前置条件：固定账号 e2e_tester_2026
    # （密码 Test1234!）已在库中，脚本不负责建号
依赖说明：用 requests 直发 HTTP（不经 conftest 夹具）；PASS/FAIL 仅打印汇总。
清理：依赖后端去重跳过与版本替换语义，不额外删除文档（同一文件始终 1 行）。
TC-3.1 首次上传 → TC-3.2 重复跳过 → TC-3.3 同名不同内容版本更新 → TC-3.4 full策略 → TC-3.5 replace策略
"""
import requests, io, json, time

BASE = "http://127.0.0.1:8000"  # 后端基址常量（脚本直连，不经 conftest）
PASS = 0  # 全局通过断言计数（check 累加，末尾打印汇总）
FAIL = 0  # 全局失败断言计数（非 0 即视为脚本失败）

def check(name, cond, extra=""):
    """断言辅助：按 cond 累加全局 PASS/FAIL 并打印单条结果，不抛异常（保证脚本跑完）。

    调用方：本脚本全部 TC 段。参数来源：name 为用例名，cond 为实际布尔判定，
    extra 为失败时的现场值（如 before/after 计数）。无返回值。
    """
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")

def login(username, password):
    """登录并返回 access_token 字符串；失败直接 assert 终止（后续用例无法进行）。

    调用方：脚本启动段。参数来源：固定测试账号 e2e_tester_2026（须预先存在）。
    """
    r = requests.post(f"{BASE}/login/account", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.text}"
    return r.json()["access_token"]

# --- 登录 ---
token = login("e2e_tester_2026", "Test1234!")
headers = {"Authorization": f"Bearer {token}"}
print(f"[login] token len={len(token)}")

# --- 获取上传前列表计数 ---
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
before_count = len([d for d in r.json().get("data", []) if d.get("scope") == "private"])
print(f"[before] private docs count={before_count}")

# === TC-3.1 首次上传 ===
# 输入意图（正常）：人工智能主题长文本（×5 保证切片量），首次出现应入库成功
print("\n=== TC-3.1 首次上传 ===")
content_v1 = "人工智能是计算机科学的一个重要分支，它研究如何让机器表现出智能行为。机器学习是AI的核心技术，深度学习是机器学习的子集。" * 5
files = {"files": ("e2e_kb_v1.txt", io.BytesIO(content_v1.encode()), "text/plain")}
data = {"scope": "private"}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=120)
resp = r.json()
print(f"  response: {resp}")
check("TC-3.1: status=success", resp.get("status") == "success")
check("TC-3.1: files[0].status=success", resp.get("files", [{}])[0].get("status") == "success")
time.sleep(2)

# 验证列表
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
after_count = len([d for d in r.json().get("data", []) if d.get("scope") == "private"])
check("TC-3.1: 列表+1", after_count == before_count + 1, f"(before={before_count}, after={after_count})")

# === TC-3.2 重复上传（应跳过）===
# 输入意图（重复/去重）：与 TC-3.1 字节级相同的内容与文件名，期望命中去重直接 skipped
print("\n=== TC-3.2 重复上传跳过 ===")
files = {"files": ("e2e_kb_v1.txt", io.BytesIO(content_v1.encode()), "text/plain")}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=120)
resp = r.json()
print(f"  response: {resp}")
check("TC-3.2: files[0].status=skipped", resp.get("files", [{}])[0].get("status") == "skipped")
check("TC-3.2: message含跳过", "跳过" in resp.get("message", ""))
# 验证列表不变
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
count_after_dup = len([d for d in r.json().get("data", []) if d.get("scope") == "private"])
check("TC-3.2: 列表不变", count_after_dup == after_count)

# === TC-3.3 同名不同内容 → 版本更新 ===
# 输入意图（边界）：文件名相同但主题完全不同（AI→数据库），触发版本更新而非新增行
print("\n=== TC-3.3 同名不同内容版本更新 ===")
content_v2 = "数据库系统是计算机科学的核心课程，关系型数据库使用SQL语言进行数据操作。NoSQL数据库提供灵活的数据模型。分布式数据库通过分片实现可扩展性。" * 5
files = {"files": ("e2e_kb_v1.txt", io.BytesIO(content_v2.encode()), "text/plain")}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=120)
resp = r.json()
print(f"  response: {resp}")
check("TC-3.3: files[0].status=success", resp.get("files", [{}])[0].get("status") == "success")
# 验证列表仅 1 行（最新版本）
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
private_list = [d for d in r.json().get("data", []) if d.get("scope") == "private"]
v1_items = [d for d in private_list if d.get("original_name") == "e2e_kb_v1.txt"]
check("TC-3.3: 列表仅1行e2e_kb_v1", len(v1_items) == 1, f"(found {len(v1_items)})")
check("TC-3.3: 列表数量不变", len(private_list) == after_count)

# === TC-3.4 去重策略 full ===
# 输入意图（正常）：显式 dedup_strategy=full 上传当前库内已有内容，期望全量相似命中 skipped
print("\n=== TC-3.4 去重策略full ===")
files = {"files": ("e2e_kb_v1.txt", io.BytesIO(content_v2.encode()), "text/plain")}
data_full = {"scope": "private", "dedup_strategy": "full"}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data_full, timeout=120)
resp = r.json()
print(f"  response: {resp}")
check("TC-3.4: full策略skip", resp.get("files", [{}])[0].get("status") == "skipped")

# === TC-3.5 更新策略 replace ===
# 输入意图（正常）：再次换主题（操作系统）并显式 update_strategy=replace，
# 期望旧内容被替换、成功且列表仍只 1 行
print("\n=== TC-3.5 更新策略replace ===")
content_v3 = "操作系统是管理计算机硬件资源的系统软件，进程调度、内存管理和文件系统是其核心功能。Linux是开源的类Unix操作系统。" * 5
files = {"files": ("e2e_kb_v1.txt", io.BytesIO(content_v3.encode()), "text/plain")}
data_replace = {"scope": "private", "update_strategy": "replace"}
r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data_replace, timeout=120)
resp = r.json()
print(f"  response: {resp}")
check("TC-3.5: replace策略success", resp.get("files", [{}])[0].get("status") == "success")
# 验证列表仍 1 行
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
private_list = [d for d in r.json().get("data", []) if d.get("scope") == "private"]
v1_items = [d for d in private_list if d.get("original_name") == "e2e_kb_v1.txt"]
check("TC-3.5: replace后列表仍1行", len(v1_items) == 1, f"(found {len(v1_items)})")

print(f"\n{'='*50}")
print(f"阶段二总计: {PASS} 通过, {FAIL} 失败")
print(f"{'='*50}")
