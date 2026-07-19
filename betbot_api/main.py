"""
BetBot REST API entrypoint (FastAPI).

Run locally:
    uvicorn betbot_api.main:app --reload --port 8000

In Docker (see docker-compose.yml `api` service):
    uvicorn betbot_api.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging

import os

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from betbot.api import OddsAPIClient, SPORT_KEYS
from betbot.config import load_settings
from betbot.db import Database
from betbot_api.auth import require_auth
from betbot_api.deps import get_db, limiter
from betbot_api.routers import (
    agent_runs as agent_runs_router,
    arbitrage as arbitrage_router,
    bankroll as bankroll_router,
    basketball as basketball_router,
    ml as ml_router,
    predictions as predictions_router,
    promotions as promotions_router,
    recommend as recommend_router,
    settings as settings_router,
    sources_health as sources_health_router,
    stats as stats_router,
    tennis as tennis_router,
)
from betbot_api.schemas import EventBrief, EventsResponse, HealthResponse

logger = logging.getLogger("betbot_api")
logging.basicConfig(level=logging.INFO)


app = FastAPI(
    title="BetBot API",
    description="REST API for BetBot — drives the AI agent and exposes predictions/ROI.",
    version="1.0.0",
)

# Rate limiter is owned by betbot_api.deps so it's importable by routers
# without creating a main↔routers circular dependency.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — strict in prod (BETBOT_DOMAIN), permissive only in local dev.
_cors_origins: list[str] = []
_prod_domain = os.getenv("BETBOT_DOMAIN", "").strip()
if _prod_domain:
    _cors_origins = [f"https://{_prod_domain}", f"http://{_prod_domain}"]
else:
    _cors_origins = ["http://localhost:8501", "http://127.0.0.1:8501"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["authorization", "content-type", "x-requested-with"],
)


# Security headers middleware — defends against MIME sniffing, click-jacking,
# information leakage via referrer, and (when behind HTTPS) downgrade attacks.
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), geolocation=(), payment=()")
        # HSTS only when running behind HTTPS — opt-in via env to avoid breaking
        # local HTTP development.
        if os.getenv("BETBOT_HSTS_ENABLED", "0") == "1":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains",
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Routers — endpoints grouped by resource for readability and isolation
# ---------------------------------------------------------------------------

app.include_router(agent_runs_router.router)
app.include_router(arbitrage_router.router)
app.include_router(bankroll_router.router)
app.include_router(basketball_router.router)
app.include_router(ml_router.router)
app.include_router(predictions_router.router)
app.include_router(promotions_router.router)
app.include_router(recommend_router.router)
app.include_router(settings_router.router)
app.include_router(sources_health_router.router)
app.include_router(stats_router.router)
app.include_router(tennis_router.router)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.post("/auth/login")
@limiter.limit("5/minute")  # protect against brute-force
def auth_login(request: Request,
               username: str = Query(...),
               password: str = Query(...)) -> dict:
    """
    Issue a JWT for valid credentials. Returns 401 otherwise.

    Active when BETBOT_JWT_SECRET is set. Otherwise the dashboard / API
    keep using HTTP Basic. Token TTL = BETBOT_JWT_TTL_MIN minutes (default 60).
    """
    from betbot_api.jwt_auth import (
        authenticate_user, create_access_token, jwt_enabled,
    )
    if not jwt_enabled():
        raise HTTPException(status_code=503,
                            detail="JWT disabled (BETBOT_JWT_SECRET not set)")
    if not authenticate_user(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(subject=username)
    return {"access_token": token, "token_type": "bearer",
            "expires_in_min": int(os.getenv("BETBOT_JWT_TTL_MIN", "60"))}


@app.get("/health", response_model=HealthResponse)
def health(db: Database = Depends(get_db)) -> HealthResponse:
    """
    Liveness + readiness probe.

    Exercises a real `SELECT 1` against Postgres so the check fails when the
    DB is unreachable, frozen, or in read-only failover state. If the SELECT
    raises, returns HTTP 503 instead of a stale "ok" payload.
    """
    from time import monotonic
    from sqlalchemy import text
    from betbot.bankroll import get_state
    from betbot.api import OddsAPIClient, _quota_minimum, _enabled_sport_keys
    from betbot.database import session_scope

    s = load_settings()

    # ---- DB probe : SELECT 1 with latency measurement ----------------------
    db_ok = False
    db_latency_ms = -1
    try:
        t0 = monotonic()
        with session_scope() as session:
            session.execute(text("SELECT 1")).scalar_one()
        db_latency_ms = int((monotonic() - t0) * 1000)
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.error("Health DB probe failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")

    n_teams = sum(len(db.get_all_team_stats_for_league(k)) for k in SPORT_KEYS)
    bankroll_state = get_state()

    # Probe the Odds API quota + active sports — both use the free /v4/sports endpoint
    quota_remaining = -1
    quota_min = _quota_minimum()
    quota_exhausted = False
    active_sports: list[str] = []
    try:
        client = OddsAPIClient(s.odds_api_key)
        active = client.get_active_sports()
        quota_remaining = client.quota_remaining
        quota_exhausted = quota_remaining >= 0 and quota_remaining < quota_min
        wishlist = _enabled_sport_keys()
        active_sports = [k for k in wishlist if k in active]
    except Exception:
        pass

    return HealthResponse(
        status="ok",
        teams_in_db=n_teams,
        scan_hours=s.scan_hours,
        bankroll_initial=s.bankroll,
        balance=bankroll_state.balance,
        available=bankroll_state.available,
        agent_enabled=bool(s.anthropic_api_key),
        odds_quota_remaining=quota_remaining,
        odds_quota_minimum=quota_min,
        odds_quota_exhausted=quota_exhausted,
        active_sports=active_sports,
        db_latency_ms=db_latency_ms,
    )


# ---------------------------------------------------------------------------
# Events / odds
# ---------------------------------------------------------------------------

@app.get("/events", response_model=EventsResponse)
def list_events(
    sport_key: str | None = Query(default=None, description="Optional league filter"),
    today_only: bool = Query(default=True),
    _: str = Depends(require_auth),
) -> EventsResponse:
    s = load_settings()
    client = OddsAPIClient(s.odds_api_key)
    if sport_key:
        all_events = {sport_key: client.get_events_with_odds(sport_key)}
    else:
        all_events = client.fetch_all_sports()

    from betbot.shared import filter_upcoming_today
    by_sport: dict[str, list[EventBrief]] = {}
    for sk, events in all_events.items():
        kept = filter_upcoming_today(events, s.min_before_kickoff) if today_only else events
        if kept:
            by_sport[sk] = [
                EventBrief(
                    event_id=e.get("id"),
                    sport_key=sk,
                    home_team=e.get("home_team"),
                    away_team=e.get("away_team"),
                    commence_time=e.get("commence_time"),
                    n_bookmakers=len(e.get("bookmakers", [])),
                )
                for e in kept
            ]

    total = sum(len(v) for v in by_sport.values())
    return EventsResponse(total=total, by_sport=by_sport, today_only=today_only)


# All endpoints live in betbot_api/routers/. The only routes defined directly
# on the app object are /auth/login, /health, and /events (above).

