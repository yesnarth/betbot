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
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from betbot.api import OddsAPIClient, SPORT_KEYS
from betbot.config import load_settings
from betbot.db import Database
from betbot.resolver import resolve_pending
from betbot_api.auth import require_auth
from betbot_api.schemas import (
    AgentFilters,
    AgentResponse,
    BankrollLedgerRow,
    BankrollMutation,
    BankrollSnapshot,
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
    version="1.0.0",
)

# Rate limiting (per remote IP). Tighter limits on expensive endpoints below.
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
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

@app.post("/predictions/{prediction_id}/confirm-placed")
@limiter.limit("30/minute")
def confirm_placed(
    request: Request,
    prediction_id: int,
    body: dict | None = None,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Confirm the user actually placed this bet at their bookmaker.
    THIS is when the bankroll is debited (atomic with the placement-status
    update). Pre-flight guard checks (stop-loss, daily cap, exposure)
    apply here, NOT at scan time.

    Body (optional): {"bookmaker": "pinnacle", "unconfirm": false}.
    Set unconfirm=true to revert (re-credits the stake).
    """
    from betbot.bankroll import InsufficientFundsError
    from betbot.guards import GuardViolation
    body = body or {}
    bookmaker = body.get("bookmaker")
    unconfirm = bool(body.get("unconfirm", False))
    try:
        ok = db.confirm_prediction_placed(prediction_id, bookmaker, unconfirm=unconfirm)
    except InsufficientFundsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GuardViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Prediction {prediction_id} not found")
    return {"prediction_id": prediction_id, "actually_placed": not unconfirm,
            "bookmaker": bookmaker}


@app.post("/predictions/{prediction_id}/skip")
@limiter.limit("30/minute")
def skip_prediction(
    request: Request,
    prediction_id: int,
    body: dict | None = None,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Skip a proposed pick — the user passed on the recommendation.
    No bankroll movement. Kept in DB for analytics (would-have ROI).
    """
    body = body or {}
    reason = body.get("reason", "user_skipped")
    try:
        ok = db.skip_prediction(prediction_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Prediction {prediction_id} not found")
    return {"prediction_id": prediction_id, "placement_status": "skipped",
            "reason": reason}


@app.post("/predictions/{prediction_id}/unskip")
@limiter.limit("30/minute")
def unskip_prediction(
    request: Request,
    prediction_id: int,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Revert a skipped pick back to 'proposed'. Useful when the user clicked
    skip by mistake. Refused for picks already confirmed.
    """
    try:
        ok = db.unskip_prediction(prediction_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Prediction {prediction_id} not found")
    return {"prediction_id": prediction_id, "placement_status": "proposed"}


@app.get("/predictions/proposed", response_model=list[PredictionRow])
def proposed_predictions(
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[PredictionRow]:
    """Picks the bot has proposed and the user hasn't acted on yet."""
    return [PredictionRow(**r) for r in db.get_proposed_predictions()]


@app.get("/predictions/pending", response_model=list[PredictionRow])
def pending_predictions(
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[PredictionRow]:
    """Confirmed bets awaiting match outcome (the user is on the hook for these)."""
    return [PredictionRow(**r) for r in db.get_confirmed_pending()]


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


@app.post("/stats/ab-test")
@limiter.limit("5/minute")
def ab_test(
    request: Request,
    body: dict,
    _: str = Depends(require_auth),
) -> dict:
    """
    Compare two rule variants on resolved historical predictions.

    Body:
      variant_a: {"name": str, ... knobs ...}
      variant_b: {"name": str, ... knobs ...}
      days:      lookback window (default 90)
      only_placed: only count bets the user actually played (default false)

    Knobs (each variant):
      market_shrink_soft, market_shrink_hard, market_shrink_max,
      overconfidence_cap, overconfidence_penalty,
      huge_edge_threshold, huge_edge_penalty
    """
    from betbot.ab_test import RuleVariant, compare_variants
    a = RuleVariant(**(body.get("variant_a") or {"name": "A"}))
    b = RuleVariant(**(body.get("variant_b") or {"name": "B"}))
    return compare_variants(
        a, b,
        days=int(body.get("days", 90)),
        only_placed=bool(body.get("only_placed", False)),
    )


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
@limiter.limit("10/minute")  # protects the Odds API quota
def recommend_manual(
    request: Request,
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
            odds_quota_remaining=odds_client.quota_remaining,
            odds_quota_exhausted=odds_client.quota_exhausted,
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
        odds_quota_remaining=odds_client.quota_remaining,
        odds_quota_exhausted=odds_client.quota_exhausted,
    )


@app.post("/recommend/agent-local", response_model=LocalAgentResponse)
@limiter.limit("10/minute")  # protects Odds API + Tavily quotas
def recommend_agent_local(
    request: Request,
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
            n_news_calls=0, n_weather_calls=0,
            tavily_available=bool(os.getenv("TAVILY_API_KEY")),
            odds_quota_remaining=odds_client.quota_remaining,
            odds_quota_exhausted=odds_client.quota_exhausted,
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
        trigger="dashboard",
        filters=filters.model_dump(),
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
        odds_quota_remaining=odds_client.quota_remaining,
        odds_quota_exhausted=odds_client.quota_exhausted,
    )


@app.post("/agent/recommend", response_model=AgentResponse)
@limiter.limit("3/minute")  # Anthropic API costs $$ — never let a client spam this
async def agent_recommend(
    request: Request,
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


# ---------------------------------------------------------------------------
# Bankroll — real capital tracking (Phase 9)
# ---------------------------------------------------------------------------

@app.get("/arbitrage")
@limiter.limit("10/minute")
def arbitrage_scan(
    request: Request,
    sport_key: str | None = Query(default=None,
                                  description="Filter to one sport, else scan all"),
    today_only: bool = Query(default=True),
    _: str = Depends(require_auth),
) -> dict:
    """
    Detect cross-bookmaker arbitrage opportunities — guaranteed profit if you
    can place all legs at the listed odds. Real arbs are RARE in liquid markets
    (~1 in 200-1000 events) and usually < 1% profit.
    """
    from betbot.arbitrage import scan_arbs, arb_to_dict
    from betbot.main import _filter_upcoming_today
    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    if sport_key:
        all_events = {sport_key: odds_client.get_events_with_odds(sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    for sk, ev in all_events.items():
        kept = _filter_upcoming_today(ev, s.min_before_kickoff) if today_only else ev
        if kept:
            events_by_sport[sk] = kept

    arbs = scan_arbs(events_by_sport, market_key="h2h")
    return {
        "n_opportunities": len(arbs),
        "arbs": [arb_to_dict(a) for a in arbs],
        "notes": [
            "Real arbs are usually < 1% profit and require fast execution.",
            "Bookmakers may void your bet or limit your account if you arb regularly.",
            "Always verify odds on the bookmaker site before placing — odds move.",
        ],
    }


@app.get("/health/sources")
def health_sources(_: str = Depends(require_auth)) -> dict:
    """
    Probe every external data source and report its status.

    Each source carries:
      - status: "ok" | "ko" | "not_configured"  (distinguishes credential issue from real outage)
      - criticality: "critical" | "important" | "optional"
        * critical  : without it the system stops working (odds_api)
        * important : degrades quality but doesn't break anything (football_data, club_elo)
        * optional  : only powers a specific feature (anthropic = AI agent, tavily = news)
      - latency_ms: how long the probe took
      - reason: error message when status != "ok"
    """
    import os
    import time
    from datetime import datetime, timezone
    from betbot.data_sources import club_elo, understat
    s = load_settings()

    # Per-probe timeout : a single slow source (Understat is notoriously
    # flaky) must not block the whole health endpoint. Each probe runs in
    # its own thread with a 4s wall-clock cap. Prevents the dashboard
    # "Erreur : timed out" we used to see when Understat was being slow.
    import concurrent.futures as _cf
    PROBE_TIMEOUT_SEC = 4

    def _probe(name: str, criticality: str, configured: bool, live_check) -> dict:
        if not configured:
            return {
                "name": name, "criticality": criticality,
                "status": "not_configured", "ok": False,
                "latency_ms": 0,
                "reason": f"clé/credential absent — voir .env",
            }
        t0 = time.monotonic()
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(live_check)
                ok = bool(future.result(timeout=PROBE_TIMEOUT_SEC))
            return {
                "name": name, "criticality": criticality,
                "status": "ok" if ok else "ko",
                "ok": ok,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": "" if ok else "probe returned falsy result",
            }
        except _cf.TimeoutError:
            return {
                "name": name, "criticality": criticality,
                "status": "ko", "ok": False,
                "latency_ms": PROBE_TIMEOUT_SEC * 1000,
                "reason": f"probe timed out after {PROBE_TIMEOUT_SEC}s",
            }
        except Exception as exc:
            return {
                "name": name, "criticality": criticality,
                "status": "ko", "ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": str(exc)[:200],
            }

    sources = [
        _probe("odds_api",      "critical",  bool(s.odds_api_key),
               lambda: bool(s.odds_api_key)),
        _probe("football_data", "important", bool(s.football_data_api_key),
               lambda: bool(s.football_data_api_key)),
        _probe("club_elo",      "important", True,
               lambda: len(club_elo.get_all_elo_ratings()) > 100),
        _probe("understat",     "optional",  True,
               understat.is_available),
        _probe("api_football",  "optional",  bool(os.getenv("API_FOOTBALL_KEY")),
               lambda: bool(os.getenv("API_FOOTBALL_KEY"))),
        _probe("tavily",        "optional",  bool(os.getenv("TAVILY_API_KEY")),
               lambda: bool(os.getenv("TAVILY_API_KEY"))),
        _probe("anthropic",     "optional",  bool(s.anthropic_api_key),
               lambda: bool(s.anthropic_api_key)),
    ]
    return {
        "sources": sources,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# ML probability calibrator — Isotonic regression on resolved bets
# ---------------------------------------------------------------------------

@app.get("/ml/calibrator/status")
def ml_calibrator_status(_: str = Depends(require_auth)) -> dict:
    """
    Show whether a calibrator is fitted and ready, plus how many resolved bets
    are available for the next training run.
    """
    from betbot.ml import calibrator_status, _collect_training_data, MIN_SAMPLES_TO_TRUST
    status = calibrator_status()
    samples = _collect_training_data()
    n_resolved = len(samples)
    return {
        **status,
        "n_resolved_bets": n_resolved,
        "min_samples_to_trust": MIN_SAMPLES_TO_TRUST,
        "ready_to_train": n_resolved >= MIN_SAMPLES_TO_TRUST,
    }


@app.post("/ml/calibrator/train")
@limiter.limit("5/minute")
def ml_calibrator_train(
    request: Request,
    _: str = Depends(require_auth),
) -> dict:
    """Force a retrain of the calibrator on whatever resolved bets are available."""
    from betbot.ml import train_calibrator, reset_cache
    result = train_calibrator()
    reset_cache()  # so the next scan picks up the new model
    return result


# ---------------------------------------------------------------------------
# Tennis ELO ratings
# ---------------------------------------------------------------------------

@app.get("/tennis/status")
def tennis_status(_: str = Depends(require_auth)) -> dict:
    """Show the state of the tennis ELO ratings (n_players, top, last match date)."""
    from betbot.tennis_model import status
    return status()


@app.post("/tennis/refresh")
@limiter.limit("2/minute")
def tennis_refresh(
    request: Request,
    tour: str = Query(default="atp", description="atp | wta | both"),
    _: str = Depends(require_auth),
) -> dict:
    """Force a refresh of tennis ELO from the latest Sackmann CSV data."""
    from betbot.tennis_bootstrap import refresh_ratings
    return refresh_ratings(tour=tour)


@app.get("/tennis/predict")
def tennis_predict_endpoint(
    home: str = Query(...),
    away: str = Query(...),
    surface: str = Query(default="Hard"),
    _: str = Depends(require_auth),
) -> dict:
    """Quick preview of an ELO-based tennis prediction for any player pair."""
    from betbot.tennis_model import predict
    p = predict(home, away, surface)
    if p is None:
        return {"error": "player not found in ratings"}
    return p.__dict__


# ---------------------------------------------------------------------------
# Basketball pace / off-rating model
# ---------------------------------------------------------------------------

@app.get("/basketball/status")
def basketball_status(_: str = Depends(require_auth)) -> dict:
    """Show the state of the NBA team stats (n_teams, top by net rating)."""
    from betbot.basketball_model import status
    return status()


@app.post("/basketball/refresh")
@limiter.limit("2/minute")
def basketball_refresh(
    request: Request,
    season: int | None = Query(default=None),
    _: str = Depends(require_auth),
) -> dict:
    """Force a refresh of NBA team stats from basketball-reference."""
    from betbot.basketball_bootstrap import refresh_stats
    return refresh_stats(season_year=season)


@app.get("/basketball/predict")
def basketball_predict_endpoint(
    home: str = Query(...),
    away: str = Query(...),
    league: str = Query(default="nba"),
    _: str = Depends(require_auth),
) -> dict:
    """Quick preview of a pace-based basketball prediction."""
    from betbot.basketball_model import predict
    p = predict(home, away, league=league)
    if p is None:
        return {"error": "team not found"}
    return p.__dict__


@app.get("/agent/runs")
def list_agent_runs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trigger: str | None = Query(default=None,
                                description="Filter: 'api', 'dashboard', or 'scheduled'"),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[dict]:
    """Audit trail: every AI-agent invocation with reasoning + cost + picks."""
    return db.list_agent_runs(limit=limit, offset=offset, trigger=trigger)


@app.get("/agent/runs/{run_id}")
def get_agent_run(
    run_id: int,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Full detail of a single agent run, including the reasoning trace."""
    row = db.get_agent_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent run #{run_id} not found")
    return row


@app.post("/promotions")
def add_promotion(body: dict, _: str = Depends(require_auth)) -> dict:
    """
    Record a bookmaker promo (freebet, deposit match, refund, etc.).
    Body:
      kind            : "freebet" | "deposit_match" | "refund" | "boost" | "cashback"
      nominal_value   : face value (e.g. 50 for a 50€ freebet)
      cash_equivalent : realistic EV after rollover/odds caps (often < nominal)
      bookmaker_key   : optional account identifier
      rollover_x      : optional wagering requirement
      note            : optional free text
    """
    from betbot.promotions import record_promotion
    promo_id = record_promotion(
        kind=body.get("kind", "").strip(),
        nominal_value=float(body["nominal_value"]),
        cash_equivalent=float(body["cash_equivalent"]),
        bookmaker_key=body.get("bookmaker_key"),
        rollover_x=body.get("rollover_x"),
        expires_at=body.get("expires_at"),
        note=body.get("note"),
    )
    return {"id": promo_id}


@app.get("/promotions")
def list_promos(only_unused: bool = Query(default=False),
                _: str = Depends(require_auth)) -> list[dict]:
    from betbot.promotions import list_promotions
    return list_promotions(only_unused=only_unused)


@app.get("/promotions/summary")
def promo_summary(_: str = Depends(require_auth)) -> dict:
    from betbot.promotions import promotions_summary
    return promotions_summary()


@app.post("/promotions/{promo_id}/used")
def mark_promo_used(promo_id: int, _: str = Depends(require_auth)) -> dict:
    from betbot.promotions import mark_promotion_used
    if not mark_promotion_used(promo_id):
        raise HTTPException(status_code=404, detail=f"Promotion {promo_id} not found")
    return {"id": promo_id, "used": True}


@app.post("/cashouts")
def add_cashout(body: dict, _: str = Depends(require_auth)) -> dict:
    """
    Record a cash-out on a pending prediction. Body:
      prediction_id          : the bet being cashed out
      cash_out_amount        : actual amount received
      bookmaker_offered_price: the cash-out price the bookie showed (optional)
      note                   : optional
    """
    from betbot.promotions import record_cashout
    try:
        cashout_id = record_cashout(
            prediction_id=int(body["prediction_id"]),
            cash_out_amount=float(body["cash_out_amount"]),
            bookmaker_offered_price=body.get("bookmaker_offered_price"),
            note=body.get("note"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": cashout_id}


@app.get("/cashouts")
def list_cashouts_endpoint(
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(require_auth),
) -> list[dict]:
    from betbot.promotions import list_cashouts
    return list_cashouts(limit=limit)


@app.get("/bankroll/state", response_model=BankrollSnapshot)
def bankroll_state(_: str = Depends(require_auth)) -> BankrollSnapshot:
    """Current snapshot: balance, committed (immobilized on pending bets),
    available (free cash), total deposits/withdrawals, P&L."""
    from betbot.bankroll import get_state
    s = get_state()
    return BankrollSnapshot(**s.__dict__)


@app.post("/bankroll/deposit", response_model=BankrollSnapshot)
@limiter.limit("10/minute")
def bankroll_deposit(
    request: Request,
    body: BankrollMutation,
    _: str = Depends(require_auth),
) -> BankrollSnapshot:
    """Add capital. The amount is always positive."""
    from betbot.bankroll import deposit
    s = deposit(body.amount, note=body.note)
    return BankrollSnapshot(**s.__dict__)


@app.post("/bankroll/withdraw", response_model=BankrollSnapshot)
@limiter.limit("10/minute")
def bankroll_withdraw(
    request: Request,
    body: BankrollMutation,
    _: str = Depends(require_auth),
) -> BankrollSnapshot:
    """Remove capital. Refuses if the available balance is insufficient."""
    from betbot.bankroll import InsufficientFundsError, withdraw
    try:
        s = withdraw(body.amount, note=body.note)
    except InsufficientFundsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BankrollSnapshot(**s.__dict__)


@app.get("/bankroll/history", response_model=list[BankrollLedgerRow])
def bankroll_history(
    limit: int = Query(default=200, ge=1, le=1000),
    bookmaker_key: str | None = Query(default=None,
                                      description="Filter to one bookmaker account"),
    _: str = Depends(require_auth),
) -> list[BankrollLedgerRow]:
    """Recent ledger entries, newest first. Optionally filtered by bookmaker."""
    from betbot.bankroll import get_history
    return [BankrollLedgerRow(**row)
            for row in get_history(limit=limit, bookmaker_key=bookmaker_key)]


@app.get("/bankroll/guards")
def bankroll_guards(_: str = Depends(require_auth)) -> dict:
    """Current guard state: stop-loss status, exposure, daily caps used/remaining."""
    from betbot.guards import get_guard_status
    return get_guard_status()


@app.get("/bankroll/bookmakers")
def bankroll_bookmakers(
    active_only: bool = Query(default=False),
    _: str = Depends(require_auth),
) -> list[dict]:
    """List all bookmaker accounts with their per-account balance."""
    from betbot.bankroll import list_bookmakers
    return list_bookmakers(active_only=active_only)


@app.post("/bankroll/bookmakers")
def bankroll_add_bookmaker(
    body: dict,
    _: str = Depends(require_auth),
) -> dict:
    """Register a new bookmaker account. Body: {key, display_name, note?}."""
    from betbot.bankroll import add_bookmaker
    return add_bookmaker(
        key=body.get("key", "").strip(),
        display_name=body.get("display_name", "").strip(),
        note=body.get("note"),
    )


@app.get("/bankroll/evolution")
def bankroll_evolution(
    days: int = Query(default=30, ge=1, le=365),
    bookmaker_key: str | None = Query(default=None,
                                      description="Filter to one bookmaker account"),
    _: str = Depends(require_auth),
) -> list[dict]:
    """Time series for the dashboard chart, optionally filtered by bookmaker."""
    from betbot.bankroll import get_evolution
    return get_evolution(days=days, bookmaker_key=bookmaker_key)
