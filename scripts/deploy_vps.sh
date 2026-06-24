#!/usr/bin/env bash
# BetBot — OVH VPS (Ubuntu) production bootstrap.
#
# Run this ON the VPS, as a sudo-capable user, FROM THE REPO ROOT:
#
#   cd /path/to/betbot
#   chmod +x scripts/deploy_vps.sh        # once
#   ./scripts/deploy_vps.sh
#
# What it does (all steps idempotent and safe to re-run):
#   1. Installs Docker Engine + the Compose plugin if missing, and adds the
#      current user to the `docker` group.
#   2. Configures ufw to allow OpenSSH + 80/tcp + 443/tcp + 443/udp (Caddy
#      publishes 80, 443, and 443/udp for HTTP/3), then enables it
#      (non-interactive). Postgres (5432) and Redis (6379) are NEVER opened —
#      the prod overlay binds them to the compose network only.
#   3. Refuses to continue unless `.env` exists AND contains a valid, SECURE
#      production config: a strong POSTGRES_PASSWORD, REST API auth enabled
#      (API_BASIC_PASSWORD or JWT), BETBOT_DOMAIN, ACME_EMAIL, and HSTS on.
#      Secrets are NEVER created, printed, or logged by this script.
#   4. (Optional) Restores a Postgres dump + data volume from ./backups INTO a
#      freshly created database BEFORE the application containers start, so the
#      worker/api/dashboard never race the restore.
#   5. Builds and starts the production stack with the Caddy/HTTPS overlay:
#        docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
#   6. Prints next steps (watch the Caddy cert, the public HTTPS URLs).
#
# This script contains NO secrets.

set -euo pipefail

# ── Resolve repo root (script lives in <repo>/scripts/) ──────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)

log()  { echo "[deploy] $*"; }
warn() { echo "[deploy] WARN: $*" >&2; }
die()  { echo "[deploy] ERROR: $*" >&2; exit 1; }

log "Repo root: $REPO_ROOT"

# Sanity-check we're really in the repo root (compose files must be present).
[ -f docker-compose.yml ]      || die "docker-compose.yml not found — run this from the repo root."
[ -f docker-compose.prod.yml ] || die "docker-compose.prod.yml not found — run this from the repo root."

# ── sudo helper (works whether or not we're already root) ────────────────────
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || die "Not root and 'sudo' is not installed."
  SUDO="sudo"
fi

# ── 1. Docker Engine + Compose plugin ────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  log "Docker already installed: $(docker --version)"
else
  log "Docker not found — installing via https://get.docker.com ..."
  tmp_script="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp_script'" EXIT
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com -o "$tmp_script"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp_script" https://get.docker.com
  else
    die "Neither curl nor wget is available to download the Docker installer."
  fi
  $SUDO sh "$tmp_script"
  rm -f "$tmp_script"
  trap - EXIT
  log "Docker installed: $(docker --version)"
fi

# The Compose plugin ships with get.docker.com, but verify and install if the
# host already had an old Docker without it.
if docker compose version >/dev/null 2>&1; then
  log "Docker Compose plugin present: $(docker compose version | head -n1)"
else
  log "Docker Compose plugin missing — installing docker-compose-plugin ..."
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y docker-compose-plugin
  else
    die "Cannot auto-install the Compose plugin (apt-get not found). Install 'docker-compose-plugin' manually."
  fi
  docker compose version >/dev/null 2>&1 || die "Compose plugin install did not take effect."
  log "Docker Compose plugin installed: $(docker compose version | head -n1)"
fi

# ── Add current user to the docker group (idempotent) ────────────────────────
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [ "$TARGET_USER" = "root" ]; then
  log "Running as root — no docker group membership needed."
elif id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  log "User '$TARGET_USER' is already in the 'docker' group."
else
  log "Adding user '$TARGET_USER' to the 'docker' group ..."
  $SUDO usermod -aG docker "$TARGET_USER"
  warn "Group change takes effect on next login. For THIS session, docker"
  warn "commands below run via sudo; afterwards log out/in (or run 'newgrp docker')"
  warn "to use docker without sudo."
fi

# Decide how to invoke docker for the rest of this run. If the current process
# can't reach the daemon socket yet (fresh group add), fall back to sudo so the
# deploy still completes in one pass.
if docker info >/dev/null 2>&1; then
  DOCKER=""
elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
  log "Using sudo for docker in this session (group membership not yet active)."
  DOCKER="$SUDO"
else
  die "Cannot talk to the Docker daemon. Is it running? Try: $SUDO systemctl start docker"
fi

# Wrapper so every compose invocation honours the sudo decision above.
dc() { $DOCKER docker compose "${COMPOSE_FILES[@]}" "$@"; }

# ── 2. Firewall (ufw): OpenSSH + 80/tcp + 443/tcp + 443/udp ──────────────────
# Caddy (the only internet-facing service in the prod overlay) publishes
# 80:80, 443:443 and 443:443/udp. We open exactly those plus SSH. Postgres
# (5432) and Redis (6379) are intentionally left closed — the prod overlay
# resets their host port bindings so they only exist on the compose network.
if command -v ufw >/dev/null 2>&1; then
  log "Configuring ufw (OpenSSH, 80/tcp, 443/tcp, 443/udp) ..."
  # `ufw allow` is idempotent — re-adding an existing rule is a no-op.
  $SUDO ufw allow OpenSSH   >/dev/null 2>&1 || $SUDO ufw allow 22/tcp >/dev/null
  $SUDO ufw allow 80/tcp    >/dev/null
  $SUDO ufw allow 443/tcp   >/dev/null
  $SUDO ufw allow 443/udp   >/dev/null   # HTTP/3 (QUIC) — Caddy publishes 443/udp
  if $SUDO ufw status | grep -q "Status: active"; then
    log "ufw already active; rules ensured."
  else
    log "Enabling ufw (non-interactive) ..."
    $SUDO ufw --force enable
  fi
  $SUDO ufw status verbose | sed 's/^/[deploy]   /'
else
  warn "ufw not installed — skipping firewall configuration."
  warn "Ensure ONLY 22, 80, 443/tcp and 443/udp are open via your OVH/cloud"
  warn "firewall. Never expose Postgres (5432) or Redis (6379) to the internet."
fi

# ── 3. Require a SECURE .env (NEVER create or weaken secrets automatically) ───
if [ ! -f .env ]; then
  die ".env is missing.

  This deploy needs a .env with production secrets. It will NOT be created
  automatically. Bootstrap it from the example and fill in real values:

      cp .env.example .env
      \${EDITOR:-nano} .env

  At minimum set, for a PUBLIC server:
      BETBOT_DOMAIN        — your domain, A record already pointing here
      ACME_EMAIL           — Let's Encrypt contact address
      POSTGRES_PASSWORD    — strong random value (NOT the dev default)
      API_BASIC_PASSWORD   — strong random value  (or configure JWT:
                             BETBOT_JWT_SECRET + BETBOT_PASSWORD_HASH)
      BETBOT_HSTS_ENABLED=1
      ANTHROPIC_API_KEY    — optional, enables the AI agent

  Make sure BETBOT_DOMAIN's A record points at this server before deploying.

  Then re-run: ./scripts/deploy_vps.sh"
fi

# Confirm .env is git-ignored so secrets can never be committed. The repo ships
# a .gitignore with `.env`; warn loudly if that protection is ever removed.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git check-ignore -q .env 2>/dev/null; then
    warn "SECURITY: .env is NOT git-ignored! Add '.env' to .gitignore before committing anything."
  fi
fi

# Read a value from .env WITHOUT sourcing it (avoids executing arbitrary content
# and avoids leaking values into the environment/logs). Strips quotes/CR.
env_get() {
  grep -E "^[[:space:]]*$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- \
    | sed -e 's/[[:space:]]*#.*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' -e 's/\r$//' \
    | awk '{$1=$1};1'
}

log "Validating .env for a secure public deployment ..."

# Required, non-empty.
[ -n "$(env_get BETBOT_DOMAIN)" ] || die "BETBOT_DOMAIN is empty in .env — set it to your public domain."
[ -n "$(env_get ACME_EMAIL)" ]    || die "ACME_EMAIL is empty in .env — Let's Encrypt needs a contact address."

# POSTGRES_PASSWORD must be present and must NOT be the well-known dev default.
PG_PWD="$(env_get POSTGRES_PASSWORD)"
[ -n "$PG_PWD" ]                  || die "POSTGRES_PASSWORD is empty in .env — set a strong random value for production."
[ "$PG_PWD" != "betbot_dev_pwd" ] || die "POSTGRES_PASSWORD is still the dev default 'betbot_dev_pwd' — change it before exposing this server."
[ "${#PG_PWD}" -ge 16 ]          || warn "POSTGRES_PASSWORD is shorter than 16 chars — use a stronger value."

# REST API auth MUST be enabled on a public server: either HTTP Basic
# (API_BASIC_PASSWORD non-empty) OR JWT (BETBOT_JWT_SECRET + BETBOT_PASSWORD_HASH).
API_PWD="$(env_get API_BASIC_PASSWORD)"
JWT_SECRET="$(env_get BETBOT_JWT_SECRET)"
JWT_HASH="$(env_get BETBOT_PASSWORD_HASH)"
if [ -n "$API_PWD" ]; then
  log "REST API auth: HTTP Basic enabled (API_BASIC_PASSWORD set)."
elif [ -n "$JWT_SECRET" ] && [ -n "$JWT_HASH" ]; then
  log "REST API auth: JWT enabled (BETBOT_JWT_SECRET + BETBOT_PASSWORD_HASH set)."
else
  die "REST API has NO authentication configured — refusing to expose it publicly.
  Set API_BASIC_PASSWORD to a strong value, OR configure JWT by setting both
  BETBOT_JWT_SECRET and BETBOT_PASSWORD_HASH in .env."
fi

# Never allow the plaintext-password fallback on a public box.
if [ "$(env_get BETBOT_ALLOW_PLAINTEXT_PASSWORD)" = "1" ]; then
  die "BETBOT_ALLOW_PLAINTEXT_PASSWORD=1 is for local dev only — remove it for a public deployment."
fi

# HSTS should be on now that we serve over HTTPS via Caddy.
if [ "$(env_get BETBOT_HSTS_ENABLED)" != "1" ]; then
  warn "BETBOT_HSTS_ENABLED is not 1 — enable it (BETBOT_HSTS_ENABLED=1) for HSTS on the API responses."
fi

log ".env validated — secrets stay in .env and are never printed or logged."

# Best-effort DNS sanity check (non-fatal): does BETBOT_DOMAIN resolve to us?
DOMAIN="$(env_get BETBOT_DOMAIN)"
if [ "$DOMAIN" != "localhost" ] && command -v getent >/dev/null 2>&1; then
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
  if [ -z "$resolved" ]; then
    warn "DNS: '$DOMAIN' does not resolve yet. Add an A record pointing at this server"
    warn "     and wait for propagation, or Let's Encrypt issuance will fail."
  else
    log "DNS: '$DOMAIN' resolves to $resolved (verify this is THIS server's public IP)."
  fi
fi

# ── 4. Optional restore from ./backups BEFORE the app comes up ────────────────
# Backups (created by the `backup` service) are paired files in ./backups:
#   betbot_<YYYYMMDD_HHMMSS>.sql.gz   — plain pg_dump piped through gzip
#   data_<YYYYMMDD_HHMMSS>.tar.gz     — tar of /app/data (tennis ELO, etc.)
# To restore on a fresh server, set RESTORE=1 (and optionally RESTORE_TS to pin
# a timestamp; default = most recent dump). We bring up ONLY the db, wait until
# it is healthy, drop/recreate the database, load the SQL dump, then restore the
# data volume — all BEFORE worker/api/dashboard start, so nothing races the load.
RESTORE="${RESTORE:-0}"
if [ "$RESTORE" = "1" ]; then
  [ -d ./backups ] || die "RESTORE=1 but ./backups directory does not exist."

  if [ -n "${RESTORE_TS:-}" ]; then
    sql_backup="./backups/betbot_${RESTORE_TS}.sql.gz"
    data_backup="./backups/data_${RESTORE_TS}.tar.gz"
  else
    # Pick the newest dump by sorted timestamp in the filename.
    sql_backup="$(ls -1 ./backups/betbot_*.sql.gz 2>/dev/null | sort | tail -n1 || true)"
    if [ -n "$sql_backup" ]; then
      ts="$(basename "$sql_backup" | sed -e 's/^betbot_//' -e 's/\.sql\.gz$//')"
      data_backup="./backups/data_${ts}.tar.gz"
    fi
  fi

  [ -n "${sql_backup:-}" ] && [ -f "$sql_backup" ] || \
    die "RESTORE=1 but no Postgres dump found (looked for ./backups/betbot_*.sql.gz${RESTORE_TS:+ with ts $RESTORE_TS})."

  log "Restore requested. Postgres dump: $sql_backup"
  [ -f "$data_backup" ] && log "Data volume tarball: $data_backup" \
                        || warn "No matching data tarball ($data_backup) — restoring Postgres only."

  log "Bringing up ONLY the db service for restore ..."
  dc up -d db

  log "Waiting for Postgres to become healthy ..."
  for i in $(seq 1 60); do
    if dc exec -T db pg_isready -U betbot -d betbot >/dev/null 2>&1; then
      break
    fi
    [ "$i" -eq 60 ] && die "Postgres did not become ready in time for restore."
    sleep 2
  done

  log "Dropping and recreating database 'betbot' (DESTRUCTIVE) ..."
  dc exec -T db psql -U betbot -d postgres -c "DROP DATABASE IF EXISTS betbot WITH (FORCE);"
  dc exec -T db psql -U betbot -d postgres -c "CREATE DATABASE betbot;"

  log "Restoring Postgres dump (gunzip | psql -U betbot -d betbot) ..."
  gunzip -c "$sql_backup" | dc exec -T db psql -U betbot -d betbot

  if [ -f "$data_backup" ]; then
    log "Restoring data volume into the betbot_data volume (/app/data) ..."
    # Run a throwaway container with the data volume mounted (via the backup
    # service, which mounts betbot_data) and untar the host file into /app/data.
    dc run --rm --no-deps \
      -v "$REPO_ROOT/backups:/host_backups:ro" \
      --entrypoint sh backup \
      -c "mkdir -p /app/data && tar -xzf '/host_backups/$(basename "$data_backup")' -C /app/data"
  fi

  log "Restore complete — proceeding to bring up the full stack."
else
  log "No restore requested (set RESTORE=1 to restore from ./backups before first boot)."
fi

# ── 5. Build + start the production stack (Caddy/HTTPS overlay) ───────────────
log "Pulling base images (best-effort) ..."
dc pull --ignore-buildable || \
  warn "Image pull reported issues; continuing — 'up --build' will rebuild local images."

log "Starting production stack:"
log "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
dc up -d --build

log "Stack status:"
dc ps | sed 's/^/[deploy]   /'

# ── 6. Next steps ─────────────────────────────────────────────────────────────
DOMAIN="${DOMAIN:-<BETBOT_DOMAIN>}"

cat <<EOF

[deploy] ============================================================
[deploy] Deploy complete. Next steps:
[deploy]
[deploy] 1. Watch Caddy obtain the Let's Encrypt certificate:
[deploy]
[deploy]      docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy
[deploy]
[deploy]    Look for a line like "certificate obtained successfully".
[deploy]    The first HTTPS request after issuance may take a few seconds.
[deploy]    (Cert issuance needs ports 80/443 reachable AND BETBOT_DOMAIN's
[deploy]     A record pointing at this server. To avoid Let's Encrypt rate
[deploy]     limits while testing, enable the staging acme_ca line in Caddyfile.)
[deploy]
[deploy] 2. Once the cert is issued, the app is live at:
[deploy]
[deploy]      https://${DOMAIN}/             (dashboard — Streamlit)
[deploy]      https://${DOMAIN}/api/health   (API health check — FastAPI)
[deploy]
[deploy] 3. Tail the worker / api logs if needed:
[deploy]
[deploy]      docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f worker api
[deploy]
[deploy] Re-running this script is safe and idempotent.
[deploy] To restore from ./backups before first boot, re-run with RESTORE=1
[deploy] (optionally RESTORE_TS=YYYYMMDD_HHMMSS).
[deploy] ============================================================
EOF
