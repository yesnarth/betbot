"""HTTP client for the FastAPI backend, with optional Basic Auth.

Every network/HTTP failure is normalized to `ApiError`, which carries a short
user-facing French message (`.user_message`) and the HTTP status when known.
The dashboard never surfaces a raw traceback for a backend hiccup — section
renderers are wrapped by betbot_dashboard.ui.guarded, and ad-hoc call sites can
catch ApiError directly.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

API_URL = os.getenv("BETBOT_API_URL", "http://localhost:8000").rstrip("/")
BASIC_USER = os.getenv("API_BASIC_USER", "betbot")
BASIC_PASSWORD = os.getenv("API_BASIC_PASSWORD", "")
AUTH = (BASIC_USER, BASIC_PASSWORD) if BASIC_PASSWORD else None


# Some POST endpoints can run for 8+ minutes (Claude agent with ambitious
# "5 parlays of 5 legs" requests, cold-start calibration running 5 backtests
# back-to-back, etc.). Picking 900s as the default keeps the dashboard from
# giving up before the backend does. The agent_run row is persisted whether
# or not the HTTP response makes it back, so a frontend timeout only loses
# the live response — the result is always recoverable from Historique IA.
LONG_RUNNING_TIMEOUT = 900


class ApiError(Exception):
    """A backend call failed. `user_message` is safe to display to the user."""

    def __init__(self, user_message: str, status_code: int | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


def _extract_detail(resp: httpx.Response) -> str:
    """Pull FastAPI's {"detail": ...} (or pydantic errors) out of an error body."""
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip()[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list) and detail:  # pydantic validation errors
        first = detail[0]
        if isinstance(first, dict):
            return str(first.get("msg", first))
    return str(detail) if detail else ""


def _friendly_status(resp: httpx.Response) -> str:
    base = {
        400: "Requête invalide",
        401: "Authentification refusée (vérifie API_BASIC_PASSWORD)",
        403: "Accès refusé",
        404: "Ressource introuvable",
        409: "Conflit (l'état a peut-être changé entre-temps)",
        422: "Données invalides",
        429: "Trop de requêtes — réessaie dans un instant",
        500: "Erreur interne du backend",
        503: "Backend indisponible — un service démarre peut-être encore",
    }.get(resp.status_code, f"Erreur HTTP {resp.status_code}")
    detail = _extract_detail(resp)
    return f"{base} : {detail}" if detail else base


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> Any:
    try:
        r = httpx.request(
            method, f"{API_URL}{path}",
            params=params, json=json, headers=headers, auth=AUTH, timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        raise ApiError(_friendly_status(exc.response), exc.response.status_code) from exc
    except httpx.TimeoutException as exc:
        raise ApiError(f"Le backend a mis trop de temps à répondre (timeout {int(timeout)} s).") from exc
    except httpx.RequestError as exc:
        raise ApiError(
            f"API injoignable sur {API_URL}. Vérifie que les containers tournent "
            f"(`docker compose ps`)."
        ) from exc


def api_get(path: str, **params: Any) -> Any:
    return _request("GET", path, params=params, timeout=30)


def api_post(
    path: str,
    json: dict | None = None,
    headers: dict[str, str] | None = None,
    **params: Any,
) -> Any:
    return _request("POST", path, params=params, json=json, headers=headers,
                    timeout=LONG_RUNNING_TIMEOUT)
