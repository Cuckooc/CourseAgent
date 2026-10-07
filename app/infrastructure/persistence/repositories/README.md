# DAO 数据访问层

数据访问层封装 MySQL 数据读写，每个文件对应一张（或一组）业务表，为 service/control 层提供会话创建、归属校验、列表分页、状态标记等原子能力。

## 📁 目录结构

```
dao/
├── base_information.py   # 所有信息写入 DAO 的抽象基类
├── read.py               # 用户/会话只读查询（user_information / session_information）
├── user.py               # 用户账号写入（user_information）
├── history.py            # 会话历史写入（history_information）
├── information.py        # 会话消息写入（session_information）
├── session.py            # 会话生命周期管理 + 进程内取号锁
├── soft_delete.py        # 软删除（history + session 同事务标记）
├── session_keyword.py    # 会话关键词（session_keywords）
├── feedback.py           # 用户反馈（chat_feedback）
├── chain_log.py          # Agent 链路日志（chain_log）
├── document_review.py    # 文档审核（document_review）
├── profile.py            # 用户画像（user_profile）
└── knowledge.py          # 知识库文件存储（文件名构造/元数据）
```

## 📄 关键文件说明

### `base_information.py` - 抽象基类

所有信息写入 DAO 的抽象基类，统一事务边界与异常表达（业务失败抛 `core/responses.py` 的 BizException）。

### `session.py` - 会话生命周期

**核心能力**:
- 会话创建（含防并发取号：获取不存在则注册的进程内取号锁）
- 会话列表分页查询 `get_session_list_paged`
- 归属校验 `is_session_owner`（防越权访问的基础）
- 管理会话标题、状态等元数据

### `soft_delete.py` - 软删除

`history_information` + `session_information` **同事务**标记软删除，保证两侧一致性；配合 `core/purge_scheduler.py` 实现 3 年过期清理、7 天注销到期硬删除。

### `chain_log.py` - Agent 链路日志

对应 MySQL 表 `chain_log`，由 `AgentService` 用于**异步**持久化多 Agent 链路日志（各阶段状态、耗时、错误），是排查流水线问题的主要数据来源。

### `knowledge.py` - 知识库存储

构造知识库存储文件名 `{user_id}_{安全原始名}_{hash32}.{ext}`，避免文件名冲突与路径穿越。

### 其余文件

| 文件 | 对应表 | 用途 |
|------|--------|------|
| `read.py` | user_information / session_information | 登录身份查询、会话消息只读 |
| `user.py` | user_information | 注册写入、角色调整、注销 |
| `history.py` | history_information | 会话历史读写 |
| `information.py` | session_information | 逐条消息落库 |
| `session_keyword.py` | session_keywords | 关键词 UPSERT（Redis 累积后定期持久化） |
| `feedback.py` | chat_feedback | 点赞/点踩反馈 |
| `document_review.py` | document_review | 审核记录状态流转 |
| `profile.py` | user_profile | 画像三字段（profile_text/interests/topics）读写 |

## 🔗 依赖关系

- 被 `service/`、`control/`（util/user.py 编排）依赖
- 依赖 `db/session.py` 获取 SQLAlchemy 会话、`db/models.py` ORM 模型
- 表结构详见 [db/README.md](../db/README.md)
