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

# ── 3. Offsite copy (optional) ───────────────────────────────────────────
# If BACKUP_REMOTE_TARGET is set, push fresh backups offsite via rclone. The
# user picks any rclone-supported backend (S3, B2, GDrive, Dropbox, SFTP,
# ...) by mounting their rclone.conf in /root/.config/rclone/rclone.conf
# and setting BACKUP_REMOTE_TARGET=remote:bucket/path .
#
# Local backups remain authoritative — a failure here logs a warning but
# never aborts the daily cycle. Disk-resident copies are still good.
if [ -n "${BACKUP_REMOTE_TARGET:-}" ]; then
  if command -v rclone >/dev/null 2>&1; then
    echo "[backup] Offsite copy → ${BACKUP_REMOTE_TARGET}"
    # Push only the two files we just created (not the full BACKUP_DIR) —
    # the retention step below handles old-file pruning locally.
    if rclone copy "$sql_file" "$BACKUP_REMOTE_TARGET" --no-traverse 2>&1; then
      echo "[backup] Offsite Postgres OK"
    else
      echo "[backup] WARN — offsite Postgres upload failed (exit $?)"
    fi
    if [ -f "${data_file:-}" ]; then
      if rclone copy "$data_file" "$BACKUP_REMOTE_TARGET" --no-traverse 2>&1; then
        echo "[backup] Offsite data volume OK"
      else
        echo "[backup] WARN — offsite data volume upload failed (exit $?)"
      fi
    fi
    # Optional remote retention: drop files older than BACKUP_REMOTE_RETENTION_DAYS.
    # Use a slightly larger window than local retention so the remote is the
    # belt-and-suspenders copy. Skip if unset.
    if [ -n "${BACKUP_REMOTE_RETENTION_DAYS:-}" ]; then
      echo "[backup] Pruning offsite copies older than ${BACKUP_REMOTE_RETENTION_DAYS}d"
      rclone delete "$BACKUP_REMOTE_TARGET" \
        --min-age "${BACKUP_REMOTE_RETENTION_DAYS}d" \
        --include 'betbot_*.sql.gz' --include 'data_*.tar.gz' 2>&1 || \
        echo "[backup] WARN — offsite prune failed (non-fatal)"
    fi
  else
    echo "[backup] WARN — BACKUP_REMOTE_TARGET set but rclone not installed; skipping offsite"
  fi
fi

# ── Retention: delete files older than RETENTION_DAYS ────────────────────
find "$BACKUP_DIR" -maxdepth 1 -name 'betbot_*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'data_*.tar.gz'   -mtime "+${RETENTION_DAYS}" -delete
echo "[backup] Cleanup done (retention=${RETENTION_DAYS}d)"
