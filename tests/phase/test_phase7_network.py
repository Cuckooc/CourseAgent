"""
模块名：tests/phase/test_phase7_network.py。

阶段七：网络中断与恢复脚本测试（脚本式用例：模块导入即顺序执行，全局 PASS/FAIL +
check() 汇总；pytest 收集时同样按脚本执行）。风险类型：SSE 断连后半开会话、
异常 session_id 导致的崩溃、跨用户会话越权访问。

测试场景清单（数据意图见正文行内注释）：
TC-7.1 完整对话（正常）收到 done 帧后调 POST /chat/recover → 200、
       recover_status=completed、回补 ai_output 非空；
TC-7.2 流式中途断连（边界/故障模拟）：只读 2 帧即 r.close() 模拟客户端掉线，
       随后用不存在 session_id 探测 recover 容错（200/400/404，不 500）；
TC-7.3 跨用户恢复（恶意/越权）：用户 B 携带自己的 JWT 恢复用户 A 的会话，
       期望 403/404 或 status=fail；
TC-7.4 异常 session_id（边界：0 与 -1）调 recover，期望 200/400/404/422 优雅处理。

被测对象来源：
- 路由：app/api/v1/chat.py（POST /chat/stream SSE、POST /chat/recover
  会话恢复与状态判定 completed/missing、/history 本脚本未直接调用）；
- 业务：app/application/chat/chat_service.py 的会话状态机与归属校验（user_id 隔离）。

运行方式：
    python tests/phase/test_phase7_network.py
    # 需后端 :8000 与真实 LLM（SSE 帧由模型真实产出）；
    # 自建临时账号 e2enet<时间戳>/e2enet<时间戳+1>，密码 Test1234!
依赖说明：requests stream=True 直发 HTTP，不经 conftest。
清理：临时账号不删除；对话记录留在其会话历史中（账号仅本脚本使用）。
"""
import requests
import json
import time

BASE = "http://127.0.0.1:8000"  # 后端基址常量（脚本直连）
PASS = FAIL = 0  # 全局通过/失败断言计数

def check(name, cond, extra=""):
    """断言辅助：累加全局 PASS/FAIL 并打印，不抛异常。调用方：本脚本全部 TC。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")

def register_login(tag):
    """注册并登录临时账号 e2enet<tag>，返回 (用户名, access_token)。

    调用方：TC-7.1 前建用户 A、TC-7.3 建用户 B（tag+1 保证用户名不同）。
    """
    u = f"e2enet{tag}"
    requests.post(f"{BASE}/login/register", json={"user_name": u, "user_pwd": "Test1234!", "email": f"{u}@ex.com"})
    r = requests.post(f"{BASE}/login/account", json={"username": u, "password": "Test1234!"})
    return u, r.json()["access_token"]

def stream_chat(token, question, read_all=True, max_frames=None, hard_close=False):
    """发起 SSE 对话并收集帧。

    read_all=True（正常）：读到流结束，finally 中 r.close()，返回全部帧；
    read_all=False（故障模拟）：读到 max_frames（默认 2）帧即 break 硬断开，
    模拟客户端网络掉线，用于 TC-7.2 的半开会话场景。
    """
    r = requests.post(f"{BASE}/chat/stream",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"user_input": question, "session_id": 0}, stream=True, timeout=60)
    frames = []
    try:
        for line in r.iter_lines():
            if line and line.startswith(b"data: "):
                frame = json.loads(line[6:])
                frames.append(frame)
                if not read_all and len(frames) >= (max_frames or 2):
                    break
    finally:
        r.close()
    return frames

def recover(token, session_id):
    """调 POST /chat/recover 恢复指定会话，返回 (HTTP 码, 响应 dict)。

    调用方：TC-7.1（本人完整会话）、TC-7.2（不存在 sid 容错）、
    TC-7.3（他人 sid 越权）、TC-7.4（0/负数边界）。
    """
    r = requests.post(f"{BASE}/chat/recover",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"session_id": session_id}, timeout=30)
    return r.status_code, r.json()

tag = int(time.time()) % 1000000
print("注册用户 A ...")
userA, tokenA = register_login(tag)
print(f"  userA={userA}")

# === TC-7.1 完整对话 → recover completed ===
print("\n=== TC-7.1 完整对话后恢复 ===")
frames = stream_chat(tokenA, "什么是光合作用？")
done = [f for f in frames if f.get("type") == "done"]
check("7.1: 收到 done 帧", len(done) == 1, f"(frames types={[f['type'] for f in frames][:5]})")
if done:
    sid = done[0]["session_id"]
    print(f"  session_id={sid}")
    code, resp = recover(tokenA, sid)
    rstatus = resp.get("data", {}).get("recover_status")
    check("7.1: recover HTTP 200", code == 200)
    check("7.1: recover_status=completed", rstatus == "completed", f"(got {rstatus})")
    check("7.1: 回补 ai_output 非空", bool(resp.get("data", {}).get("ai_output")))

# === TC-7.2 流式中途断连 → recover 优雅降级 ===
print("\n=== TC-7.2 流式中途断连 ===")
# 用较长问题，读 2 帧（status + 第一个delta）后立即断开
frames = stream_chat(tokenA, "请详细论述牛顿三大运动定律并各举一个生活中的应用例子", read_all=False, max_frames=2)
got_partial = len(frames) >= 1
check("7.2: 中断前收到部分帧", got_partial, f"(got {len(frames)})")
# 新会话 session_id 在 done 帧才返回；中断时前端拿不到 sid，
# 这里用 history 查最新会话验证后端无崩溃（稍等让服务器写完或感知断开）
time.sleep(3)
# 用一个大的 session_id 探测 recover 容错
code, resp = recover(tokenA, 99999999)
check("7.2: 不存在会话恢复不崩溃", code in (200, 400, 404))
print(f"    recover(99999999) → HTTP {code} status={resp.get('status')}")

# === TC-7.3 跨用户恢复他人会话 → 拒绝 ===
print("\n=== TC-7.3 跨用户恢复隔离 ===")
if done:
    sid = done[0]["session_id"]
    print("注册用户 B ...")
    time.sleep(2)
    userB, tokenB = register_login(tag + 1)
    code, resp = recover(tokenB, sid)
    msg = resp.get("message", "")
    print(f"    userB recover userA session {sid} → HTTP {code} msg={msg}")
    check("7.3: 跨用户恢复被拒", code in (403, 404) or resp.get("status") == "fail")

# === TC-7.4 无效会话 id（0 / 负数）容错 ===
print("\n=== TC-7.4 异常 session_id 容错 ===")
for bad_sid in [0, -1]:
    code, resp = recover(tokenA, bad_sid)
    check(f"7.4: session_id={bad_sid} 优雅处理", code in (200, 400, 404, 422), f"(got {code})")

print(f"\n{'='*50}\n网络中断测试: {PASS} 通过, {FAIL} 失败\n{'='*50}")
