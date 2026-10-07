# Multi-Agent 多智能体编排层

多智能体编排层实现对话主链路的五阶段流水线：以**状态机**驱动各 Agent 顺序协作，以**消息总线**解耦通信，配合失败诊断、兜底与验证器保障链路健壮性。

## 📁 目录结构

```
multi_agent/
├── state_machine.py      # ⭐ 流水线状态机（状态枚举/转移/重试/循环检测）
├── message_bus.py        # Agent 间消息总线（点对点邮箱 + 广播）
├── base_agent.py         # 判定类 Agent 抽象基类（接口约定 + 通用能力）
├── vague_agent.py        # 阶段 1：模糊/明确二分类判定
├── analysis_agent.py     # 阶段 2：下游工具分发决策
├── rag_agent.py          # 阶段 3 RAG 分支：知识库检索
├── file_agent.py         # 阶段 3 File 分支：上传文件检索
├── summary_agent.py      # 阶段 4：结构化汇总（JSON Schema 校验）
├── chat_agent.py         # 阶段 5：最终回答生成（流式，独立实现）
├── verifier.py           # 输出与用户意图一致性验证 + 相关性打分
├── failure_diagnoser.py  # 关键 Agent 失败原因诊断（缺信息/技术错误）
├── fallback.py           # 两级兜底：澄清/错误响应构造 + 状态切换
└── retrieval.py          # 检索底层实现（版本块过滤：仅保留 is_latest）
```

## 🔁 状态机流转

**主要状态**（`AgentState` 枚举）:
```
IDLE → VAGUE_DETECTING → ANALYZING → RETRIEVING → SUMMARIZING → GENERATING → 完成
                                  ↘ File 分支（上传文件检索）
   任一失败 → 诊断（MISSING_INFO / TECHNICAL_ERROR）→ 兜底态
```

**PipelineStateMachine 核心机制**:
- 单次请求状态维护 + 重试计数 + 回退计数
- **关键/非关键 Agent 差异化重试上限**（关键 Agent 重试耗尽走兜底，非关键可跳过）
- 循环检测（防止状态反复回跳）
- 链日志记录（经 `dao/chain_log.py` 异步落库）

## 📄 关键文件说明

### `message_bus.py` - 消息总线

- **AgentMessage** 标准结构：sender/receiver/payload/task_id/state/error（旧字段 `message` 自动映射到 `payload` 兼容）
- **线程安全、每请求隔离**：点对点邮箱投递、读取后清空、支持广播
- 各 Agent 间解耦通信的唯一通道

### `base_agent.py` - 抽象基类

判定类 Agent（Vague/Analysis 等）的接口约定 + 通用能力复用：统一 LLM 构建、Agent 元信息、`create_agent` 状态机调度入口、决策输出语义。FileAgent 与 ChatAgent 因流程特殊为独立实现。

### `vague_agent.py` / `analysis_agent.py`

- **VagueAgent**: LLM 对用户 query 二分类（模糊/明确），模糊提问引导澄清
- **AnalysisAgent**: 分析需求并决策下游工具分发（交由 tools/dispatcher 执行）

### `rag_agent.py` / `file_agent.py` / `retrieval.py`

- **RAGAgent**: 加载全局 Chroma 向量库（不存在则从内置 JSON 知识库构建）
- **FileAgent**: 检索会话临时知识库（用户本次会话上传的文件）
- **retrieval**: 底层检索实现，版本块过滤——排除 `is_latest=False` 的旧版本块，无该字段的历史块与 `is_latest=True` 均保留

### `summary_agent.py` + `output_validator.py`（core）

解析 SummaryAgent 的 JSON 输出：先用 Pydantic Schema 严格验证，失败回退宽松解析，双保险。

### `chat_agent.py` - 流水线终点

最终回答生成，独立实现（不继承 BaseAgent），输出经 SSE 流式推送给前端。

### `failure_diagnoser.py` + `fallback.py` + `verifier.py` - 健壮性三件套

- **诊断**: 失败归类为 MISSING_INFO（缺信息 → 构造澄清引导）/ TECHNICAL_ERROR（技术错误 → 错误响应）
- **兜底**: 两级兜底处理器，构造响应并切换状态机到兜底态
- **验证**: Agent 输出与用户原始意图一致性校验，兼汇总相关性打分

## 🔗 依赖关系

- 被 `service/agent_service.py` 编排调用
- 依赖 `model_llm/`（LLM 网关）、`tools/`（工具执行）、`service/vector_store.py`（向量库）
- 消息结构详见各文件 docstring；对话主链路全景见根 [README.md](../README.md)
