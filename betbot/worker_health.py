"""
Worker health endpoint — minimal HTTP server embedded in the scheduler process.

The Docker `worker` container by default has no port to probe. Past versions
relied on `ps grep python` which only catches a dead process — not a wedged
APScheduler. This module starts a tiny HTTP server on port 8001 inside the
worker process; it returns 200 only if:

  - the scheduler instance is running,
  - at least one job has fired in the last `STALE_AFTER_SECS` seconds
    (or no job has fired yet AND the scheduler started < grace period ago).

Compose's `healthcheck:` polls this URL.

Why HTTP and not a file/lock-file? Files don't survive container restarts the
way a fresh in-process counter does, and we want the probe to truly exercise
the scheduler thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("betbot.worker_health")

# How long a job-less scheduler is allowed to run before we report unhealthy.
GRACE_PERIOD_SECS = 30 * 60          # 30 min — first scheduled scan is at most ~hours away
STALE_AFTER_SECS = 25 * 3600         # 25 hours — flag if no job fired in over a day


class WorkerHealthState:
    """Singleton pattern: a single instance shared between the scheduler
    callbacks and the HTTP probe handler."""

    _instance: "WorkerHealthState | None" = None

    def __init__(self) -> None:
        self.scheduler = None                  # APScheduler reference
        self.process_started_at = time.time()
        self.last_job_fired_at: float | None = None
        self.last_job_name: str | None = None
        self.last_job_error: str | None = None

    @classmethod
    def get(cls) -> "WorkerHealthState":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def record_job_fired(self, name: str, error: str | None = None) -> None:
        self.last_job_fired_at = time.time()
        self.last_job_name = name
        self.last_job_error = error

    def to_dict(self) -> dict:
        now = time.time()
        uptime = now - self.process_started_at
        scheduler_running = bool(self.scheduler and self.scheduler.running)

        # Job freshness check
        if self.last_job_fired_at is None:
            seconds_since_last_job = None
            healthy_jobs = uptime < GRACE_PERIOD_SECS  # first run grace
        else:
            seconds_since_last_job = now - self.last_job_fired_at
            healthy_jobs = seconds_since_last_job < STALE_AFTER_SECS

        # APScheduler exposes pending jobs; we report the next 5 firing times
        next_runs: list[dict] = []
        if scheduler_running:
            try:
                for job in self.scheduler.get_jobs():
                    next_runs.append({
                        "id": job.id,
                        "name": job.name,
                        "next_run_time": (
                            job.next_run_time.isoformat() if job.next_run_time else None
                        ),
                    })
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not enumerate scheduler jobs: %s", exc)

        ok = scheduler_running and healthy_jobs

        return {
            "ok": ok,
            "scheduler_running": scheduler_running,
            "uptime_seconds": int(uptime),
            "last_job_fired_at": (
                datetime.fromtimestamp(self.last_job_fired_at, tz=timezone.utc).isoformat()
                if self.last_job_fired_at else None
            ),
            "last_job_name": self.last_job_name,
            "last_job_error": self.last_job_error,
            "seconds_since_last_job": (
                int(seconds_since_last_job) if seconds_since_last_job is not None else None
            ),
            "stale_threshold_seconds": STALE_AFTER_SECS,
            "next_runs": next_runs[:5],
        }


# ---------------------------------------------------------------------------
# Tiny HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default access logs
        pass

    def do_GET(self):
        state = WorkerHealthState.get().to_dict()
        body = json.dumps(state).encode("utf-8")
        self.send_response(200 if state["ok"] else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server(port: int = 8001) -> None:
    """Start the health server in a daemon thread. Idempotent."""
    state = WorkerHealthState.get()
    if getattr(state, "_http_started", False):
        return

    def _serve():
        try:
            server = HTTPServer(("0.0.0.0", port), _Handler)
            logger.info("Worker health HTTP server listening on :%d", port)
            server.serve_forever()
        except Exception as exc:  # noqa: BLE001
            logger.error("Worker health server crashed: %s", exc)

    t = threading.Thread(target=_serve, name="worker-health-http", daemon=True)
    t.start()
    state._http_started = True  # type: ignore[attr-defined]
