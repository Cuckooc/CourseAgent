# 🎓 CourseAgent 智能课程咨询服务系统

> **面向 PBL 场景的智能课程咨询平台**
> 基于 FastAPI 与多智能体状态机编排，融合 LangChain + ChromaDB 实现 RAG 知识问答，提供企业级的安全、审计与可观测能力

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.121-green.svg)](https://fastapi.tiangolo.com)
[![LangChain](https://img.shields.io/badge/LangChain-0.2-informational.svg)](https://www.langchain.com)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev)
[![Docker](https://img.shields.io/badge/Docker-Compose%20Deploy-2496ED.svg)](https://docs.docker.com/compose/)
[![License](https://img.shields.io/badge/License-MIT-orange.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-Pytest%20Suite-success.svg)](tests/)

## 🚀 快速开始

### 📋 系统要求
- **操作系统**: Windows 10/11 / macOS / Linux
- **Python**: 3.8 或更高版本
- **Node.js**: 18+（前端构建）
- **中间件**: MySQL 8.0、Redis 7（本地调试或 Docker 皆可）

### 💻 本地开发

```bash
# 1. 克隆项目
git clone https://github.com/Cuckooc/CourseAgent.git
cd CourseAgent

# 2. 创建虚拟环境（推荐）
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

# 3. 安装后端依赖
pip install -r requirements.txt

# 4. 配置环境变量（从案例模板复制后按需修改）
cp env/config.env.example env/config.env
cp env/qianwen_config.env.example env/qianwen_config.env
# 至少配置：MySQL 连接、Redis 连接、DashScope API Key

# 5. 初始化数据库（建表脚本）
mysql -u root -p < course.sql

# 6. 启动后端（默认 8000 端口）
uvicorn app.main:app --reload

# 7. 启动前端（另开终端）
cd web/frontend
npm install
npm run dev
```

### 🐳 Docker 一键部署

```bash
# 前置准备：
# 1. env/config.env 中数据库 host 改为 compose 服务名 mysql
# 2. TLS 证书放置 ./nginx/certs/{fullchain.pem, privkey.pem}
# 3. 注入 MySQL root 密码环境变量
docker compose up -d --build
```

编排内容：MySQL 8.0 + Redis 7 + API（多 worker）+ Nginx（TLS 终结 / SSE 透传）+ Prometheus + Alertmanager。
仅 Nginx 暴露 80/443，其余服务全部内网互通。详细步骤见 [部署文档.md](docs/部署文档.md)。

## 💡 主要功能

### 🤖 **多智能体对话引擎**
- **五阶段流水线** - 模糊判定 → 需求分析 → 检索（RAG / 上传文件双分支）→ 结构化汇总 → 回答生成
- **状态机编排** - 关键/非关键 Agent 差异化重试上限、循环检测、回退计数
- **失败自愈** - 失败原因诊断器（缺信息/技术错误）+ 两级兜底响应
- **工具调用层** - 统一查表、跨域鉴权、参数校验、预算控制、超时降级
- **输出验证** - SummaryAgent 结构化 Schema 校验 + 意图一致性验证器

### 📚 **RAG 知识问答**
- **ChromaDB 持久化向量库** - 会话级临时库与全局库隔离，删除会话自动清理
- **父子块切分** - 按文本长度自适应切分策略，父块保语境、子块保精度
- **DashScope 嵌入** - text-embedding-v2 带重试与查询缓存，维度一致性保障
- **会话临时知识库** - 用户上传文件即刻可问，(user_id, session_id) 复合键隔离

### 📄 **文档智能解析**
- **PDF 类型自动路由** - 依据文本密度/乱码率/图片面积/双栏布局分流四种解析路径
- **扫描件 OCR** - 逐页调用 qwen-vl-plus，单页重试与降级
- **图文密集型** - qwen-vl-max 多模态转写表格与图片
- **双栏版式处理** - 中缝检测与双栏合并
- **人工审核流** - OCR/多模态结果 pending → 通过/驳回两步审核

### 🧠 **四层记忆体系**
- **短期记忆** - Redis 原文缓存（mem:short:{user}:{session}）
- **长期记忆** - 会话静默临期自动批量转存 MySQL
- **用户画像** - 兴趣/常问主题增量提取，后台守护落库
- **会话关键词** - 各轮主题词累积 UPSERT 持久化，支撑会话接续

### 🔐 **企业级安全**
- **认证鉴权** - JWT + bcrypt + 令牌版本号吊销，RBAC 学生/管理员双角色
- **高危操作防护** - 两步式删除（预览 + 一次性确认令牌）+ 撤销窗口
- **纵深防御** - SQL 守卫（永真条件阻断）、参数白名单、敏感词过滤、输出校验
- **审计追踪** - 审计日志独立 sink、request_id 全链路透传

### 📊 **可观测性**
- **Prometheus 指标** - HTTP RED（速率/错误/耗时）+ LLM 用量
- **告警分发** - Alertmanager 规则评估与 webhook 通知
- **健康探针** - /healthz（存活）/ /readyz（MySQL 依赖就绪）/ /metrics

## 🏗️ 技术架构

### 📦 **模块化设计**
```
📦 CourseAgent/
├── 📁 control/                # 控制器层 - FastAPI 路由与入口 [详见 control/README.md]
│   ├── app.py                 # 应用入口：生命周期/中间件/异常处理/SPA 托管
│   ├── chat_control.py        # 对话接口（含 SSE 流式）
│   ├── login_control.py       # 认证接口（注册/登录/验证码）
│   ├── file_control.py        # 文件上传与解析任务
│   ├── history_control.py     # 会话历史管理
│   ├── knowledge_control.py   # 知识库文件管理
│   ├── profile_control.py     # 用户画像
│   ├── review_control.py      # 文档审核
│   └── admin_control.py       # 管理员接口（用量/用户管理）
├── 📁 core/                   # 核心组件层 - 横切关注点 [详见 core/README.md]
│   ├── config.py              # 环境变量配置单例
│   ├── deps.py                # JWT 身份依赖/限流
│   ├── security.py            # bcrypt/JWT 签发校验
│   ├── delete_guard.py        # 两步式删除令牌
│   ├── sql_guard.py           # SQL 永真条件阻断
│   ├── metrics.py             # Prometheus 指标
│   └── ...                    # 审计/内容过滤/邮件/验证码/降级告警等
├── 📁 service/                # 业务服务层 [详见 service/README.md]
│   ├── agent_service.py       # 多 Agent 流水线编排（核心）
│   ├── chat_service.py        # 对话编排服务
│   ├── file_service.py        # 文件入库服务
│   ├── knowledge_service.py   # 知识库管理
│   ├── vector_store.py        # ChromaDB 单例
│   └── ...                    # 脱敏/偏好/审核/临时知识库等
├── 📁 multi_agent/            # 多智能体编排层 [详见 multi_agent/README.md]
│   ├── state_machine.py       # 流水线状态机
│   ├── message_bus.py         # Agent 间消息总线
│   ├── base_agent.py          # 判定类 Agent 抽象基类
│   └── vague/analysis/rag/file/summary/chat_agent.py  # 五阶段 Agent
├── 📁 tools/                  # 工具调用层 [详见 tools/README.md]
│   ├── dispatcher.py          # 统一执行入口（鉴权/预算/降级）
│   ├── registry.py            # 工具注册表
│   └── business/              # 业务工具实现
├── 📁 memory/                 # 记忆体系 [详见 memory/README.md]
│   ├── short_term.py          # Redis 短期记忆
│   ├── long_term.py           # 长期记忆转存
│   ├── profile_service.py     # 用户画像
│   └── ...                    # 上下文压缩/关键词/会话接续
├── 📁 model_llm/              # LLM 网关 [详见 model_llm/README.md]
│   ├── gateway.py             # 主模型 + 降级模型重试网关
│   └── llm.py / llm_business.py  # LLM 抽象基类与业务提示词
├── 📁 embedding/              # 向量嵌入 [详见 embedding/README.md]
├── 📁 file_analysis/          # 文档解析 [详见 file_analysis/README.md]
├── 📁 dao/                    # 数据访问层 [详见 dao/README.md]
├── 📁 db/                     # SQLAlchemy 模型与迁移 SQL [详见 db/README.md]
├── 📁 util/                    # 通用工具函数 [详见 util/README.md]
├── 📁 env/                    # 环境变量案例模板 [详见 env/README.md]
├── 📁 web/frontend/           # React 18 前端 [详见 web/frontend/README.md]
├── 📁 docs/                   # 项目文档 [详见 docs/README.md]
├── 📁 migrations/             # Alembic 版本迁移 [详见 migrations/README.md]
├── 📁 scripts/                # 运维脚本 [详见 scripts/README.md]
├── 📁 monitoring/             # Prometheus/Alertmanager 配置 [详见 monitoring/README.md]
├── 📁 nginx/                  # Nginx 反代配置
├── 📄 course.sql              # 建表初始化脚本
├── 📄 docker-compose.yml      # 生产编排（6 服务）
├── 📄 Dockerfile              # 后端镜像构建
└── 📄 requirements.txt        # Python 依赖（锁定实测版本）
```

> **📖 详细文档**: 每个主要目录都包含 `README.md`，说明其中文件与职责，点击上方链接查看。

### 🎯 **分层架构原则**
```
┌──────────────────────────┐
│   前端 React 18 + AntD   │ ← 用户界面层（SSE 流式渲染）
├──────────────────────────┤
│  control/ 控制器层        │ ← FastAPI 路由（鉴权/限流/校验）
├──────────────────────────┤
│  service/ 服务层          │ ← 业务编排（Agent 流水线驱动）
├──────────────────────────┤
│  multi_agent/ + tools/   │ ← 多智能体 + 工具调用
├──────────────────────────┤
│  dao/ + db/ + memory/    │ ← 数据访问 + 记忆体系
├──────────────────────────┤
│  MySQL + Redis + Chroma  │ ← 存储层（关系/缓存/向量）
└──────────────────────────┘
```

### 🔁 **对话主链路**
```
用户提问 → VagueAgent（模糊/明确判定）
        → AnalysisAgent（工具分发决策）
        → RAGAgent（知识库检索）/ FileAgent（上传文件检索）
        → SummaryAgent（结构化汇总，Schema 校验）
        → Verifier（意图一致性验证）
        → ChatAgent（流式生成回答，SSE 输出）
   任一失败 → FailureDiagnoser → Fallback（澄清/错误兜底）
```

## 🧪 测试与质量

### ✅ **运行测试**
```bash
# 运行全部套件（默认排除 slow 端到端用例）
pytest

# 仅需要后端在线的用例 / 含真实 LLM 的端到端用例
pytest -m backend
pytest -m "slow"

# 生成覆盖率报告
pytest --cov=. --cov-report=term-missing
```

测试覆盖安全/对抗/并发/边界/RBAC/端到端/数据库完整性/文件上传八个维度，详见 [tests/README.md](tests/README.md)。

### 🔍 **质量工具链**
```bash
# Lint 检查（ruff 配置见 ruff.toml）
ruff check .

# 供应链漏洞扫描
python scripts/osv_scan.py

# 前端 Lint
cd web/frontend && npm run lint
```

### 📊 **CI 流水线**
GitHub Actions 自动执行 lint 与依赖漏洞扫描（见 `.github/workflows/ci.yml`）。

## 🔧 开发指南

> 📖 **完整文档**: [部署文档](docs/部署文档.md) · [用户使用说明](docs/用户使用说明.md)

### 📈 **常见开发场景**
1. **新增 API 接口**: 在 `control/` 添加路由 → 业务逻辑放 `service/` → 数据访问放 `dao/`
2. **新增 Agent**: 继承 `multi_agent/base_agent.py` → 在 `state_machine.py` 注册状态 → `agent_service.py` 接入流水线
3. **新增工具**: 在 `tools/business/` 实现并通过 `tools/registry.py` 注册
4. **数据库变更**: 在 `migrations/versions/` 新增 Alembic 迁移脚本

## 🤝 贡献指南

> 📋 **准备贡献代码？** 请先阅读 **[CONTRIBUTING.md](CONTRIBUTING.md)** —— 包含分层约束、代码规范、测试要求与 PR 自检清单。

### 🐛 **问题报告**
- 提供详细的错误信息、复现步骤与 request_id
- 说明操作系统、Python 版本与部署方式

### 💡 **功能建议**
- 描述具体的使用场景和需求
- 说明对现有流水线/数据模型的影响范围

## 📚 详细文档索引

### 用户文档
- **[用户使用说明.md](docs/用户使用说明.md)** - 📘 面向最终用户的功能与操作指南
- **[部署文档.md](docs/部署文档.md)** - 🚀 生产环境部署（Docker Compose / TLS / 监控）

> 内部设计文档（框架设计、架构分析等）保留在本地 `docs/` 目录，未纳入公开发布版本。

### 模块文档
| 模块 | 说明 |
|------|------|
| [control/](control/README.md) | 控制器层（9 个路由模块） |
| [core/](core/README.md) | 核心组件层（24 个横切组件） |
| [service/](service/README.md) | 业务服务层（10 个服务） |
| [multi_agent/](multi_agent/README.md) | 多智能体编排（状态机 + 8 个协作组件） |
| [tools/](tools/README.md) | 工具调用层（调度/注册/业务工具） |
| [memory/](memory/README.md) | 记忆体系（6 个组件） |
| [model_llm/](model_llm/README.md) | LLM 网关 |
| [embedding/](embedding/README.md) | 向量嵌入 |
| [file_analysis/](file_analysis/README.md) | 文档解析（7 个组件） |
| [dao/](dao/README.md) | 数据访问层（13 个 DAO） |
| [db/](db/README.md) | 数据库模型与迁移 SQL |
| [web/frontend/](web/frontend/README.md) | React 前端架构 |
| [env/](env/README.md) | 环境变量配置案例 |
| [docs/](docs/README.md) | 全部文档索引 |

### 快速导航

| 我想了解... | 查看文档 |
|------------|---------|
| **如何部署上线** | **[部署文档.md](docs/部署文档.md)** 🚀 |
| **怎么使用系统** | **[用户使用说明.md](docs/用户使用说明.md)** 📘 |
| 多智能体如何协作 | [multi_agent/README.md](multi_agent/README.md) |
| 对话接口如何调用 | [control/README.md](control/README.md) → chat_control |
| RAG 检索如何实现 | [embedding/README.md](embedding/README.md) + [service/README.md](service/README.md) → vector_store |
| PDF 如何解析入库 | [file_analysis/README.md](file_analysis/README.md) |
| 记忆如何管理 | [memory/README.md](memory/README.md) |
| 数据库表结构 | [db/README.md](db/README.md) |
| 环境变量怎么配 | [env/README.md](env/README.md) |
| 前端架构与流式渲染 | [web/frontend/README.md](web/frontend/README.md) |
| 监控告警如何搭建 | [monitoring/README.md](monitoring/README.md) |

---

<div align="center">

**🎓 让课程咨询更智能，让学习路径更清晰 ✨**
