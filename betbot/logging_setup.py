"""
Centralized logging configuration.

When LOG_FORMAT=json (default in containers), emits one JSON object per log
line with a stable schema for ingestion by log aggregators (Loki, ELK, etc.).
When LOG_FORMAT=text, emits human-readable plain text — better in a dev TTY.

Schema:
  {
    "ts":       "2026-05-06T14:33:21.000Z",   ISO-8601 UTC, ms precision
    "level":    "INFO" | "WARNING" | ...
    "logger":   "betbot.bankroll",
    "msg":      "Bankroll : deposit +50.00 → balance 150.00",
    "service":  "worker" | "api" | "dashboard" | (auto-detected),
    "thread":   "MainThread" | ...,
    "extra":    { ...arbitrary context fields passed via logger.info(..., extra={...}) }
  }
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler


def _detect_service_name() -> str:
    """Best-effort guess of which container/process we're in."""
    if os.getenv("BETBOT_SERVICE"):
        return os.getenv("BETBOT_SERVICE", "")
    argv0 = " ".join(sys.argv).lower()
    if "uvicorn" in argv0 or "betbot_api" in argv0:
        return "api"
    if "streamlit" in argv0:
        return "dashboard"
    if "betbot.main" in argv0:
        return "worker"
    if "betbot_mcp" in argv0:
        return "mcp"
    return "betbot"


SERVICE = _detect_service_name()


class JSONFormatter(logging.Formatter):
    """One JSON object per log record, suitable for `docker logs` ingestion."""

    # Keys reserved by LogRecord internals that we don't want to dump
    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "asctime", "message", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}Z"
        payload: dict = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": SERVICE,
            "thread": record.threadName,
        }
        # Surface extras (anything passed via extra={...})
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in self._RESERVED and not k.startswith("_")
        }
        if extras:
            payload["extra"] = extras
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """Human-readable formatter for local dev (LOG_FORMAT=text)."""

    def __init__(self):
        super().__init__("%(asctime)s %(levelname)-8s %(name)s — %(message)s")


def configure(log_path: str = "") -> logging.Logger:
    """
    Configure the root `betbot` logger. Idempotent — safe to call multiple
    times. Honors:
      - LOG_FORMAT  : "json" (default in container) | "text"
      - LOG_LEVEL   : "INFO" (default) | "DEBUG" | ...
      - log_path    : optional rotating file (skipped if empty or unwritable)
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt_kind = os.getenv("LOG_FORMAT", "json" if os.path.exists("/.dockerenv") else "text")

    formatter: logging.Formatter
    formatter = JSONFormatter() if fmt_kind == "json" else _TextFormatter()

    root = logging.getLogger("betbot")
    root.setLevel(level)
    # Replace handlers (idempotency)
    root.handlers.clear()

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    root.addHandler(ch)

    if log_path:
        try:
            fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(formatter)
            root.addHandler(fh)
        except (PermissionError, OSError) as exc:
            root.warning("Skipping file log %s : %s (stderr-only)", log_path, exc)

    # Tame noisy third-party loggers
    for noisy in ("httpx", "urllib3", "asyncio", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root
