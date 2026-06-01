"""Recommendation endpoints — manual (deterministic), local agent (rules), AI agent (Claude)."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request

from betbot.api import OddsAPIClient
from betbot.config import load_settings
from betbot.db import Database
from betbot_api.auth import require_auth
from betbot_api.deps import limiter
from betbot_api.schemas import (
    AgentFilters,
    AgentResponse,
    LocalAgentFilters,
    LocalAgentResponse,
    ManualScanFilters,
    ManualScanResponse,
    TargetParlayFilters,
    TargetParlayResponse,
)

router = APIRouter(tags=["recommend"])


@router.post("/recommend/manual", response_model=ManualScanResponse)
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
    from betbot.shared import filter_upcoming_today, load_team_stats_from_db

    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    edge = filters.min_edge if filters.min_edge is not None else s.min_value_edge
    prob = filters.min_prob if filters.min_prob is not None else s.min_model_prob
    odds = filters.min_odds if filters.min_odds is not None else s.min_book_odds

    if filters.sport_key:
        all_events = {filters.sport_key: odds_client.get_events_with_odds(filters.sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    for sk, ev in all_events.items():
        kept = filter_upcoming_today(ev, s.min_before_kickoff) if filters.today_only else ev
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

    db = Database(s.database_url)
    prebuilt = load_team_stats_from_db(db, events_by_sport.keys())
    raw = detect_value_bets(
        events_by_sport=events_by_sport,
        match_history_by_sport={},
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        min_value_edge=edge,
        min_model_prob=prob,
        min_book_odds=odds,
        min_edge_vs_novig=s.min_edge_vs_novig,
        prebuilt_stats_by_sport=prebuilt,
    )
    ranked = rank_value_bets(raw)[: s.top_bets]
    parlays = build_parlays(ranked, n_legs=filters.n_legs, top_n=filters.n_combos)

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
            "reliability": b.reliability,
        }

    picks_out = [bet_to_dict(b) for b in ranked]
    parlays_out = [
        {
            "n_legs": len(p.bets),
            "combined_odds": p.combined_odds,
            "combined_prob": p.combined_prob,
            "combined_ev_pct": p.combined_ev,
            "correlated": p.correlated,
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


@router.post("/recommend/parlay-target", response_model=TargetParlayResponse)
@limiter.limit("3/minute")  # heavy fan-out scan (all leagues) — tighter than other scans
def recommend_parlay_target(
    request: Request,
    filters: TargetParlayFilters,
    _: str = Depends(require_auth),
) -> TargetParlayResponse:
    """
    ×1000 "lottery" parlay mode. Scans broadly (set SCAN_ALL_SOCCER=1 to cover
    every in-season football league), gathers a large pool of candidate legs with
    RELAXED filters (NO no-vig gate, EV ≥ 0 by default), then greedily stacks
    event-disjoint legs until combined odds reach `target_odds`.

    HIGH VARIANCE by design — a ×1000 combo wins ~0.1% of the time. The safe
    singles path (worker + /recommend/manual) keeps all its protections.
    """
    from betbot.analysis import build_target_parlays, detect_value_bets
    from betbot.shared import filter_upcoming_today, load_team_stats_from_db

    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    if filters.sport_key:
        all_events = {filters.sport_key: odds_client.get_events_with_odds(filters.sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    for sk, ev in all_events.items():
        kept = filter_upcoming_today(ev, s.min_before_kickoff) if filters.today_only else ev
        if kept:
            events_by_sport[sk] = kept

    n_events = sum(len(v) for v in events_by_sport.values())
    if n_events == 0:
        return TargetParlayResponse(
            parlays=[], n_candidates=0, best_achievable_odds=0.0,
            target_odds=filters.target_odds, n_events_scanned=0,
            odds_quota_remaining=odds_client.quota_remaining,
            odds_quota_exhausted=odds_client.quota_exhausted,
        )

    db = Database(s.database_url)
    prebuilt = load_team_stats_from_db(db, events_by_sport.keys())
    # RELAXED pool : no no-vig gate (min_edge_vs_novig=0), EV floor = filters.min_edge.
    pool = detect_value_bets(
        events_by_sport=events_by_sport,
        match_history_by_sport={},
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        min_value_edge=filters.min_edge,
        min_model_prob=filters.min_prob,
        min_book_odds=filters.min_leg_odds,
        min_edge_vs_novig=0.0,
        require_positive_stake=False,   # lottery pool : keep every edge≥0 leg
        prebuilt_stats_by_sport=prebuilt,
    )

    parlays = build_target_parlays(
        pool, target_odds=filters.target_odds, max_legs=filters.max_legs,
        top_n=filters.n_combos, min_leg_odds=filters.min_leg_odds,
    )

    # Best achievable odds (single greedy chain) — informative when the target
    # can't be reached with today's pool.
    best = 1.0
    seen: set[str] = set()
    for b in sorted(pool, key=lambda x: (x.value_edge * (x.reliability or 1.0), x.model_prob),
                    reverse=True):
        if b.event_id in seen:
            continue
        seen.add(b.event_id)
        best *= b.best_odds
        if len(seen) >= filters.max_legs:
            break

    def bet_to_dict(b):
        return {
            "event_id": b.event_id, "sport_key": b.sport_key, "league": b.league_label,
            "home_team": b.home_team, "away_team": b.away_team, "market": b.market,
            "selection_code": b.selection_code, "selection_label": b.selection_label,
            "model_prob": b.model_prob, "best_odds": b.best_odds, "best_book": b.best_book,
            "value_edge": b.value_edge, "kelly_stake": b.kelly_stake,
            "model_type": b.model_type, "reliability": b.reliability,
        }

    parlays_out = [
        {
            "n_legs": len(p.bets),
            "combined_odds": p.combined_odds,
            "combined_prob": p.combined_prob,
            "combined_ev_pct": p.combined_ev,
            "correlated": p.correlated,
            "legs": [bet_to_dict(b) for b in p.bets],
        }
        for p in parlays
    ]

    return TargetParlayResponse(
        parlays=parlays_out,
        n_candidates=len(pool),
        best_achievable_odds=round(best, 2),
        target_odds=filters.target_odds,
        n_events_scanned=n_events,
        odds_quota_remaining=odds_client.quota_remaining,
        odds_quota_exhausted=odds_client.quota_exhausted,
    )


@router.post("/recommend/agent-local", response_model=LocalAgentResponse)
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
    from betbot.shared import filter_upcoming_today, load_team_stats_from_db

    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    edge = filters.min_edge if filters.min_edge is not None else s.min_value_edge
    prob = filters.min_prob if filters.min_prob is not None else s.min_model_prob
    odds = filters.min_odds if filters.min_odds is not None else s.min_book_odds

    if filters.sport_key:
        all_events = {filters.sport_key: odds_client.get_events_with_odds(filters.sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    commence_by_id: dict[str, str] = {}
    for sk, ev in all_events.items():
        kept = filter_upcoming_today(ev, s.min_before_kickoff) if filters.today_only else ev
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

    from betbot.analysis import detect_value_bets, rank_value_bets
    db = Database(s.database_url)
    prebuilt = load_team_stats_from_db(db, events_by_sport.keys())
    raw_bets = detect_value_bets(
        events_by_sport=events_by_sport,
        match_history_by_sport={},
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        min_value_edge=edge,
        min_model_prob=prob,
        min_book_odds=odds,
        min_edge_vs_novig=s.min_edge_vs_novig,
        prebuilt_stats_by_sport=prebuilt,
    )
    ranked = rank_value_bets(raw_bets)[: s.top_bets]

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
            "reliability": b.reliability,
            "commence_time": commence_by_id.get(b.event_id, ""),
        }

    raw_picks = [bet_to_dict(b) for b in ranked]

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
            "correlated": p.correlated,
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


@router.post("/agent/recommend", response_model=AgentResponse)
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
