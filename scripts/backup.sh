#!/usr/bin/env bash
# 全量备份：MySQL（mysqldump 逻辑备份）+ Chroma 向量库 + 上传文件（tar 卷快照）
# 在 Docker 宿主机上执行；建议 cron 每日一次。
#
# cron 示例（每日 03:00，日志追加）：
#   0 3 * * * cd /opt/PBL && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# 恢复：见 scripts/restore.sh（备份必须演练恢复，否则等于没有备份）
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d_%H%M%S)"
# compose 默认项目名 = 目录名小写；若用 docker compose -p 改过项目名需同步修改
PROJECT="${COMPOSE_PROJECT_NAME:-pbl}"

mkdir -p "$BACKUP_DIR"
log() { echo "[$(date '+%F %T')] $*"; }

# 1) MySQL 逻辑备份（--single-transaction 一致性快照，不锁表）
log "dumping mysql -> mysql_${STAMP}.sql.gz"
docker compose exec -T mysql sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers db_course' \
  | gzip > "$BACKUP_DIR/mysql_${STAMP}.sql.gz"

# 2) Chroma 向量库（named volume 只读挂载打 tar，不依赖宿主机卷物理路径）
log "taring chromadb volume -> chroma_${STAMP}.tar.gz"
docker run --rm -v "${PROJECT}_chromadb_data:/src:ro" -v "$(cd "$BACKUP_DIR" && pwd):/dst" \
  alpine tar czf "/dst/chroma_${STAMP}.tar.gz" -C /src .

# 3) 上传文件
log "taring uploads volume -> uploads_${STAMP}.tar.gz"
docker run --rm -v "${PROJECT}_uploads_data:/src:ro" -v "$(cd "$BACKUP_DIR" && pwd):/dst" \
  alpine tar czf "/dst/uploads_${STAMP}.tar.gz" -C /src .

# 4) 过期清理（按修改时间，超过保留期删除）
find "$BACKUP_DIR" -type f \( -name 'mysql_*.sql.gz' -o -name 'chroma_*.tar.gz' -o -name 'uploads_*.tar.gz' \) \
  -mtime "+${RETENTION_DAYS}" -delete

log "backup done: $(ls -1t "$BACKUP_DIR" | head -n 3 | tr '\n' ' ')"
