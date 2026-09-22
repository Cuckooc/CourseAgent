#!/usr/bin/env bash
# 恢复脚本：与 scripts/backup.sh 配套。危险操作——会覆盖现有数据，先停 api 再执行。
#
# 用法：
#   ./scripts/restore.sh mysql    backups/mysql_20260913_030000.sql.gz
#   ./scripts/restore.sh chroma   backups/chroma_20260913_030000.tar.gz
#   ./scripts/restore.sh uploads  backups/uploads_20260913_030000.tar.gz
set -euo pipefail

KIND="${1:-}"
FILE="${2:-}"
PROJECT="${COMPOSE_PROJECT_NAME:-pbl}"

[ -z "$KIND" ] || [ -z "$FILE" ] && { grep '^#' "$0" | head -n 12; exit 1; }
[ -f "$FILE" ] || { echo "备份文件不存在: $FILE"; exit 1; }

echo "!! 即将覆盖现有 ${KIND} 数据，请确认 api 已停止：docker compose stop api"
read -r -p "输入 YES 继续: " ok; [ "$ok" = "YES" ] || exit 1

case "$KIND" in
  mysql)
    # 整库重建：先 DROP/CREATE 保证与备份一致
    docker compose exec -T mysql sh -c \
      'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "DROP DATABASE IF EXISTS db_course; CREATE DATABASE db_course CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"'
    gunzip -c "$FILE" | docker compose exec -T mysql sh -c \
      'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" db_course'
    echo "mysql 恢复完成，请启动 api 并抽查 /healthz 与登录"
    ;;
  chroma)
    docker run --rm -v "${PROJECT}_chromadb_data:/dst" -v "$(cd "$(dirname "$FILE")" && pwd):/src:ro" \
      alpine sh -c "rm -rf /dst/* && tar xzf /src/$(basename "$FILE") -C /dst"
    echo "chroma 恢复完成"
    ;;
  uploads)
    docker run --rm -v "${PROJECT}_uploads_data:/dst" -v "$(cd "$(dirname "$FILE")" && pwd):/src:ro" \
      alpine sh -c "rm -rf /dst/* && tar xzf /src/$(basename "$FILE") -C /dst"
    echo "uploads 恢复完成"
    ;;
  *)
    echo "未知类型: $KIND（可选 mysql|chroma|uploads）"; exit 1
    ;;
esac
