# Memory 记忆体系

记忆体系负责对话上下文的多层持久化与滚动管理：Redis 承担短期记忆缓存，MySQL 承担长期记忆与画像，后台守护线程异步转存，支撑长对话与跨会话连续性。

## 📁 目录结构

```
memory/
├── short_term.py            # Redis 短期记忆（mem:short:{user_id}:{session_id}）
├── long_term.py             # 长期记忆转存守护（临期批量转 MySQL 后清缓存）
├── context_memory.py        # 上下文压缩（短期 + 长期之上的统一视图）
├── profile_service.py       # 用户画像（profile_text/interests/topics 三字段）
├── session_keyword_service.py # 会话关键词累积（Redis 累积 → 定期 UPSERT MySQL）
└── session_rollover.py      # 会话接续（静默会话开启接续会话并迁移记忆）
```

## 🧠 四层记忆模型

```
┌─────────────────────────────────────────────┐
│ 短期记忆  Redis 原文缓存（毫秒级读写）          │
│   mem:short:{user_id}:{session_id}          │
├─────────────────────────────────────────────┤
│ 上下文压缩  统一视图：压缩历史 + 检索增强注入    │
├─────────────────────────────────────────────┤
│ 长期记忆  MySQL session_information          │
│   （静默临期/条数超限 → 批量转存 → 删缓存）      │
├─────────────────────────────────────────────┤
│ 用户资产  画像 + 会话关键词（跨会话）           │
└─────────────────────────────────────────────┘
```

## 📄 文件详细说明

### `short_term.py` - 短期记忆

按 `mem:short:{user_id}:{session_id}` 键存储会话消息 list。Redis 不可用时由调用方走内存降级。由 `core/redis_client.py` 提供单例。

### `long_term.py` - 长期记忆转存守护

承接短期记忆：会话**静默临近过期**或消息条数超限时，批量转存 MySQL 后删除 Redis 缓存。`get_long_term_flusher().start_background_flusher()` 由 `control/app.py` 生命周期启动。

### `context_memory.py` - 上下文压缩

在短期记忆（Redis 原文）与长期记忆（MySQL session_information）之上构建统一上下文视图：集中"是否强依赖业务上下文"的启发式规则，按需压缩/裁剪历史，控制注入 LLM 的 token 预算。

### `profile_service.py` - 用户画像

维护每用户的 `profile_text / interests / topics` 三字段：对话期间增量提取，暂存于进程内，`start_background_flusher()` 定期落库（`dao/profile.py`），启动时补扫停机期间已满 7 天的暂存。

### `session_keyword_service.py` - 会话关键词

把各轮对话（含上下文压缩、RAG 汇总环节）产出的主题词在 Redis 中累积，定期 UPSERT 到 `session_keywords` 表持久化，为会话接续与检索提供信号。

### `session_rollover.py` - 会话接续

静默会话到期后开启接续会话：生成接续标题（空/默认标题 → "会话接续"；已有标题追加"（接续）"），迁移必要记忆上下文，保证用户体验连续。

## 🔗 依赖关系

- 被 `service/chat_service.py`（上下文注入）与 `service/agent_service.py` 调用
- 依赖 `core/redis_client.py`、`dao/`（profile/session_keyword/information）
- 3 个后台 flusher 均由 `control/app.py` lifespan 启动，带 Redis 周期锁互斥（多 worker 安全）
