# Env 环境变量配置案例

本目录存放**配置案例模板**（`.example` 后缀），部署时复制为同名去掉 `.example` 的文件并填入真实值。真实配置文件已被 `.gitignore` 排除，**严禁提交真实密钥**。

## 📁 目录结构

```
env/
├── config.env.example           # 主配置案例（数据库/JWT/上传/Redis/可观测性）
└── qianwen_config.env.example   # 通义大模型配置案例（DashScope）
```

## 🚀 使用方法

```bash
cp env/config.env.example env/config.env
cp env/qianwen_config.env.example env/qianwen_config.env
# 编辑填入真实值后启动应用
```

## 📄 config.env 配置项分组

| 分组 | 配置项 | 说明 |
|------|--------|------|
| **MySQL** | host / user / password / database / charset / port | 数据库连接（Docker 部署时 host 改为服务名 `mysql`） |
| **应用** | app_env | `dev` / `prod`（影响日志格式与安全检查） |
| **JWT** | jwt_secret | 生产必须换随机长字符串（`secrets.token_hex(32)`） |
| | jwt_algorithm / access_token_expire_minutes | HS256 签名 / token 有效期（默认 720 分钟） |
| **CORS** | cors_origins | 生产环境配置具体域名白名单（逗号分隔） |
| **上传** | upload_max_mb / upload_dir / upload_allowed_ext | 大小上限（50MB）/ 存储目录 / 允许的扩展名 |
| **Redis** | redis_url | 可选；多副本部署必须配置（限流/验证码共享），单机可留空走内存降级 |
| **可观测性** | log_level / log_json / metrics_enabled | 日志级别 / JSON 格式 / Prometheus 指标开关 |

## 📄 qianwen_config.env

DashScope 相关配置：API Key 与模型名（对话模型 / qwen-vl 系列 OCR 与多模态模型 / text-embedding-v2 嵌入模型）。

## ⚠️ 安全提示

- `jwt_secret` 生产环境使用默认值时应用启动会打印告警（见 `control/app.py` lifespan）
- 配置真实值后确认 `git status` 中不出现 `env/config.env`、`env/qianwen_config.env`
