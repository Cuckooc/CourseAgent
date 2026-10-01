# Tests 测试套件

后端集成测试套件，覆盖安全、对抗、并发、边界、RBAC 权限矩阵、端到端对话、数据库完整性、文件上传等风险维度。

## 📁 目录结构

```
tests/
├── conftest.py            # 全局引导与夹具（sys.path/HTTP 封装/三角色账号工厂/测后软删）
├── test_security.py       # 安全测试（注入/越权/鉴权边界）
├── test_adversarial.py    # 对抗测试（恶意输入/提示注入）
├── test_concurrency.py    # 并发测试（ThreadPoolExecutor 线程池）
├── test_boundary.py       # 边界测试（参数极值/异常输入）
├── test_api_rbac.py       # RBAC 权限矩阵（学生/教师/管理员三角色）
├── test_chat_e2e.py       # 端到端对话（真实 LLM/向量库，slow 标记）
├── test_db_integrity.py   # 数据库完整性（事务/级联软删）
├── test_file_upload.py    # 文件上传（类型/大小/解析链路）
├── phase/                 # 阶段式脚本测试（模块导入即执行，脚本式用例）
│   ├── test_all_changes.py        # 纯单元集成脚本（无 LLM/MySQL/Redis 依赖）
│   ├── test_dedup_version.py      # 知识库去重与版本管理
│   ├── test_phase2_kb.py          # 知识库上传/去重/版本 API 端到端
│   ├── test_phase4_stress.py      # 压力测试
│   ├── test_phase4b_concurrency.py # 并发测试
│   ├── test_phase5_boundary.py    # 边界测试
│   ├── test_phase6_roles.py       # 角色权限测试
│   ├── test_phase7_network.py     # 网络异常测试
│   └── test_phase8_agent_eval.py  # Agent 评估
└── tools/                 # 工具调用层测试
    └── test_tool_layer_p1.py      # P1 纯单元套件（注册表越权/参数钳制/Dispatcher 横切，无外部依赖）
```

> **执行模型差异**：根目录 8 个套件为标准 pytest 用例；`phase/` 为脚本式用例（模块导入即顺序执行，PASS/FAIL 计数 + 退出码反映成败，pytest 收集时按脚本执行）；`tools/` 为纯单元测试（无网络/LLM/数据库依赖，CI 可直接运行）。

## 🧪 运行方式

```bash
# 运行全部套件（默认排除 slow；后端/DB 不可达的用例自行 skip 或失败）
pytest

# 仅需要后端 :8000 在线的用例
pytest -m backend

# 需要 MySQL 连通的用例
pytest -m db

# 含真实 LLM/向量库的端到端用例（显式加入）
pytest -m "slow"
```

## ⚙️ 约定说明

- **被测后端**：默认 `http://localhost:8000`，可用环境变量 `PBL_TEST_BASE_URL` 覆盖
- **测试账号**：三角色工厂夹具（user/teacher/admin），走 DAO 直建绕开注册限流，账号名带 `pu_/pt_/pa_` 前缀 + 毫秒时间戳，**测后自动软删除**（级联业务数据），不污染生产数据
- **markers**：`backend`（需后端在线）/ `slow`（真实 LLM，默认不跑）/ `db`（需 MySQL），注册于 [pytest.ini](../pytest.ini)
- **无 pytest-asyncio**：并发用例全部使用线程池，无 async 用例
- **安全声明**：测试账号使用 `@pytest.local` 域名 + 弱密码 `Test1234`，仅由夹具创建，不接触 env/ 配置目录

## 📦 测试依赖

`pytest` + `pytest-cov`（可选，覆盖率），见根 [requirements.txt](../requirements.txt) 测试依赖分组。
