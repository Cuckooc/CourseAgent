"""
模块名：tests/phase/test_phase6_roles.py。

阶段六：身份角色权限脚本测试（脚本式用例：模块导入即顺序执行，全局 PASS/FAIL +
check() 汇总；pytest 收集时同样按脚本执行）。风险类型：水平/垂直越权。

测试场景：同一受控账号 e2erole<时间戳> 依次切换 user / teacher / admin 三种角色，
每切换一次用 fresh_token() 重新登录取新 JWT（角色以库内 role 字段为准）：
- user：GET /admin/users → 403；公共库上传 → 403/fail；对话放行（非 403）
- teacher：GET /admin/users → 403；公共库上传 → 200 success；对话放行
- admin：GET /admin/users → 200；对话被业务规则禁用 → 403
角色切换手段：set_role 经 db/session.session_scope 直连 MySQL UPDATE
user_information.role（不走 API），测后无条件恢复为 user。

被测对象来源：
- 路由：app/api/v1/admin.py（GET /admin/users，admin only）、
  app/api/v1/files.py（POST /file/path scope=public，teacher/admin）、
  app/api/v1/chat.py（POST /chat/stream，admin 禁用）；
- 守卫：core/deps.py 的 require_role/角色判定（依据 JWT 内/库内 role）。

运行方式：
    python tests/phase/test_phase6_roles.py
    # 需后端 :8000 与真实 MySQL（脚本直连库改角色）；脚本运行目录依赖
    # sys.path.insert 与 load_dotenv（见文件头，加载 env/config.env 与
    # env/qianwen_config.env），这些引导代码不可改动
依赖说明：requests 直发 HTTP + SQLAlchemy 直连库；不经 conftest。
清理：受控账号测后恢复 user 角色但不删除；teacher 上传的公共文件因 user 身份
无删除权限而保留，需后续统一清理（cleanup_public 当前为空占位）。
"""
import sys, os, io, time, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "env", "config.env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "env", "qianwen_config.env"))
import core.config  # noqa
from app.infrastructure.persistence.session import session_scope
from sqlalchemy import text

BASE = "http://127.0.0.1:8000"  # 后端基址常量（脚本直连）
PASS = FAIL = 0  # 全局通过/失败断言计数

def check(name, cond, extra=""):
    """断言辅助：累加全局 PASS/FAIL 并打印，不抛异常。调用方：本脚本全部检查点。"""
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {name}")
    else:
        FAIL += 1; print(f"  [FAIL] {name} {extra}")

def set_role(user_id, role):
    """直连数据库把指定用户角色改为 user/teacher/admin。

    参数来源：user_id 为受控账号 uid；role 为目标角色字符串。
    注意：不走业务 API，改后 sleep 0.5s 等变更落库生效；无返回值。
    """
    with session_scope() as s:
        s.execute(text("UPDATE user_information SET role=:r WHERE id=:id"), {"r": role, "id": user_id})
    time.sleep(0.5)  # 角色即时生效，短暂等待

# --- 准备受控账号：注册临时账号并直连库查出 user_id（后续 set_role 的目标）---
uname = f"e2erole{int(time.time())%1000000}"
email = f"{uname}@ex.com"
r = requests.post(f"{BASE}/login/register", json={"user_name": uname, "user_pwd": "Test1234!", "email": email})
print(f"注册受控账号 {uname}: {r.json().get('status')}")
uid = None
with session_scope() as s:
    row = s.execute(text("SELECT id FROM user_information WHERE user_name=:n"), {"n": uname}).mappings().first()
    uid = row["id"]
print(f"user_id={uid}")

def fresh_token():
    """用受控账号重新登录，返回全新 access_token（每次切角色后必须重取，避免旧 JWT 缓存角色）。"""
    r = requests.post(f"{BASE}/login/account", json={"username": uname, "password": "Test1234!"})
    return r.json()["access_token"]

def admin_users_list(token):
    """探针：以 token 调 GET /admin/users，返回 Response（200=放行，403=越权拒绝）。"""
    return requests.get(f"{BASE}/admin/users", headers={"Authorization": f"Bearer {token}"}, timeout=30)

def chat_probe(token):
    """探针：以 token 发起 SSE 对话探测前置守卫。

    不等待完整流（调用方立即 close）：403 会在 SSE 建立前立即返回；
    放行则流开始建立（admin 角色预期被业务规则 403 禁用）。
    """
    # 不实际等待完整流，只看前置守卫是否放行（403 会立即返回；放行则开始 SSE）
    return requests.post(f"{BASE}/chat/stream",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"user_input": "权限探测", "session_id": 0}, stream=True, timeout=15)

def public_upload(token):
    """探针：以 token 向公共库（scope=public）上传 e2e_pub_<账号>.txt，返回 Response。

    预期：user 403/fail；teacher/admin success。
    """
    files = {"files": (f"e2e_pub_{uname}.txt", io.BytesIO("公共知识库权限测试内容".encode()), "text/plain")}
    return requests.post(f"{BASE}/file/path", headers={"Authorization": f"Bearer {token}"},
                         files=files, data={"scope": "public"}, timeout=60)

# 清理公共上传文件（空占位：user 身份无权删公共文件，当前留待人工/后续统一清理）
def cleanup_public(token_admin=None):
    pass

# ========== 角色 1: user ==========
print("\n=== 角色 user ===")
set_role(uid, "user")
token = fresh_token()

r = admin_users_list(token)
check("user: GET /admin/users → 403", r.status_code == 403, f"(got {r.status_code})")

r = public_upload(token)
resp = r.json()
check("user: 上传公共库 → 403", r.status_code == 403 or resp.get("status") == "fail", f"(got {r.status_code} {resp.get('message','')})")

# user 对话应放行（SSE 开始，可能 200）
try:
    r = chat_probe(token)
    code = r.status_code
    r.close()
    check("user: 对话 → 非403", code != 403, f"(got {code})")
except Exception as e:
    check("user: 对话 → 非403", False, str(e))

# ========== 角色 2: teacher ==========
print("\n=== 角色 teacher ===")
set_role(uid, "teacher")
token = fresh_token()

r = admin_users_list(token)
check("teacher: GET /admin/users → 403", r.status_code == 403, f"(got {r.status_code})")

r = public_upload(token)
resp = r.json()
check("teacher: 上传公共库 → 成功", r.status_code == 200 and resp.get("status") == "success",
      f"(got {r.status_code} {resp.get('message','')})")
print(f"    public upload: {resp.get('message','')}")

try:
    r = chat_probe(token)
    code = r.status_code
    r.close()
    check("teacher: 对话 → 非403", code != 403, f"(got {code})")
except Exception as e:
    check("teacher: 对话 → 非403", False, str(e))

# ========== 角色 3: admin ==========
print("\n=== 角色 admin ===")
set_role(uid, "admin")
token = fresh_token()

r = admin_users_list(token)
check("admin: GET /admin/users → 200", r.status_code == 200, f"(got {r.status_code})")

try:
    r = chat_probe(token)
    code = r.status_code
    try:
        body = r.json()
        msg = body.get("message", "")
    except Exception:
        msg = ""
    r.close()
    check("admin: 对话 → 403 禁用", code == 403, f"(got {code} {msg})")
except Exception as e:
    check("admin: 对话 → 403 禁用", False, str(e))

# ========== 恢复 user 并清理公共测试文件 ==========
print("\n=== 清理 ===")
set_role(uid, "user")
# 用当前 user 身份无法删公共文件（需 owner/admin），保留文件由后续统一清理
print(f"  已将 {uname} 恢复为 user 角色")

print(f"\n{'='*50}\n角色权限测试: {PASS} 通过, {FAIL} 失败\n{'='*50}")
