#!/usr/bin/env bash
# pcc 数据库备份。从 config.toml 读连接信息，只做 pg_dump，不删任何历史备份。
# 用法：bash deploy/backup.sh [备份目录]   默认 ./backups
# cron 示例：0 3 * * * cd /opt/aa-server && bash deploy/backup.sh >> /var/log/pcc-backup.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${PCC_CONFIG:-$ROOT/config.toml}"
BACKUP_DIR="${1:-$ROOT/backups}"

if [ ! -f "$CFG" ]; then
  echo "配置文件不存在: $CFG" >&2
  exit 1
fi

eval "$(python3 - "$CFG" <<'PY'
import shlex
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

with open(sys.argv[1], "rb") as f:
    d = tomllib.load(f)["database"]

for k, v in (("PGHOST", "host"), ("PGPORT", "port"),
             ("PGDB", "dbname"), ("PGUSER", "user"), ("PGPASS", "password")):
    print(f"{k}={shlex.quote(str(d[v]))}")
PY
)"

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/aa_data-$(date +%F-%H%M).sql.gz"
if ! PGPASSWORD="$PGPASS" pg_dump -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDB" | gzip > "$OUT"; then
  echo "pg_dump 失败：本机 pg_dump 版本需与服务器 PostgreSQL 一致；此脚本应在服务器上运行。" >&2
  exit 1
fi
echo "备份完成: $OUT ($(du -h "$OUT" | cut -f1))"
