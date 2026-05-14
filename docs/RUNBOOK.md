# BetBot — Runbook

Quick recipes for diagnostics, recovery, and rollback. Skim this when something
breaks at 3am.

---

## Health check — is everything actually working?

```bash
docker compose ps                          # all containers up + healthy?
curl -sS http://127.0.0.1:8000/health      # API + DB probe, JSON response
curl -sS http://127.0.0.1:8501             # dashboard responds 200
```

The dashboard's **Système → Sources** tab probes every external API (Odds,
football-data, Tavily, Open-Meteo, Anthropic) with per-source timeouts.
Faster than reading logs.

---

## Migration rollback

Alembic migrations are forward and reverse — every revision defines `upgrade()`
and `downgrade()`. List of revisions: `alembic/versions/*.py`.

```bash
# Inspect current state
docker compose exec api alembic current
docker compose exec api alembic history --rev-range base:head

# Roll back ONE step (typical after a bad deploy)
docker compose exec api alembic downgrade -1

# Roll back to a specific revision
docker compose exec api alembic downgrade <revision_id>

# Catastrophic: roll all the way back to empty
docker compose exec api alembic downgrade base
```

**Before rolling back in production:**

1. Take a fresh backup: `docker compose exec backup sh /usr/local/bin/backup_db.sh`
2. Stop the worker (which writes to DB):
   `docker compose stop worker`
3. Run the downgrade
4. Restart the worker once you've confirmed the schema is sane:
   `docker compose up -d worker`

If a migration's `downgrade()` is missing or broken, you cannot reverse it
cleanly. Restore from the backup taken in step 1 instead (see next section).

---

## Restoring from backup

Local backups live in `./backups/` as paired `betbot_<ts>.sql.gz` and
`data_<ts>.tar.gz`. The most recent file is the most accurate; older files
are 1 per day going back 30 days (`BACKUP_RETENTION_DAYS`).

```bash
# Pick the file you want to restore from
ls -lh ./backups/ | tail

# Stop services that might race writes
docker compose stop worker api dashboard

# Drop & recreate the database  (DESTRUCTIVE — that's the point)
docker compose exec db psql -U betbot -d postgres -c "DROP DATABASE betbot;"
docker compose exec db psql -U betbot -d postgres -c "CREATE DATABASE betbot;"

# Restore Postgres dump
gunzip -c ./backups/betbot_20260514_030000.sql.gz | \
  docker compose exec -T db psql -U betbot -d betbot

# Restore data volume (tennis ELO, basketball stats, calibrator)
docker compose run --rm -v "$(pwd)/backups:/host_backups" --entrypoint sh backup \
  -c "tar -xzf /host_backups/data_20260514_030000.tar.gz -C /app/data"

# Restart
docker compose up -d
```

**Offsite backups** (if configured via `BACKUP_REMOTE_TARGET`): list and
download via `rclone ls <target>` and `rclone copy <target>/<file> ./backups/`.

---

## Odds API quota exhausted

Symptom: dashboard banner "Quota Odds API épuisé" and scans return empty.

```bash
# Check the live quota
curl -sS http://127.0.0.1:8000/health | grep odds_quota

# Lower the safety floor if you want to keep scanning to the very last request
echo "ODDS_QUOTA_MINIMUM=5" >> .env       # default is 20
docker compose up -d --force-recreate api worker

# Wait it out — The Odds API free tier resets on the 1st of each month
```

Reduce per-scan cost by:
- Disabling `MULTI_SPORT_TENNIS` / `MULTI_SPORT_BASKETBALL` if you only care about soccer
- Increasing `MIN_BEFORE_KICKOFF` so the worker filters out far-future matches earlier
- Cutting `SCAN_HOURS` from 3 daily scans to 2

---

## Bankroll looks wrong

The bankroll is an append-only ledger (`bankroll_ledger` table). Each row's
`balance_after` is the running sum at insertion time. If the displayed
balance disagrees with `SUM(amount)`, the ledger has been tampered with.

```bash
# Verify ledger integrity
docker compose exec db psql -U betbot -d betbot -c "
  SELECT
    SUM(amount)::numeric(10,2) AS computed_balance,
    (SELECT balance_after FROM bankroll_ledger
     ORDER BY id DESC LIMIT 1)::numeric(10,2) AS last_row_balance
  FROM bankroll_ledger;
"
```

If `computed_balance != last_row_balance`, restore from backup.

To manually correct without restoring (small drift only):

```bash
curl -X POST http://127.0.0.1:8000/bankroll/withdraw \
  -H "Content-Type: application/json" \
  -d '{"amount": 0.50, "note": "ledger correction"}'
```

---

## Calibrator producing absurd outputs

Symptom: every `model_prob` after calibration collapses to near-0 or near-1.
Usually means the training data was insufficient or pathological.

```bash
# Inspect the calibrator
curl -sS http://127.0.0.1:8000/ml/calibrator/status

# Wipe it (calibration becomes a no-op — raw model probs used)
docker compose exec api rm -f /app/data/calibrator.json

# Re-train from a cold start (synthetic samples from 5 leagues)
curl -X POST http://127.0.0.1:8000/ml/calibrator/cold-start

# Or wait for the weekly retrain on Sunday 03:30 UTC
```

---

## Worker scheduler hung / missed jobs

The worker exposes a deep healthcheck at `http://worker:8001/`. It returns
503 when no job has fired in 25h (covers any of the 3 daily scans missing).

```bash
docker compose exec worker python -c "
import urllib.request
import json
r = urllib.request.urlopen('http://localhost:8001/').read()
print(json.dumps(json.loads(r), indent=2))
"
```

Restart the worker to recover:

```bash
docker compose restart worker
# Or, if the image is stale:
docker compose up -d --force-recreate worker
```

The next scheduled scan will catch up via APScheduler's `misfire_grace_time`
(4 h for daily scans, 1 h for the weekly stats refresh).

---

## Emergency: stop everything

Useful when something is going wrong and you want to freeze state for
inspection before recovery.

```bash
# Stop without losing volumes
docker compose stop

# Inspect logs at leisure
docker compose logs --tail=200 worker
docker compose logs --tail=200 api

# Bring back up
docker compose up -d
```

---

## Where things live

| Resource | Location |
|---|---|
| Predictions, bankroll ledger, agent runs | Postgres `betbot` database |
| Tennis ELO ratings | `data/tennis_elo.json` (Docker volume `betbot_data`) |
| Basketball stats | `data/basketball_teams.json` |
| ML calibrator | `data/calibrator.json` |
| Source health snapshot | `data/source_health.json` |
| Daily backups | `./backups/` (host) |
| Logs | `docker compose logs <service>` (no host file) |
