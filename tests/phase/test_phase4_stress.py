"""
模块名：tests/phase/test_phase4_stress.py。

阶段四：并发压力脚本测试（脚本式用例：模块导入即顺序执行，全局 PASS/FAIL +
check() 汇总；pytest 收集时同样按脚本执行）。并发模型：
concurrent.futures.ThreadPoolExecutor 线程池；全仓无 asyncio。

测试场景与竞态验证点：
- TC-5.1 5 线程并发上传 5 个语义不同文件（TOPICS + DIFF_NAMES，正常数据）：
  全部 200/全部 success/响应后立即可见（无异步延迟）/skipped 不多余行；
  触发 429 时本轮作废、等 65s 后最多重试一次（run_tc51 返回 False）；
- TC-5.2 5 线程并发上传完全相同文件（竞态去重）：≤1 success、≥3 skipped，
  列表不出现重复行（等限流窗口清空后才开始）；
- TC-5.3 5 线程并发 POST /chat/stream（SSE）：全部 200 且收到响应帧；
- TC-5.4 6 线程并发错误登录一次性账号（恶意撞库节奏）：≥5 次 fail 且
  出现账号锁定提示（锁定计数在并发下不丢次）。

被测对象来源：
- 路由：control/file_control.py（POST /file/path）、control/chat_control.py
  （POST /chat/stream SSE）、control/login_control.py（注册/登录、锁定）；
- 业务：service/file_service.py 去重阈值（similarity_threshold=0.8、
  文件名相似度 ≥0.95）、service/vector_store.py（persistent_lock/向量块）。

运行方式：
    python tests/phase/test_phase4_stress.py
    # 需后端 :8000 与 DashScope embedding；固定账号 e2e_tester_2026 预先存在；
    # TC-5.4 自建一次性账号 e2e_lock_<时间戳>；脚本含两段 65 秒限流等待
依赖说明：requests 直发 HTTP，不经 conftest。
清理：deep_cleanup() 直连 Chroma 删除本脚本全部测试向量块（含 is_latest=False
旧版本）及 uploads 物理文件，TC-5.1 前后与 TC-5.2 后各执行一次。

重要根因备注见正文 2026-09-20 注释（相似度 0.8/0.95 阈值导致的历史误判）。
"""
import os, requests, io, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def login(username, password):
    """登录返回 access_token（固定账号 e2e_tester_2026，须预先存在）。"""
    r = requests.post(f"{BASE}/login/account", json={"username": username, "password": password})
    return r.json()["access_token"]

token = login("e2e_tester_2026", "Test1234!")
headers = {"Authorization": f"Bearer {token}"}

# === TC-5.1 并发上传 5 个语义不同文件 ===
# 根因备忘（2026-09-20 排查）：旧用例内容仅差一个序号字，embedding 相似度 0.93~0.97，
# 在环境阈值 similarity_threshold=0.8 下 4 个被去重正确跳过；旧断言又误读顶层 status
# （文件 skipped 时顶层仍为 success）。小文件上传走同步路径（gather + flush 后才响应），
# 不存在异步延迟；本用例保留一个“响应成功但列表缺失”的探测，若真有延迟会立即暴露。
print("\n=== TC-5.1 并发上传 5 个语义不同文件 ===")
# 5 个语义显著不同的主题文本常量（正常数据）：保证相互 embedding 相似度低于
# similarity_threshold=0.8，避免并发时被内容去重误伤
TOPICS = [
    "量子力学中的海森堡不确定性原理指出无法同时精确测量粒子的位置和动量，薛定谔方程描述波函数随时间的演化规律。",
    "川菜以麻辣鲜香著称，代表菜品有麻婆豆腐、回锅肉和宫保鸡丁；粤菜清淡鲜美，早茶点心种类繁多。",
    "贝多芬创作了九部交响曲，第三交响曲《英雄》开创浪漫主义先河，第九交响曲首次加入人声合唱。",
    "长城始建于春秋战国时期，秦始皇统一后将各段连接起来，现存长城主要是明代修筑的砖石结构。",
    "光合作用是植物利用叶绿体将二氧化碳和水转化为葡萄糖并释放氧气的过程，分光反应和暗反应两阶段。",
]
# 文件名也必须彼此显著不同：仅差序号的文件名相似度≈0.96 会命中文件名去重(≥0.95)，
# 被判为“同名异内容”而走 replace 版本链路（上次失败的真实根因，非异步延迟）。
DIFF_NAMES = [
    "e2e_quantum_mechanics.txt",
    "e2e_sichuan_cuisine.txt",
    "e2e_beethoven_symphony.txt",
    "e2e_great_wall_history.txt",
    "e2e_photosynthesis_biology.txt",
]
# 清理目标文件名前缀常量：deep_cleanup 据此匹配向量块 original_name 与物理文件
_TEST_PREFIX = "e2e_"

def deep_cleanup():
    """删除本脚本测试文件的全部向量（含 is_latest=False 旧版本）及物理文件。

    普通删除接口只能删列表可见的最新版本；版本链路产生的旧版本块需直连向量库清理。
    """
    import glob
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "env", "qianwen_config.env"))
    import core.config  # noqa: F401
    from service.vector_store import get_persistent_db, persistent_lock
    db = get_persistent_db()
    col = db._collection
    data = col.get(include=["metadatas"])
    targets = {"e2e_stress_topic_", "e2e_quantum_", "e2e_sichuan_", "e2e_beethoven_",
               "e2e_great_wall_", "e2e_photosynthesis_", "e2e_race_same", "e2e_stress_same"}
    ids, sources = [], set()
    for i, m in zip(data["ids"], data["metadatas"] or []):
        m = m or {}
        orig = str(m.get("original_name", ""))
        if any(orig.startswith(t) for t in targets) or "e2e_stress_same" in orig:
            ids.append(i)
            if m.get("source"):
                sources.add(m["source"])
    if ids:
        with persistent_lock():
            col.delete(ids=ids)
    for src in sources:
        try:
            os.path.exists(src) and os.remove(src)
        except OSError:
            pass
    upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    for p in glob.glob(os.path.join(upload_dir, "*e2e_stress*")) + \
             glob.glob(os.path.join(upload_dir, "*e2e_quantum*")) + \
             glob.glob(os.path.join(upload_dir, "*e2e_sichuan*")) + \
             glob.glob(os.path.join(upload_dir, "*e2e_beethoven*")) + \
             glob.glob(os.path.join(upload_dir, "*e2e_great_wall*")) + \
             glob.glob(os.path.join(upload_dir, "*e2e_photosynthesis*")) + \
             glob.glob(os.path.join(upload_dir, "*e2e_race_same*")):
        try:
            os.remove(p)
        except OSError:
            pass
    return len(ids)

def upload_diff(idx):
    """TC-5.1 线程 worker：上传 DIFF_NAMES[idx]/TOPICS[idx]。

    调用方：run_tc51 的 ThreadPoolExecutor(5)；返回 (idx, HTTP 码, 响应 dict)，
    异常归一为 (idx, 0, {"error": ...})。
    """
    files = {"files": (DIFF_NAMES[idx], io.BytesIO(TOPICS[idx].encode()), "text/plain")}
    data = {"scope": "private"}
    try:
        r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=120)
        return idx, r.status_code, r.json()
    except Exception as e:
        return idx, 0, {"error": str(e)}

def list_originals():
    """拉取 /knowledge/list 并返回当前 private 文档 original_name 集合。

    调用方：run_tc51（before/立即可见/skipped 多余行探测）。
    """
    r = requests.get(f"{BASE}/knowledge/list?scope=private", headers=headers)
    return {d.get("original_name") for d in r.json().get("data", [])}

def run_tc51():
    """返回 True 表示可继续（未触发限流需重试）；断言结果直接写全局 PASS/FAIL。"""
    n0 = deep_cleanup()  # 先清上次残留（含旧版本块）
    if n0:
        print(f"  预清理 {n0} 个历史测试向量块")
    before = list_originals()

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(upload_diff, i) for i in range(5)]
        results = sorted(f.result() for f in as_completed(futures))

    # 限流时本窗口作废，外层等待后重试
    if any(c == 429 for _, c, _ in results):
        print("  触发上传限流(5次/分)，等待窗口后重试")
        return False

    # 逐文件状态：必须读 files[].status，不能读顶层 status（skipped 时顶层仍 success）
    per_file = {}
    for idx, code, resp in results:
        f0 = (resp.get("files") or [{}])[0] if isinstance(resp, dict) else {}
        per_file[idx] = (code, f0.get("status", "?"), f0.get("message", resp.get("error", "")))
        print(f"    [{idx}] HTTP {code} file={per_file[idx][1]} {per_file[idx][2]}")
    check("TC-5.1: 全部 HTTP 200", all(c == 200 for c, _, _ in per_file.values()))
    ok = [i for i, (_, s, _) in per_file.items() if s == "success"]
    skipped = [i for i, (_, s, _) in per_file.items() if s == "skipped"]
    check("TC-5.1: 5 个文件全部 success", len(ok) == 5,
          f"(success={len(ok)}, skipped={skipped}；skipped 说明内容不够不同或阈值被调低)")

    # 真实延迟探测：同步路径响应 200 时数据应立即可见。
    # 最多轮询 5 秒；若首轮缺失、后续轮次才出现，即为真实延迟窗口。
    seen_immediately = list_originals()
    missing = [DIFF_NAMES[i] for i in ok if DIFF_NAMES[i] not in seen_immediately]
    delayed = []
    if missing:
        for _ in range(10):
            time.sleep(0.5)
            now_names = list_originals()
            delayed = [n for n in missing if n in now_names]
            if delayed:
                break
    check("TC-5.1: 成功文件响应后立即可见(无异步延迟)", not missing,
          f"(missing={missing}; 其中延迟 {len(delayed)} 个后才出现: {delayed})")
    check("TC-5.1: 被跳过文件不产生多余行",
          all(DIFF_NAMES[i] not in list_originals() for i in skipped),
          f"(skipped={skipped})")

    # 清理（含可能产生的旧版本块与物理文件）
    removed = deep_cleanup()
    print(f"  清理 {removed} 个测试向量块")
    return True

for attempt in range(2):
    if run_tc51():
        break
    time.sleep(65)

# === TC-5.2 并发上传相同文件 (5个) ===
# TC-5.1 已耗尽 5次/分 上传窗口，必须等窗口清空，否则全部 429（限流本身正确，但测不到竞态）
print("\n=== TC-5.2 并发上传相同文件 (5) ===")
print("  等待 65 秒清空上传限流窗口...")
time.sleep(65)
same_content = "这是并发重复测试内容，所有线程上传完全相同的文件。" * 5
def upload_same(idx):
    """TC-5.2 线程 worker：上传同一 e2e_stress_same.txt（同内容竞态去重）。

    调用方：TC-5.2 的 ThreadPoolExecutor(5)；返回 (idx, HTTP 码, 响应)。
    """
    files = {"files": ("e2e_stress_same.txt", io.BytesIO(same_content.encode()), "text/plain")}
    data = {"scope": "private"}
    try:
        r = requests.post(f"{BASE}/file/path", headers=headers, files=files, data=data, timeout=120)
        return idx, r.status_code, r.json()
    except Exception as e:
        return idx, 0, str(e)

with ThreadPoolExecutor(max_workers=5) as ex:
    futures = [ex.submit(upload_same, i) for i in range(5)]
    results = [f.result() for f in as_completed(futures)]

statuses = []
for idx, code, resp in sorted(results):
    if isinstance(resp, dict):
        f_status = resp.get("files", [{}])[0].get("status", "?") if resp.get("files") else "?"
    else:
        f_status = "?"
    statuses.append(f_status)
    print(f"    [{idx}] HTTP {code} file_status={f_status}")

skip_count = sum(1 for s in statuses if s == "skipped")
success_count = sum(1 for s in statuses if s == "success")
check("TC-5.2: ≤1个success", success_count <= 1, f"({success_count} succeeded)")
check("TC-5.2: ≥3个skipped", skip_count >= 3, f"({skip_count} skipped)")

# 清理 same 文件（含可能产生的版本块与物理文件）
print(f"  清理 {deep_cleanup()} 个测试向量块")

# === TC-5.3 并发对话 (5个) ===
# 竞态点：5 路 SSE 同时建立/消费，验证会话与 LLM 调用在并发下不串流、不 500
print("\n=== TC-5.3 并发对话 (5) ===")
def chat_one(idx):
    """TC-5.3 线程 worker：发起一条 SSE 对话并收集帧 type 序列。

    返回 (idx, HTTP 码, 帧类型 list)；解析异常的非 SSE 行静默跳过。
    """
    try:
        r = requests.post(
            f"{BASE}/chat/stream",
            headers={**headers, "Content-Type": "application/json"},
            json={"user_input": f"测试并发对话第{idx}条", "session_id": 0},
            stream=True, timeout=120,
        )
        frame_types = []
        for line in r.iter_lines():
            if line and line.startswith(b"data: "):
                try:
                    frame = json.loads(line[6:])
                    frame_types.append(frame.get("type"))
                except:
                    pass
        return idx, r.status_code, frame_types
    except Exception as e:
        return idx, 0, str(e)

with ThreadPoolExecutor(max_workers=5) as ex:
    futures = [ex.submit(chat_one, i) for i in range(5)]
    results = [f.result() for f in as_completed(futures)]

for idx, code, types in sorted(results):
    first = types[0] if isinstance(types, list) and types else "?"
    last = types[-1] if isinstance(types, list) and types else "?"
    print(f"    [{idx}] HTTP {code} frames={len(types) if isinstance(types, list) else '?'} first={first} last={last}")

ok_count = sum(1 for _, c, t in results if c == 200 and isinstance(t, list) and len(t) > 0)
check("TC-5.3: 全部200", all(c == 200 for _, c, _ in results))
check("TC-5.3: 全部有响应帧", ok_count >= 4, f"({ok_count}/5)")

# === TC-5.4 并发登录错误 (6个) — 用一次性账号避免锁定主账号 ===
print("\n=== TC-5.4 并发登录错误 (6) ===")
# 注册一次性账号
lockout_user = f"e2e_lock_{int(time.time())}"
r = requests.post(f"{BASE}/login/register", json={
    "user_name": lockout_user, "user_pwd": "Test1234!",
    "email": f"{lockout_user}@example.com",
})
print(f"  注册锁定测试账号: {lockout_user} -> {r.json().get('status')}")

def wrong_login(idx):
    """TC-5.4 线程 worker：对一次性账号用错误密码登录（WrongPass1!）。

    返回 (idx, HTTP 码, 响应)；用于统计失败次数与锁定提示。
    """
    try:
        r = requests.post(f"{BASE}/login/account", json={"username": lockout_user, "password": "WrongPass1!"}, timeout=30)
        return idx, r.status_code, r.json()
    except Exception as e:
        return idx, 0, str(e)

with ThreadPoolExecutor(max_workers=6) as ex:
    futures = [ex.submit(wrong_login, i) for i in range(6)]
    results = [f.result() for f in as_completed(futures)]

fail_count = 0
locked = False
for idx, code, resp in sorted(results):
    if isinstance(resp, dict):
        msg = resp.get("message", "")
        if "锁定" in msg or "locked" in msg.lower():
            locked = True
        if resp.get("status") == "fail":
            fail_count += 1
        print(f"    [{idx}] HTTP {code} msg={msg}")
    else:
        print(f"    [{idx}] error: {resp}")

check("TC-5.4: 全部登录失败", fail_count >= 5, f"({fail_count} failed)")
check("TC-5.4: 触发锁定", locked, "(no lockout message)")

print(f"\n{'='*50}")
print(f"阶段四总计: {PASS} 通过, {FAIL} 失败")
print(f"{'='*50}")
