# Util 通用工具层

通用工具层提供跨业务的轻量工具函数：上下文规则、结果归一化、标题生成、用户信息编排。

## 📁 目录结构

```
util/
├── context.py         # 业务上下文依赖判定规则
├── result_handle.py   # Agent 结果归一化
├── title.py           # 会话标题生成（LLM）
└── user.py            # 用户信息编排入口
```

## 📄 文件详细说明

### `context.py` - 上下文依赖判定

集中"是否强依赖业务上下文"的启发式规则，判断当前请求需要哪些上下文（记忆/画像/关键词），供对话编排按需注入。

### `result_handle.py` - 结果归一化

把 Agent 原始输出归一化为前端展示与入库共用的扁平字典结构，屏蔽各 Agent 输出格式差异。

### `title.py` - 标题生成

复用 `TitleLLM` 的 `generate()` 提示词模板，为会话自动生成简短标题。

### `user.py` - 用户信息编排

对 control 层提供单一入口，编排 `dao/`（Information_Read 等）完成用户信息聚合查询，避免控制器直接拼装多个 DAO。

## 🔗 依赖关系

被 `control/` 与 `service/` 轻量依赖，自身依赖 `dao/` 与 `model_llm/`。
