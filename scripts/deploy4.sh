#!/usr/bin/env bash
set -u
cd /home/ubuntu/betbot || exit 1
rm -rf /tmp/betbot-build && mkdir -p /tmp/betbot-build || exit 1
tar xzf /tmp/betbot-src.tar.gz -C /tmp/betbot-build || exit 1
docker build -t betbot:latest /tmp/betbot-build > /tmp/build.log 2>&1
rc=$?; echo "BUILD EXIT=$rc"; tail -2 /tmp/build.log
if [ "$rc" -ne 0 ]; then echo ">>> ECHEC — rien redemarre."; exit "$rc"; fi
docker compose -f docker-compose.vps.yml up -d || exit 1
sleep 25
docker ps --filter name=betbot --format '{{.Names}}|{{.Status}}'
echo -n "  api endpoint combines : "; docker exec betbot-api-prod grep -c 'blind-parlays' /app/betbot_api/routers/predictions.py
echo -n "  api module            : "; docker exec betbot-api-prod test -f /app/betbot/blind_parlays.py && echo present || echo ABSENT
echo -n "  dashboard rendu       : "; docker exec betbot-dashboard-prod grep -c '_render_blind_parlays' /app/betbot_dashboard/sections/decision.py
rm -f /tmp/betbot-src.tar.gz; rm -rf /tmp/betbot-build
