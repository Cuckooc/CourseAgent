---
name: backend-port-cluster
description: 将业务层对 infrastructure 的直接 import 按簇迁移到 app/application/ports 注册式 Port，并同步裁剪 tests/layer_guard_baseline.txt。用于继续 CourseAgent 后端分层解耦、消除 layer_guard 越层 import。不用于新增业务功能或普通 bug 修复。
---

# 后端 Port/Adapter 簇式解耦

把 `tests/layer_guard_baseline.txt` 中的越层 import 按「能力簇」（如 redis、vector_store、persistence、embeddings、document）逐簇消除。每簇一个独立提交，做完即验证，禁止一次改多簇。

## 硬约束

- Python 3.8：typing 用旧式注解（`Optional[X]`/`List[X]`），禁止 `list[dict]` 内联注解。
- ruff：行宽 100，只看 `--select E4,E7,E9,F`；存量违例导致 exit code 1 属正常，关键是**计数不增加**。
- 最小改动：优先原位替换保持行号，不顺手改风格、不删无关的存量 F401。
- 提交：`refactor(backend): …` 中文信息；用 `git commit -F .git/commit_msg_tmp.txt`，提交后删除临时文件；只 `git add` 本簇涉及文件。
- PowerShell 的 profile snapshot 数字签名报错是噪音，忽略。

## 标准流程（每簇严格按序）

1. **调研**：Grep baseline 中该簇的模块名，逐一看调用方用法——是实例化（`XxxDAO()`）、类继承（`class A(B)`）、模块函数、还是局部 import。同名类（如 `user.Information` 与 `information.Information`）要在 Port 层起区分名。
2. **建 Port**：新建 `app/application/ports/<能力>.py`，注册式服务定位器：
   - `register_xxx(impl)` 由组合根调用一次 + `get_xxx()`/门面函数未注册时抛 `RuntimeError`；
   - 模块零 app 内部依赖（只允许 `typing` 与 `core.*`）；
   - 同模块多个纯函数优先「注册实现模块对象 + 逐函数门面透传」模式（参考 ports/vector.py、ports/embeddings.py）；
   - 契约异常放 `core/exceptions.py`，Port 与 infra 各自 re-export（守卫禁 infrastructure→application）。
3. **装配**：在 `app/api/deps.py` 注册。**装配顺序陷阱**：若调用方在模块导入期/类定义期就取实现（如 `class Title(get_title_llm_cls())`），register 必须排在该 app 服务 import 之前；把该服务 import 下移到注册块之后并加 `# noqa: E402`。
4. **改调用方**：原位替换 import 与使用点；局部 import 直接换成 Port 局部导入即可，无需提升模块级。构造参数带类型注解的 DAO，删 import 后注解一并去掉（改 `param=None`）。
5. **裁 baseline**：删本簇对应行。
6. **验证四件套**（全过才算完，命令见下）。
7. **提交**，然后 `git status` 确认工作区干净。

## baseline 行号漂移规则（最容易踩的坑）

- 守卫按「路径:行号 模块」精确匹配。同文件插/删行会使**其他**违例行号漂移，baseline 必须同步修正。
- 多行 import 块替换为**等行数块**可零漂移（首选）。
- baseline 末行可能带 BOM（`﻿`），用 Edit 逐行改，不要整文件 Write（除非整簇清空或已确认内容）。
- 跑守卫后若报「新增违例」，先核对是不是已改文件的残留引用，再核对是不是行号漂移。

## 验证四件套

```powershell
$env:APP_ENV="dev"; $env:JWT_SECRET="check"; $env:METRICS_ENABLED="false"; python -c "import app.main"
python tests/layer_guard.py
pytest tests/tools/test_tool_layer_p1.py tests/test_layer_guard.py -q
python -m ruff check app/ tests/ --select E4,E7,E9,F --line-length 100 --statistics
```

ruff 总数对比上一簇：只可减少或持平；若增加，在改动文件里定位新 F401/E402 修掉（如 Port 模块 `Optional` 未使用、deps 下移 import 缺 `noqa: E402`）。

## 特殊模式

- **infra→domain 反向依赖**（如软删后清缓存）：不要让 infra 调 Port 以外的 application 模块。在 infra 侧定义 `register_xxx_hook(callable)` 回调槽，未装配时静默跳过（兼容测试直接清库），由 deps.py 组合根注入 domain 实现。
- **deps.py 自身违例**：该文件在守卫 ALLOWLIST 中，新增 infra import 无需进 baseline；baseline 里若残留其死条目（行号失实、永不被消费），可在相邻簇提交中清空。
- **同名函数跨模块**：透传时加前缀区分（如 document.file 与 text_embedding 都有 `build_chromadb` → Port 用 `file_build_chromadb`）。

## 已完成簇（勿重复处理）

llm.gateway、redis(kv/locks)、vector_store(temp/persistent)、persistence.repositories/session、embeddings(text_embedding/embedding_model/parent_child)、llm.llm_business、document(file/ocr/doc_type/two_column/multimodal)、soft_delete 回调。2026-10-08 起 baseline 为 0；守卫再报违例即真实新增，必须修复，不得直接加 baseline。
