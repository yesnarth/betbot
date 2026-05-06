"""
BetBot REST API entrypoint (FastAPI).

Run locally:
    uvicorn betbot_api.main:app --reload --port 8000

In Docker (see docker-compose.yml `api` service):
    uvicorn betbot_api.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from betbot.api import OddsAPIClient, SPORT_KEYS
from betbot.config import load_settings
from betbot.db import Database
from betbot.resolver import resolve_pending
from betbot_api.auth import require_auth
from betbot_api.schemas import (
    AgentFilters,
    AgentResponse,
    EventBrief,
    EventsResponse,
    HealthResponse,
    PredictionRow,
    ROIStats,
)

logger = logging.getLogger("betbot_api")
logging.basicConfig(level=logging.INFO)


def get_db() -> Database:
    s = load_settings()
    return Database(s.database_url)


app = FastAPI(
    title="BetBot API",
    description="REST API for BetBot — drives the AI agent and exposes predictions/ROI.",
    version="0.4.0",
)

# Permissive CORS for the local dashboard. Tighten before exposing publicly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health(db: Database = Depends(get_db)) -> HealthResponse:
    s = load_settings()
    n_teams = sum(len(db.get_all_team_stats_for_league(k)) for k in SPORT_KEYS)
    return HealthResponse(
        status="ok",
        teams_in_db=n_teams,
        scan_hours=s.scan_hours,
        bankroll=s.bankroll,
        agent_enabled=bool(s.anthropic_api_key),
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

    from betbot_mcp.server import _filter_today
    by_sport: dict[str, list[EventBrief]] = {}
    for sk, events in all_events.items():
        kept = _filter_today(events, s.min_before_kickoff) if today_only else events
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


# ---------------------------------------------------------------------------
# Predictions tracking + ROI
# ---------------------------------------------------------------------------

@app.get("/predictions/pending", response_model=list[PredictionRow])
def pending_predictions(
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[PredictionRow]:
    rows = db.get_pending_predictions()
    return [PredictionRow(**r) for r in rows]


@app.post("/predictions/resolve")
def resolve(
    days_from: int = Query(default=3, ge=1, le=3),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    s = load_settings()
    client = OddsAPIClient(s.odds_api_key)
    return resolve_pending(db, client, days_from=days_from)


@app.get("/stats/roi", response_model=ROIStats)
def roi(
    days: int = Query(default=30, ge=1, le=365),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> ROIStats:
    return ROIStats(**db.get_roi_stats(days=days))


@app.post("/stats/backtest")
def backtest(
    sport_key: str = Query(..., description="Sport key (e.g. soccer_epl)"),
    n_holdout: int = Query(default=100, ge=20, le=500),
    _: str = Depends(require_auth),
) -> dict:
    """
    Run a Brier-score / log-loss / calibration backtest on recent matches.
    Synchronous: takes 5-15 s depending on the league size.
    """
    from betbot.backtest import run_backtest
    s = load_settings()
    result = run_backtest(sport_key, s.football_data_api_key, n_holdout)
    return {
        "sport_key": result.sport_key,
        "n_matches": result.n_matches,
        "brier_score": result.brier_score,
        "log_loss": result.log_loss,
        "calibration": result.calibration,
        "notes": result.notes,
    }


# ---------------------------------------------------------------------------
# AI Agent — the dashboard's killer feature
# ---------------------------------------------------------------------------

@app.post("/agent/recommend", response_model=AgentResponse)
async def agent_recommend(
    filters: AgentFilters,
    _: str = Depends(require_auth),
) -> AgentResponse:
    """
    Ask the AI agent to recommend bets matching the user's filters.
    The agent uses MCP tools (predict_match, find_value_bets, build_parlay, ...)
    to reason and return a JSON-shaped recommendation.
    """
    s = load_settings()
    if not s.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "AI agent is disabled — set ANTHROPIC_API_KEY in .env. "
                "Other endpoints (events, predictions, roi) keep working."
            ),
        )

    # Imported lazily so the API boots even when claude-agent-sdk isn't healthy.
    from betbot_api.agent import run_agent

    result = await run_agent(filters.model_dump(exclude_none=True), trigger="api")
    return AgentResponse(**result)
