#!/bin/sh
# BetBot — daily backup script.
#
# Backs up two things to /backups (mounted from the host):
#   1. PostgreSQL  → gzipped pg_dump (predictions, bankroll ledger, team stats)
#   2. /app/data   → tarball with tennis ELO, basketball stats, ML calibrator
#                    (without these, retraining is the only recovery)
#
# 30-day rolling retention. Schedule via the `backup` service in
# docker-compose.yml (busybox loop with `sleep 86400`).

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DATA_DIR="${DATA_DIR:-/app/data}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
PG_HOST="${PG_HOST:-db}"
PG_USER="${PG_USER:-betbot}"
PG_DB="${PG_DB:-betbot}"
PG_PASSWORD="${POSTGRES_PASSWORD:-betbot_dev_pwd}"

mkdir -p "$BACKUP_DIR"
ts=$(date -u +%Y%m%d_%H%M%S)

# ── 1. Postgres ──────────────────────────────────────────────────────────
sql_file="$BACKUP_DIR/betbot_${ts}.sql.gz"
echo "[backup] Postgres dump → $sql_file"
PGPASSWORD="$PG_PASSWORD" pg_dump \
  --host="$PG_HOST" \
  --username="$PG_USER" \
  --no-owner --no-privileges \
  --format=plain \
  "$PG_DB" \
  | gzip -9 > "$sql_file"

if [ ! -s "$sql_file" ]; then
  echo "[backup] FAILED — Postgres dump is empty"
  rm -f "$sql_file"
  exit 1
fi
sql_kb=$(du -k "$sql_file" | cut -f1)
echo "[backup] Postgres OK — ${sql_kb} KB"

# ── 2. /app/data volume (tennis ELO, basket stats, ML calibrator) ────────
if [ -d "$DATA_DIR" ] && [ "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  data_file="$BACKUP_DIR/data_${ts}.tar.gz"
  echo "[backup] Data volume → $data_file"
  tar -czf "$data_file" -C "$DATA_DIR" .
  if [ -s "$data_file" ]; then
    data_kb=$(du -k "$data_file" | cut -f1)
    echo "[backup] Data volume OK — ${data_kb} KB"
  else
    echo "[backup] WARN — data volume tar is empty, removing"
    rm -f "$data_file"
  fi
else
  echo "[backup] Data volume skipped (empty or missing)"
fi

# ── Retention: delete files older than RETENTION_DAYS ────────────────────
find "$BACKUP_DIR" -maxdepth 1 -name 'betbot_*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'data_*.tar.gz'   -mtime "+${RETENTION_DAYS}" -delete
echo "[backup] Cleanup done (retention=${RETENTION_DAYS}d)"
