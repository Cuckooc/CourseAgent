# Service 业务服务层

业务服务层封装系统核心业务流程：多 Agent 流水线编排、对话上下文构建、文件入库、知识库管理、脱敏、审核工作流等。是 control 层与 dao/memory/multi_agent 层之间的业务门面。

## 📁 目录结构

```
service/
├── agent_service.py        # ⭐ Agent 自动化流程编排（状态机版，对话主链路核心）
├── chat_service.py         # 对话编排服务（应用级单例）
├── file_service.py         # 知识库文件入库服务（解析→脱敏→切分→embedding）
├── knowledge_service.py    # 知识库管理服务（列表/详情/删除，单例）
├── vector_store.py         # ChromaDB 持久化单例（懒加载 + HNSW 落盘）
├── temp_knowledge_store.py # 会话级临时知识库 (user_id, session_id) 复合键
├── mask_service.py         # 数据脱敏服务（正则替换个人敏感信息）
├── preference_service.py   # 用户偏好提取（LLM 异步抽取兴趣/主题）
├── review_service.py       # 文档审核工作流（pending → 通过/驳回）
└── admin_user_service.py   # 管理端用户管理（列表/角色调整/注销）
```

## 📄 关键文件说明

### `agent_service.py` - 多 Agent 流水线编排（核心）

**职责**: 驱动 `PipelineStateMachine`，按状态流转调度 VagueAgent → AnalysisAgent → RAGAgent/FileAgent → SummaryAgent → ChatAgent，处理重试、回退、兜底与链路日志落库。

**主要流程**:
1. 构建 `AgentMessage` 经 `MessageBus` 分发
2. 依据状态机当前状态调用对应 Agent
3. 关键 Agent 失败 → `failure_diagnoser` 诊断 → `fallback` 两级兜底
4. 各阶段产出经 `verifier` 意图一致性验证
5. 全链路写入 `dao/chain_log.py`（异步）

### `chat_service.py` - 对话编排服务

对话业务入口（应用级单例），串接：上下文压缩（`memory/context_memory.py`）→ 画像/关键词注入 → 调用 `AgentService` → 结果落库（`dao/history.py` / `dao/information.py`）→ 会话滚换（`memory/session_rollover.py`）。

### `file_service.py` + `mask_service.py` + `preference_service.py` - 文件入库链路

```
上传文件 → 按类型解析（file_analysis/）
        → 敏感信息脱敏（mask_service 正则替换）
        → 父子块切分（embedding/parent_child.py）
        → embedding 向量入库（vector_store 或 temp_knowledge_store）
        → LLM 异步提取用户偏好（preference_service）
```

### `vector_store.py` + `temp_knowledge_store.py` - 双向量库

- **vector_store**: 全局唯一 langchain Chroma 持久化实例（懒加载）；关闭时强制落盘 HNSW 尾部索引，避免重启后最近文件"消失"
- **temp_knowledge_store**: 以 `(user_id, session_id)` 复合键管理会话独立 Chroma 库，持久化到 `uploads/temp/<uid>_<sid>/chroma/`，删除会话时自动清理

### `review_service.py` - 文档审核工作流

对 OCR/多模态提取结果提供人工审核：创建 pending 记录 → 用户查看 → 通过（进入切分入库）/ 驳回。数据落 `dao/document_review.py`。

### `admin_user_service.py` - 管理端服务

用户列表查询、角色调整、账号注销（软删除 + 到期硬删除，配合 `core/purge_scheduler.py`）。

## 🔗 依赖关系

```
service ──▶ multi_agent/（流水线执行）  tools/（工具调度）
        ──▶ memory/（记忆读写）        embedding/（向量化）
        ──▶ file_analysis/（文件解析） dao/（数据落库）
        ──▶ core/（配置/脱敏/用量）
```
