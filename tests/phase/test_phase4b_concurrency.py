"""
模块名：tests/phase/test_phase4b_concurrency.py。

阶段四（修正版）：并发上传脚本测试 — 使用语义完全不同的内容避免误触去重；
尊重 5次/分钟/user 限流节奏，分批并发（脚本式用例：模块导入即顺序执行，
全局 PASS/FAIL + check() 汇总；pytest 收集时同样按脚本执行）。

并发模型：concurrent.futures.ThreadPoolExecutor 线程池（5/4 worker）并发调用
POST /file/path；无 asyncio。

测试场景与竞态验证点：
- TC-5.1b：5 个语义不同主题文件并发（TOPICS 常量，物理/美食/音乐/历史/生物），
  竞态点：全部 HTTP 200、5 个均 success 入库、列表恰增 5（无去重误伤、无丢失）；
- TC-5.2b：同一文件 4 线程并发（先 sleep 65s 清空上传限流窗口），
  竞态点：并发去重只有 ≤1 个 success、其余 skipped，列表仅 1 行
  （唯一约束/去重检查在并发下不产生重复行）。

被测对象来源：
- 路由：control/file_control.py（POST /file/path）、
  control/knowledge_control.py（/knowledge/list、/knowledge/delete/preview|confirm）；
- 业务：service/file_service.py 去重判定（相似度阈值）与上传限流
  （core/deps.py，5 次/分钟/user）。

运行方式：
    python tests/phase/test_phase4b_concurrency.py
    # 需后端 :8000、DashScope embedding；固定账号 e2e_tester_2026 预先存在；
    # 脚本内含 65 秒限流窗口等待，总耗时约 80 秒
依赖说明：requests 直发 HTTP，不经 conftest。
清理：cleanup() 经「删除预览→确认」两步 API 删除 e2e_topic*/e2e_race_same* 文档，
TC-5.1b 前后各清一次，TC-5.2b 结束再清。
"""
import requests, io, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:8000"  # 后端基址常量（脚本直连）
PASS = FAIL = 0  # 全局通过/失败断言计数

def check(name, cond, extra=""):
    """断言辅助：累加全局 PASS/FAIL 并打印，不抛异常。调用方：本脚本全部 TC。"""
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {name}")
    else:
        FAIL += 1; print(f"  [FAIL] {name} {extra}")

r = requests.post(f"{BASE}/login/account", json={"username": "e2e_tester_2026", "password": "Test1234!"})
token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# 5 个语义完全不同主题的内容常量（正常数据）：确保彼此 embedding 相似度远低于
# 去重阈值，避免并发上传时被误判 skipped；每个主题 ×3 增加文本长度
TOPICS = [
    "量子力学中的海森堡不确定性原理指出，无法同时精确测量粒子的位置和动量。薛定谔方程描述波函数随时间的演化。" * 3,  # 物理
    "川菜以麻辣鲜香著称，代表菜品有麻婆豆腐、回锅肉和宫保鸡丁。粤菜则清淡鲜美，早茶点心种类繁多。" * 3,            # 美食
    "贝多芬创作了九部交响曲，其中第三交响曲《英雄》开创了浪漫主义音乐的先河，第九交响曲加入了人声合唱。" * 3,        # 音乐
    "长城始建于春秋战国时期，秦始皇统一中国后将各段长城连接起来。现存长城主要是明代修筑的砖石结构。" * 3,            # 历史
    "光合作用是植物利用叶绿体将二氧化碳和水转化为葡萄糖并释放氧气的过程，分为光反应和暗反应两个阶段。" * 3,          # 生物
]

def cleanup(prefix):
    """按文件名前缀清理知识库文档：两步删除（preview 取 confirm_token → confirm 确认）。

    调用方：TC-5.1b 执行前/后、TC-5.2b 执行后。参数来源：prefix 为文件名前缀
    （e2e_topic / e2e_race_same）。返回去向：无，副作用为删除向量块与物理文件。
    """
    r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
    for d in r.json().get("data", []):
        if prefix in d.get("filename", ""):
            sn = d["filename"]
            r2 = requests.post(f"{BASE}/knowledge/delete/preview", headers={**headers, "Content-Type": "application/json"}, json={"stored_name": sn})
            if r2.json().get("status") == "success":
                ct = r2.json()["confirm_token"]
                requests.post(f"{BASE}/knowledge/delete/confirm", headers={**headers, "Content-Type": "application/json"}, json={"confirm_token": ct, "stored_name": sn})

# === TC-5.1b 5 个语义不同文件并发 ===
print("=== TC-5.1b 5个语义不同文件并发上传 ===")
cleanup("e2e_topic")
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
before = len(r.json().get("data", []))
print(f"  上传前列表数: {before}")

def upload_topic(idx):
    """线程 worker：并发上传 TOPICS[idx] 对应的 e2e_topic_{idx}.txt。

    调用方：TC-5.1b 的 ThreadPoolExecutor。参数来源：idx 0~4 同时索引主题与文件名；
    返回去向：(idx, HTTP 状态码, 响应 dict)，异常归一为 (idx, 0, {"error": ...})。
    """
    files = {"files": (f"e2e_topic_{idx}.txt", io.BytesIO(TOPICS[idx].encode()), "text/plain")}
    try:
        r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data={"scope": "private"}, timeout=120)
        return idx, r.status_code, r.json()
    except Exception as e:
        return idx, 0, {"error": str(e)}

with ThreadPoolExecutor(max_workers=5) as ex:
    results = sorted([f.result() for f in as_completed([ex.submit(upload_topic, i) for i in range(5)])])

new_count = 0
for idx, code, resp in results:
    fst = resp.get("files", [{}])[0].get("status", "?") if isinstance(resp, dict) else resp
    if fst == "success":
        new_count += 1
    print(f"    [{idx}] HTTP {code} file={fst}")
check("TC-5.1b: 全部HTTP 200", all(c == 200 for _, c, _ in results))
check("TC-5.1b: 5个均成功入库", new_count == 5, f"(success={new_count})")

time.sleep(3)
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
after = len(r.json().get("data", []))
print(f"  上传后列表数: {after} (delta={after-before})")
check("TC-5.1b: 列表增加5", after - before == 5, f"(delta={after-before})")
cleanup("e2e_topic")

# === TC-5.2b 同文件并发（限流节奏：等待窗口后 4 并发，留 1 次余量）===
print("\n=== TC-5.2b 同文件并发去重 ===")
print("  等待 65 秒清空上传限流窗口...")
time.sleep(65)
# 输入意图（并发去重竞态）：所有线程上传同一文件名、同一内容，
# 验证「先查重后插入」窗口在并发下仍只入库一份
same = "并发去重竞争条件测试：同一时刻多个线程上传完全相同的文件，验证只有一个成功。" * 3
def upload_same(idx):
    """线程 worker：4 线程上传完全相同的 e2e_race_same.txt。

    调用方：TC-5.2b 的 ThreadPoolExecutor(4)；返回同 upload_topic 的三元组。
    """
    files = {"files": ("e2e_race_same.txt", io.BytesIO(same.encode()), "text/plain")}
    try:
        r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data={"scope": "private"}, timeout=120)
        return idx, r.status_code, r.json()
    except Exception as e:
        return idx, 0, {"error": str(e)}

with ThreadPoolExecutor(max_workers=4) as ex:
    results = sorted([f.result() for f in as_completed([ex.submit(upload_same, i) for i in range(4)])])

succ = skp = other = 0
for idx, code, resp in results:
    fst = resp.get("files", [{}])[0].get("status", "?") if isinstance(resp, dict) and resp.get("files") else f"HTTP{code}"
    if fst == "success": succ += 1
    elif fst == "skipped": skp += 1
    else: other += 1
    print(f"    [{idx}] HTTP {code} file={fst}")
check("TC-5.2b: 成功数≤1", succ <= 1, f"(success={succ})")
check("TC-5.2b: 去重生效(skipped≥2 或 1成功其余跳过)", succ + skp >= 3 and skp >= 1, f"(success={succ}, skipped={skp})")

time.sleep(2)
r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
same_rows = [d for d in r.json().get("data", []) if d.get("original_name") == "e2e_race_same.txt"]
check("TC-5.2b: 列表只有1行", len(same_rows) == 1, f"(rows={len(same_rows)})")
cleanup("e2e_race_same")

print(f"\n{'='*50}\n阶段四修正版: {PASS} 通过, {FAIL} 失败\n{'='*50}")
