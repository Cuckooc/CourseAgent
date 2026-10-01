# Core 核心组件层

核心组件层提供跨模块的横切能力：配置、身份鉴权、限流、安全防护、可观测性、内容安全等。本层不处理业务逻辑，被 control/service/dao 各层共同依赖。

## 📁 目录结构

```
core/
├── config.py            # 环境变量配置单例（导入期逐字段读取并固化）
├── deps.py              # FastAPI 依赖：JWT 身份/角色守卫/限流
├── security.py          # bcrypt 密码哈希、JWT 签发与校验、令牌护栏
├── responses.py         # BizException 业务异常与统一响应结构
├── redis_client.py      # Redis 客户端单例（不可用时返回 None，调用方内存降级）
├── delete_guard.py      # 高危删除一次性确认令牌（颁发/校验/归属绑定）
├── undo_store.py        # 撤销操作的存取抽象（save/consume/peek）
├── sql_guard.py         # SQL 守卫：WHERE 永真条件阻断
├── param_validator.py   # 工具形参白名单校验（名称/类型/必填）
├── output_validator.py  # SummaryAgent 结构化输出 Pydantic Schema
├── content_filter.py    # 敏感词表加载与内容过滤
├── account_guard.py     # 账号安全守卫（失败计数，Redis 键拼接）
├── verification.py      # 邮箱验证码（Redis 优先/内存降级，过期清理）
├── mailer.py            # SMTP 邮件发送
├── usage.py             # LLM 用量统计（线程级用户 id 绑定）
├── upload_task.py       # 异步上传任务状态管理
├── locks.py             # 进程内/分布式锁工具
├── purge_scheduler.py   # 定时清理调度（注销到期硬删除/软删除过期清理）
├── audit.py             # 审计日志独立 sink（惰性注册，失败降级主日志）
├── degradation_alert.py # 降级告警文件 sink（惰性注册）
├── metrics.py           # Prometheus 指标：HTTP RED 中间件 + LLM 用量
├── trace.py             # request_id 链路追踪（contextvar + X-Request-ID）
├── logging_config.py    # 统一日志（loguru 渲染 + 标准 logging 桥接）
└── prompt_registry.py   # Agent/Prompt 版本号管理
```

## 📄 关键文件说明

### `config.py` - 配置单例

类定义执行期间（模块导入期）逐字段调用 `os.getenv` 读取环境变量并固化为 `settings` 单例。涵盖应用环境、JWT 密钥、MySQL/Redis 连接、DashScope Key、CORS、指标开关、上传目录等全部配置项。对应环境变量案例见 [env/README.md](../env/README.md)。

### `deps.py` - FastAPI 依赖注入

**主要依赖**:
- `get_current_user` - 解析 JWT 获取当前用户
- `require_admin` / `forbid_admin` - 管理员角色守卫 / 管理员禁入（用户态接口）
- `user_rate_limit` - 用户级限流（Redis 优先，内存降级）

### `security.py` - 认证安全

- bcrypt 密码哈希生成与校验
- JWT 签发（HS256，含令牌版本号支持吊销）
- 令牌长度护栏（缓解 PyJWT 已知 DoS 风险，见 requirements.txt 注释）

### `delete_guard.py` + `undo_store.py` - 高危操作防护

两步式删除机制：预览阶段颁发一次性确认令牌（绑定用户与会话归属），确认阶段校验令牌；删除后保留撤销窗口，`undo_store` 以 save/consume/peek 三语义一致的接口屏蔽存储介质差异。

### `metrics.py` + `trace.py` - 可观测性

- `MetricsMiddleware`: HTTP RED 指标（请求速率/错误率/耗时直方图）+ LLM 用量计数
- `RequestIdMiddleware`: 每请求确定 request_id（合法透传优先），写入 contextvar 并回写 `X-Request-ID` 响应头

### `redis_client.py` - 降级哲学

获取 Redis 单例；**未配置或不可用时返回 None**，所有调用方（限流/验证码/短期记忆）自行走内存降级路径 —— 这是全系统"单点故障不阻断服务"设计的关键。

## 🔗 依赖关系

- 被 `control/` 依赖：鉴权（deps）、限流、配置、响应结构
- 被 `service/` 依赖：配置、脱敏/过滤、用量、异常表达
- 被 `dao/` 依赖：BizException（表达可预期业务失败）
- 本层只依赖标准库与第三方基础库（redis/loguru/prometheus_client），不依赖业务模块
