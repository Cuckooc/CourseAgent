# Scripts 运维与开发脚本

运维辅助脚本目录（开发调试脚本未纳入版本库）。

## 📁 目录结构

```
scripts/
├── backup.sh     # 数据备份脚本（MySQL/上传文件/向量库）
├── restore.sh    # 备份恢复脚本
└── osv_scan.py   # 依赖供应链漏洞扫描（OSV，CI 集成）
```

## 📄 脚本说明

### `backup.sh` / `restore.sh`

配合 docker-compose 数据卷（mysql_data / uploads_data / chromadb_data）的备份与恢复流程，用法见脚本头部注释与 [部署文档.md](../docs/部署文档.md)。

### `osv_scan.py`

扫描 requirements.txt 依赖的已知漏洞（OSV 数据库），CI 中以 `--ignore-vuln` 显式登记不可升级的接受风险（详见 requirements.txt 内注释）。
