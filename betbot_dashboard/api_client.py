"""HTTP client for the FastAPI backend, with optional Basic Auth."""
from __future__ import annotations

import os
from typing import Any

import httpx

API_URL = os.getenv("BETBOT_API_URL", "http://localhost:8000").rstrip("/")
BASIC_USER = os.getenv("API_BASIC_USER", "betbot")
BASIC_PASSWORD = os.getenv("API_BASIC_PASSWORD", "")
AUTH = (BASIC_USER, BASIC_PASSWORD) if BASIC_PASSWORD else None


def api_get(path: str, **params: Any) -> Any:
    r = httpx.get(f"{API_URL}{path}", params=params, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


# Some POST endpoints can run for 8+ minutes (Claude agent with ambitious
# "5 parlays of 5 legs" requests, cold-start calibration running 5 backtests
# back-to-back, etc.). Picking 900s as the default keeps the dashboard from
# giving up before the backend does. The agent_run row is persisted whether
# or not the HTTP response makes it back, so a frontend timeout only loses
# the live response — the result is always recoverable from Historique IA.
LONG_RUNNING_TIMEOUT = 900


def api_post(
    path: str,
    json: dict | None = None,
    headers: dict[str, str] | None = None,
    **params: Any,
) -> Any:
    r = httpx.post(
        f"{API_URL}{path}",
        params=params,
        json=json,
        headers=headers,
        auth=AUTH,
        timeout=LONG_RUNNING_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()
