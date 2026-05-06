#!/bin/sh
# BetBot — Postgres backup script.
#
# Runs `pg_dump` against the `db` service in the compose network and writes
# a gzipped SQL dump to /backups (mounted from the host). 30-day rolling
# retention keeps the backup directory bounded.
#
# Schedule via the dedicated `backup` service in docker-compose.yml — the
# service uses a busybox image with an `until` loop and `sleep 86400`, so we
# get one backup per day without depending on host cron.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
PG_HOST="${PG_HOST:-db}"
PG_USER="${PG_USER:-betbot}"
PG_DB="${PG_DB:-betbot}"
PG_PASSWORD="${POSTGRES_PASSWORD:-betbot_dev_pwd}"

mkdir -p "$BACKUP_DIR"

ts=$(date -u +%Y%m%d_%H%M%S)
file="$BACKUP_DIR/betbot_${ts}.sql.gz"

echo "[backup] Starting dump → $file"
PGPASSWORD="$PG_PASSWORD" pg_dump \
  --host="$PG_HOST" \
  --username="$PG_USER" \
  --no-owner --no-privileges \
  --format=plain \
  "$PG_DB" \
  | gzip -9 > "$file"

if [ ! -s "$file" ]; then
  echo "[backup] FAILED — output is empty"
  rm -f "$file"
  exit 1
fi

size_kb=$(du -k "$file" | cut -f1)
echo "[backup] OK — ${size_kb} KB"

# Retention: delete dumps older than RETENTION_DAYS
find "$BACKUP_DIR" -maxdepth 1 -name 'betbot_*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete
echo "[backup] Cleanup done (retention=${RETENTION_DAYS}d)"
