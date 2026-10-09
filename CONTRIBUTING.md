# 贡献指南

感谢参与 CourseAgent！本文档说明本地开发、代码规范、测试与提交流程，遵循这些约定可以让你的 PR 快速通过评审。

## 1. 开发环境

```bash
# 后端（Python 3.8+）
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux
pip install -r requirements.txt

# 配置（从案例模板复制，禁止把真实配置提交入库）
cp env/config.env.example env/config.env
cp env/qianwen_config.env.example env/qianwen_config.env

# 初始化数据库
mysql -u root -p < course.sql

# 启动
uvicorn app.main:app --reload          # 后端 :8000
cd web/frontend && npm install && npm run dev   # 前端
```

依赖版本**必须锁定**：新增依赖时在 requirements.txt 写明 `==` 精确版本，并确认兼容 Python 3.8。

## 2. 架构与分层约束

```
control（路由/鉴权/校验）→ service（业务编排）
   → multi_agent / tools（智能体与工具）→ dao（数据访问）→ db（模型/连接）
                                                          ↘ memory（记忆体系）
```

**强制规则**：

- **单向依赖**：control 只能调 service；service 可调 multi_agent/tools/dao/memory；dao 不反向依赖 service/control。发现需要反向调用时，应通过回调/事件重构，而不是直接 import。
- **控制器瘦身**：control 不写业务逻辑，只做请求校验、`Depends` 注入鉴权、调用 service、返回结果。
- **业务失败抛异常**：可预期的业务错误抛 `core/responses.py` 的 `BizException`，由 `control/app.py` 全局处理器统一转响应；**不要在各接口手工拼 `{"status":"fail"}`**，也不要 `except: pass` 吞异常。
- **重对象单例化**：LLM/Chroma/服务类等重对象使用 `lru_cache` 单例工厂（参照 `get_knowledge_service()`），禁止在每请求路径里重复构建。
- **降级优先**：Redis 等外部依赖不可用时 `core/redis_client.py` 返回 None，调用方必须提供内存降级路径，不得让单点故障阻断服务。

## 3. 代码规范

### 3.1 Python

- 风格门禁：`ruff check .`（配置见 [ruff.toml](ruff.toml)：行宽 100、`target-version = "py38"`、E4/E7/E9/F 规则集）。提交前必须本地跑通。
- **Python 3.8 兼容**：不使用 3.9+ 语法（`match` 语句、`X | Y` 类型注解、内置泛型 `list[int]` 等）；类型注解用 `typing.List/Optional/Union`。
- 文件头统一使用四段式 docstring：

```python
# -*- coding: utf-8 -*-
"""
模块名：service.xxx_service
作用：一句话说明模块职责（必要时展开关键行为与边界）。

主要成员：
- XxxService：职责概述。
- XxxService.method()：关键方法说明。
- get_xxx_service()：单例工厂。

被谁使用：
- control/xxx_control.py：以何种方式调用、服务哪个接口。
"""
```

- 函数/类 docstring 说明「做什么、参数、返回、异常」；注释解释**为什么**，不要复述代码。
- 日志使用 `core/logging_config.py` 统一配置的 logger；**严禁打印密码、token、用户对话原文等 PII**（历史教训见框架审计 S7）。
- 新增可观测点时透传 `request_id`（`core/trace.py` 的 contextvar）。

### 3.2 配置与密钥

- 所有部署差异走环境变量 → `core/config.py` 的 `settings` 单例，禁止在代码里硬编码连接串/密钥。
- 新增配置项必须同步更新 `env/config.env.example`（或 qianwen 案例）；**真实 `env/*.env` 永远不入库**。

### 3.3 前端（web/frontend）

- 接口路径/SSE 帧类型/上传常量集中在 `src/api/contracts.js`（单一来源），禁止在组件里散落 URL 字符串。
- HTTP 一律走 `src/services/http.js`（自动带 JWT、401 登出、统一抛错），不要在组件里裸 fetch。
- 跨页面状态放 `src/stores/` 的 zustand store；组件局部状态用 useState。
- 新页面接入路由时同时更新 `router/nav-config.jsx` 的 `MAIN_NAV`（菜单与路由共用），并配好权限守卫。
- 提交前通过 `npm run lint`。

## 4. 数据库变更

1. 修改 `app/infrastructure/persistence/models.py` 模型；
2. 在 `migrations/versions/` 新增 Alembic 迁移（`alembic revision --autogenerate -m "描述"` 后人工核对）；
3. 同步在 `migrations/legacy_sql/` 补一份增量 SQL（生产手工执行备用），编号接续；
4. 全新部署的基线以根目录 `course.sql` 为准——表结构变更需评估是否同步基线。

软删除必须与关联记录**同事务**标记（参照 `app/infrastructure/persistence/repositories/soft_delete.py`），高危删除走 `core/delete_guard.py` 两步式令牌。

## 5. 测试要求

- Bug 修复必须附带能复现该 bug 的测试；新功能必须配套测试。
- 标记约定（注册于 [pytest.ini](pytest.ini)）：
  - 无标记：纯单元测试，CI 必须能跑（**禁止**依赖网络/LLM/MySQL/Redis）；
  - `@pytest.mark.db` / `backend`：需要数据库或后端 :8000 在线；
  - `@pytest.mark.slow`：真实 LLM/端到端，默认不执行。
- `tests/phase/` 是脚本式用例（`python tests/phase/xxx.py` 直接运行），**不要**用 pytest 收集。
- 本地验证：

```bash
ruff check .
pytest tests/ -q                      # 标准套件（自动排除 slow 与 phase/）
pytest tests/tools/ -q                # 仅无依赖单元（CI 同款）
```

## 6. 提交与 PR 流程

- 分支命名：`feature/简述`、`fix/简述`、`chore/简述`、`docs/简述`。
- Commit message 使用**约定式提交**（与仓库历史一致）：
  - `feat:` 新功能 / `fix:` 缺陷修复 / `docs:` 文档 / `refactor:` 重构
  - `test:` 测试 / `chore:` 构建与工程化 / `ci:` CI 配置 / `perf:` 性能
  - 示例：`fix: 修复 RAGAgent 空结果时 UnboundLocalError`
- 一个 PR 只做一件事，保持小而可评审；不要顺手重排无关代码。
- PR 自检清单：
  - [ ] `ruff check .` 与前端 `npm run lint` 通过
  - [ ] 新增/变更逻辑有测试覆盖，纯单元测试在无外部依赖环境可跑
  - [ ] 涉及表结构时附带迁移（Alembic + 增量 SQL）
  - [ ] 新增配置项已写入 `.example` 模板，无密钥入库
  - [ ] 新增模块/目录已补充对应 README.md
  - [ ] 日志与错误信息不含敏感数据

## 7. 文档约定

- 每个主要目录维护 `README.md`（职责、文件清单、依赖关系），结构变更时同步更新。
- `docs/` 中仅部署文档、用户使用说明与 README 索引入库；内部设计文档保留本地（.gitignore 已排除），**不要把内部设计文档加入公开仓库**。

## 8. 安全问题

请勿在公开 Issue 中提交安全漏洞详情。发现可利用的安全问题，请通过仓库主页的联系方式私下报告，我们会在修复后公开致谢。
