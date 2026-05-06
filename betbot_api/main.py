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
    LocalAgentFilters,
    LocalAgentResponse,
    ManualScanFilters,
    ManualScanResponse,
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

@app.post("/recommend/manual", response_model=ManualScanResponse)
def recommend_manual(
    filters: ManualScanFilters,
    _: str = Depends(require_auth),
) -> ManualScanResponse:
    """
    Deterministic scan — no AI. Runs the same Dixon-Coles + xG + ELO blended
    model the worker uses, applies the user's filters, and returns picks +
    parlays. Free to run, repeatable, no external API costs beyond the Odds API
    quota that the underlying fetch_events consumes.

    Use this when:
      - you don't have ANTHROPIC_API_KEY yet
      - you want a quick repeatable scan without AI overhead
      - you want a deterministic baseline to compare against the AI agent
    """
    from betbot.analysis import build_parlays, detect_value_bets, rank_value_bets
    from betbot.main import _filter_upcoming_today, _load_team_stats_from_db
    from betbot_api.main import get_db as _get_db  # avoid circular import flicker

    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    # Resolve filters with defaults from settings
    edge = filters.min_edge if filters.min_edge is not None else s.min_value_edge
    prob = filters.min_prob if filters.min_prob is not None else s.min_model_prob
    odds = filters.min_odds if filters.min_odds is not None else s.min_book_odds

    # Fetch events
    if filters.sport_key:
        all_events = {filters.sport_key: odds_client.get_events_with_odds(filters.sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    # Filter today vs upcoming
    events_by_sport: dict[str, list[dict]] = {}
    for sk, ev in all_events.items():
        kept = _filter_upcoming_today(ev, s.min_before_kickoff) if filters.today_only else ev
        if kept:
            events_by_sport[sk] = kept

    n_events = sum(len(v) for v in events_by_sport.values())
    if n_events == 0:
        return ManualScanResponse(
            picks=[], parlays=[], n_picks=0, n_parlays=0,
            filters_used=filters.model_dump(),
            n_events_scanned=0,
        )

    # Run the blended Poisson + ELO model
    db = Database(s.database_url)
    prebuilt = _load_team_stats_from_db(db, events_by_sport.keys())
    raw = detect_value_bets(
        events_by_sport=events_by_sport,
        match_history_by_sport={},
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        min_value_edge=edge,
        min_model_prob=prob,
        min_book_odds=odds,
        prebuilt_stats_by_sport=prebuilt,
    )
    ranked = rank_value_bets(raw)[: s.top_bets]
    parlays = build_parlays(ranked, n_legs=filters.n_legs, top_n=filters.n_combos)

    # Convert to dicts
    def bet_to_dict(b):
        return {
            "event_id": b.event_id,
            "sport_key": b.sport_key,
            "league": b.league_label,
            "home_team": b.home_team,
            "away_team": b.away_team,
            "market": b.market,
            "selection_code": b.selection_code,
            "selection_label": b.selection_label,
            "model_prob": b.model_prob,
            "best_odds": b.best_odds,
            "best_book": b.best_book,
            "value_edge": b.value_edge,
            "kelly_stake": b.kelly_stake,
            "model_type": b.model_type,
        }

    picks_out = [bet_to_dict(b) for b in ranked]
    parlays_out = [
        {
            "n_legs": len(p.bets),
            "combined_odds": p.combined_odds,
            "combined_prob": p.combined_prob,
            "combined_ev_pct": p.combined_ev,
            "legs": [bet_to_dict(b) for b in p.bets],
        }
        for p in parlays
    ]
    return ManualScanResponse(
        picks=picks_out,
        parlays=parlays_out,
        n_picks=len(picks_out),
        n_parlays=len(parlays_out),
        filters_used={
            "sport_key": filters.sport_key,
            "today_only": filters.today_only,
            "min_edge": edge,
            "min_prob": prob,
            "min_odds": odds,
            "n_legs": filters.n_legs,
            "n_combos": filters.n_combos,
        },
        n_events_scanned=n_events,
    )


@app.post("/recommend/agent-local", response_model=LocalAgentResponse)
def recommend_agent_local(
    filters: LocalAgentFilters,
    _: str = Depends(require_auth),
) -> LocalAgentResponse:
    """
    Local deterministic agent — no AI, no recurring cost.

    Pipeline:
      1. Run the same blended Poisson model as /recommend/manual to get raw picks
      2. For each pick, run a chain of explicit business rules:
           - injury news on either team (Tavily)
           - coach sacking / locker-room drama (Tavily)
           - bad weather on Over picks (Open-Meteo)
           - ELO contradiction (Club Elo)
           - over-confidence cap (model_prob > 85% → penalty)
           - huge edge without supporting news (>35% edge → strong penalty)
      3. Recompute the edge from the calibrated probability
      4. Reject picks whose final edge falls below min_final_edge
      5. Build parlays only from accepted picks

    Cheaper and more transparent than the Claude agent. Each rule is explicit
    Python code with a clear name in the rationale.
    """
    from betbot.analysis import build_parlays, ValueBet
    from betbot.local_agent import evaluate_picks
    from betbot.main import _filter_upcoming_today, _load_team_stats_from_db

    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    edge = filters.min_edge if filters.min_edge is not None else s.min_value_edge
    prob = filters.min_prob if filters.min_prob is not None else s.min_model_prob
    odds = filters.min_odds if filters.min_odds is not None else s.min_book_odds

    # 1. Fetch events
    if filters.sport_key:
        all_events = {filters.sport_key: odds_client.get_events_with_odds(filters.sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    commence_by_id: dict[str, str] = {}
    for sk, ev in all_events.items():
        kept = _filter_upcoming_today(ev, s.min_before_kickoff) if filters.today_only else ev
        if kept:
            events_by_sport[sk] = kept
            for e in kept:
                if e.get("id"):
                    commence_by_id[e["id"]] = e.get("commence_time", "")

    if not events_by_sport:
        return LocalAgentResponse(
            picks=[], rejected=[], parlays=[],
            n_picks_in=0, n_accepted=0, n_rejected=0, n_parlays=0,
            n_news_calls=0, n_weather_calls=0, tavily_available=False,
        )

    # 2. Run the blended model to produce raw picks
    from betbot.analysis import detect_value_bets, rank_value_bets
    db = Database(s.database_url)
    prebuilt = _load_team_stats_from_db(db, events_by_sport.keys())
    raw_bets = detect_value_bets(
        events_by_sport=events_by_sport,
        match_history_by_sport={},
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        min_value_edge=edge,
        min_model_prob=prob,
        min_book_odds=odds,
        prebuilt_stats_by_sport=prebuilt,
    )
    ranked = rank_value_bets(raw_bets)[: s.top_bets]

    # 3. Convert to plain dicts and inject commence_time so the agent can fetch weather
    def bet_to_dict(b: ValueBet) -> dict:
        return {
            "event_id": b.event_id,
            "sport_key": b.sport_key,
            "league": b.league_label,
            "home_team": b.home_team,
            "away_team": b.away_team,
            "market": b.market,
            "selection_code": b.selection_code,
            "selection_label": b.selection_label,
            "model_prob": b.model_prob,
            "best_odds": b.best_odds,
            "best_book": b.best_book,
            "value_edge": b.value_edge,
            "kelly_stake": b.kelly_stake,
            "model_type": b.model_type,
            "commence_time": commence_by_id.get(b.event_id, ""),
        }

    raw_picks = [bet_to_dict(b) for b in ranked]

    # 4. Run through the rule chain (Kelly stakes are recomputed inside)
    eval_result = evaluate_picks(
        raw_picks,
        fetch_news=filters.fetch_news,
        fetch_weather=filters.fetch_weather,
        min_final_edge=filters.min_final_edge,
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
    )

    # 5. Build parlays from accepted picks only — reconstruct ValueBet objects
    accepted_bets = [
        ValueBet(
            event_id=p["event_id"],
            sport_key=p["sport_key"],
            home_team=p["home_team"],
            away_team=p["away_team"],
            league_label=p.get("league", ""),
            market=p["market"],
            selection_code=p["selection_code"],
            selection_label=p["selection_label"],
            model_prob=p["model_prob"],
            best_odds=p["best_odds"],
            best_book=p["best_book"],
            value_edge=p["value_edge"],
            kelly_stake=p.get("kelly_stake", 0.0),
            lambda_home=None,
            lambda_away=None,
            model_type=p.get("model_type", "poisson"),
        )
        for p in eval_result["picks"]
    ]
    parlays = build_parlays(accepted_bets, n_legs=filters.n_legs, top_n=filters.n_combos)
    parlays_out = [
        {
            "n_legs": len(p.bets),
            "combined_odds": p.combined_odds,
            "combined_prob": p.combined_prob,
            "combined_ev_pct": p.combined_ev,
            "legs": [bet_to_dict(b) for b in p.bets],
        }
        for p in parlays
    ]

    return LocalAgentResponse(
        picks=eval_result["picks"],
        rejected=eval_result["rejected"],
        parlays=parlays_out,
        n_picks_in=len(raw_picks),
        n_accepted=eval_result["n_accepted"],
        n_rejected=eval_result["n_rejected"],
        n_parlays=len(parlays_out),
        n_news_calls=eval_result["n_news_calls"],
        n_weather_calls=eval_result["n_weather_calls"],
        tavily_available=eval_result["tavily_available"],
    )


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
