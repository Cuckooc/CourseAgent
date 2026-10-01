# Monitoring 监控配置

Prometheus + Alertmanager 监控告警配置，指标来源为后端 `core/metrics.py`（HTTP RED + LLM 用量）。

## 📁 目录结构

```
monitoring/
├── prometheus.yml     # 抓取配置（targets: api:8000 /metrics）
├── alerts.yml         # 告警规则（错误率/延迟/服务可用性/LLM 用量异常）
└── alertmanager.yml   # 告警分发（默认 webhook 占位，按需配置接收端）
```

## 🔧 使用方法

三个文件由 `docker-compose.yml` 中的 prometheus 与 alertmanager 服务挂载：

- Prometheus UI: `http://127.0.0.1:9090`（仅宿主机回环，公网需反代 + 鉴权）
- Alertmanager UI: `http://127.0.0.1:9093`
- 修改配置后重启对应容器生效：`docker compose restart prometheus alertmanager`

告警规则详见 [alerts.yml](alerts.yml) 注释。
