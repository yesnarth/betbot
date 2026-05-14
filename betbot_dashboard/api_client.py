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


def api_post(path: str, json: dict | None = None, **params: Any) -> Any:
    r = httpx.post(f"{API_URL}{path}", params=params, json=json, auth=AUTH, timeout=180)
    r.raise_for_status()
    return r.json()
