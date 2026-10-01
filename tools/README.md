# Tools 工具调用层

工具调用层是 **LLM 决策与业务执行之间的唯一通道**：AnalysisAgent 决策出的工具调用统一经 `ToolDispatcher` 执行，完成查表、跨域/角色鉴权、参数安全校验、并发执行、超时与降级控制。

## 📁 目录结构

```
tools/
├── dispatcher.py        # ⭐ 统一执行入口（预算/鉴权/参数安全/并发/超时/降级）
├── registry.py          # 工具注册表（惰性导入业务包触发自注册）
├── function_tools.py    # 函数式工具定义与实现
├── protocol.py          # 工具调用协议（ToolResult + 调用上下文）
├── shadow.py            # 影子决策（从 Agent 消息提取问题构造影子 prompt）
└── business/
    ├── __init__.py
    └── knowledge_business.py  # 知识库业务工具（Document → DTO 契约转换）
```

## 📄 文件详细说明

### `dispatcher.py` - ToolDispatcher（核心）

**核心职责**（决策层 → 业务层唯一通道）:
1. **查表** — 从 registry 校验工具名合法性
2. **跨域/角色鉴权** — 校验当前用户是否有权调用该工具
3. **参数安全** — 经 `core/param_validator.py` 白名单校验形参（名称/类型/必填）
4. **预算控制** — 单请求工具调用次数/开销上限
5. **并发执行** — 一批工具调用统一调度
6. **超时与降级** — 单工具超时不拖垮整链，失败降级返回结构化错误

### `registry.py` - 注册表

惰性导入 `tools/business` 业务包，触发各工具的 `register_tool` 显式自注册（避免循环导入）。新增工具只需在 business 包内注册，dispatcher 无需改动。

### `protocol.py` - 调用协议

把"这次工具调用代表谁、在哪个会话/任务中"等服务端事实显式传入工具上下文（防止工具伪造身份），`ToolResult` 统一成功/失败返回结构。

### `shadow.py` - 影子决策

从 Agent 消息体提取用户问题，作为影子决策 prompt 的核心输入（用于在不打扰主链路的情况下评估工具决策质量）。

### `business/knowledge_business.py` - 知识库业务工具

把 LangChain Document 转换为与 MessageBus 旧契约一致的 DTO `{content, metadata}`，作为 RAG/File Agent 检索结果的标准化出口。

## 🔗 依赖关系

- 被 `multi_agent/`（analysis 阶段）调用
- 依赖 `core/`（param_validator / usage 预算）
- 工具实现按"注册表 + 协议"模式扩展，详见各文件 docstring
