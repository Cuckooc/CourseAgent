# Control 控制器层

控制器层是 FastAPI 的 HTTP 接口层，负责路由注册、请求体校验、鉴权与限流依赖注入，并把请求转发给 service 层处理。所有接口统一返回 `{status, code, message, request_id}` 格式，由 `app.py` 的全局异常处理器兜底。

## 📁 目录结构

```
control/
├── app.py               # FastAPI 应用入口（生命周期/中间件/路由注册/SPA 托管）
├── chat_control.py      # 对话接口（普通对话/SSE 流式/断网恢复/反馈）
├── login_control.py     # 认证接口（注册/登录/邮箱验证码/身份查询）
├── file_control.py      # 文件上传（安全校验/落盘/异步解析任务进度）
├── history_control.py   # 会话历史（列表/详情/创建/改名/两步式删除/撤销）
├── knowledge_control.py # 知识库文件管理（可见列表/两步式删除）
├── profile_control.py   # 用户画像（查询/手动修改）
├── review_control.py    # 文档审核（OCR/多模态结果审核工作流）
└── admin_control.py     # 管理员接口（LLM 用量/用户列表/角色调整/注销）
```

## 📄 文件详细说明

### `app.py` - 应用入口

**核心职责**:
- `lifespan` 生命周期钩子：启动画像/长期记忆/关键词/清理调度 4 个后台守护线程，关闭时落盘向量索引并释放数据库连接池
- 装配中间件：请求链路追踪（RequestIdMiddleware）、Prometheus 指标、CORS
- 四类全局异常处理器（业务异常/HTTP 异常/参数校验/未捕获异常）
- K8s 探针：`GET /healthz`（存活）、`GET /readyz`（MySQL 就绪）、`GET /metrics`
- 前端 SPA 单端口托管：构建产物存在时 `/` 返回 index.html，未知 GET 路径回退 index.html，API 前缀保持 JSON 404/405 语义

**启动方式**: `uvicorn app.main:app`（Dockerfile / docker-compose 使用）

### `chat_control.py` - 对话接口

**主要路由**:
- 普通对话与 SSE 流式对话（`text/event-stream`）
- 断网恢复（重连后接续流式输出）
- 回答反馈（点赞/点踩入库）

**依赖**: `service/chat_service.py` 对话编排服务

### `login_control.py` - 认证接口

- 账号密码注册/登录、邮箱验证码登录
- 当前登录身份查询（JWT 解析）
- 验证码状态由 `core/verification.py` 管理（Redis 优先，内存降级）

### `file_control.py` - 文件上传

- 安全校验（类型/大小）→ 落盘 → 解析 → 脱敏 → embedding 入库
- 异步任务模型：上传后返回任务 ID，前端轮询进度（`core/upload_task.py`）

### `history_control.py` - 会话历史

- 会话列表分页、详情、创建、改名
- 高危删除走两步式：预览（`core/delete_guard.py` 颁发一次性令牌）→ 确认删除
- 撤销删除（撤销窗口期内）

### `review_control.py` / `knowledge_control.py` / `profile_control.py` / `admin_control.py`

- **review**: 用户查看并审核本人的 OCR/多模态文本提取结果（pending → 通过/驳回）
- **knowledge**: 知识库可见文件列表、两步式删除
- **profile**: 用户画像查询与手动修改（数据来自 `memory/profile_service.py`）
- **admin**: LLM 用量观测、用户列表、角色调整、注销（预览/确认两步式），仅管理员角色可访问

## 🔗 依赖关系

```
control ──依赖──▶ core/deps（JWT 身份、限流）
        ──依赖──▶ service（业务逻辑）
        ──注册──▶ app.py 统一挂载 8 个子路由
```

所有路由模块的鉴权依赖通过 FastAPI `Depends` 注入，控制器本身不写业务逻辑。
