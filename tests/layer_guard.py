"""
分层 import 守卫：扫描 app/ 下跨层依赖，禁止新增超出 baseline 的违例。

使用方式：
    python tests/layer_guard.py

退出码：
    0 — 无新增违例（或仅基线内已知违例）
    1 — 发现新增越层 import，需修复或更新 baseline

规则（白名单基线）：
    - api 禁 import app.infrastructure
    - application 禁 import app.infrastructure
    - domain 禁 import app.infrastructure
    - auth 禁 import app.infrastructure
    - infrastructure 禁 import app.application / app.domain / app.auth
"""
import ast
import os
import sys

RULES = {
    "application": ["app.infrastructure"],
    "domain": ["app.infrastructure"],
    "auth": ["app.infrastructure"],
    "api": ["app.infrastructure"],
    "infrastructure": ["app.application", "app.domain", "app.auth"],
}

# 组合根例外：app/api/deps.py 负责装配 Port↔Adapter，允许 import infrastructure
ALLOWLIST = {
    "app/api/deps.py",
}

BASELINE = set()
BASELINE_PATH = os.path.join(os.path.dirname(__file__), "layer_guard_baseline.txt")
if os.path.exists(BASELINE_PATH):
    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        BASELINE = {ln.strip() for ln in f if ln.strip()}


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _banned_imports(path, layer):
    # type: (str, str) -> list[tuple[str, int, str]]
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.Import):
            for a in node.names:
                mod = a.name
                if not mod:
                    continue
                for banned in RULES.get(layer, []):
                    if mod == banned or mod.startswith(banned + "."):
                        hits.append((os.path.relpath(path, ROOT).replace("\\", "/"), node.lineno, mod))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not mod:
                continue
            for banned in RULES.get(layer, []):
                if mod == banned or mod.startswith(banned + "."):
                    hits.append((os.path.relpath(path, ROOT).replace("\\", "/"), node.lineno, mod))
    return hits


def main():
    os.chdir(ROOT)  # 保证 rel path 与 baseline 一致
    new_hits = []
    for layer in RULES:
        base = os.path.join(ROOT, "app", layer)
        if not os.path.isdir(base):
            continue
        for dirpath, _, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                for fpath, lineno, mod in _banned_imports(p, layer):
                    key = "{}:{} {}".format(fpath, lineno, mod)
                    if key not in BASELINE and fpath not in ALLOWLIST:
                        new_hits.append(key)
    if new_hits:
        print("新增越层 import 违例（禁止新增，需修复或更新 tests/layer_guard_baseline.txt）：")
        for h in sorted(set(new_hits)):
            print("  " + h)
        sys.exit(1)
    print("分层 import 检查通过：无新增越层违例（当前基线 {} 条已知违例）".format(len(BASELINE)))
    sys.exit(0)


if __name__ == "__main__":
    main()
