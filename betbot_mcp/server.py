"""
BetBot MCP server.

Exposes the betbot package as Model Context Protocol tools so any MCP-aware
client (Claude Desktop, Claude Agent SDK, Cursor, etc.) can drive the bot:
fetch odds, predict matches, find value bets, build parlays, track ROI.

Run as a stdio MCP server:
    python -m betbot_mcp.server
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from betbot.analysis import (
    Parlay,
    ValueBet,
    build_parlays,
    detect_value_bets,
    rank_value_bets,
)
from betbot.api import SPORT_KEYS
from betbot.football_api import LEAGUE_MAP
from betbot.models import poisson_match_probs
from betbot.resolver import resolve_pending
from betbot_mcp.context import db, football_client, odds_client, settings

logger = logging.getLogger("betbot_mcp")

mcp = FastMCP("betbot")


# ---------------------------------------------------------------------------
# Helpers (kept small and serializer-friendly)
# ---------------------------------------------------------------------------

def _bet_to_dict(b: ValueBet) -> dict:
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


def _parlay_to_dict(p: Parlay) -> dict:
    return {
        "n_legs": len(p.bets),
        "combined_odds": p.combined_odds,
        "combined_prob": p.combined_prob,
        "combined_ev_pct": p.combined_ev,
        "legs": [_bet_to_dict(b) for b in p.bets],
    }


# Use the shared canonical implementations.
from betbot.shared import filter_upcoming_today, load_team_stats_from_db  # noqa: E402


# ---------------------------------------------------------------------------
# Tools — discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def list_sports() -> list[dict]:
    """
    Return the football leagues currently tracked, mapping The Odds API sport
    keys to football-data.org competition codes.
    """
    return [
        {"sport_key": k, "competition_code": LEAGUE_MAP.get(k), "tracked": True}
        for k in SPORT_KEYS
    ]


@mcp.tool()
def health_check() -> dict:
    """Quick liveness probe — verifies DB and that the Odds API key responds."""
    try:
        n = sum(
            len(db().get_all_team_stats_for_league(s)) for s in SPORT_KEYS
        )
        return {
            "ok": True,
            "teams_in_db": n,
            "scan_hours": settings().scan_hours,
            "min_value_edge": settings().min_value_edge,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — events / odds
# ---------------------------------------------------------------------------

@mcp.tool()
def fetch_events(
    sport_key: str | None = None,
    today_only: bool = True,
    min_before_kickoff: int | None = None,
) -> dict:
    """
    Fetch upcoming events with H2H odds.

    Args:
        sport_key: A specific sport (e.g. "soccer_epl"). If omitted, fetches all.
        today_only: If true, keep only events kicking off today (UTC).
        min_before_kickoff: Reject events starting in less than N minutes
                            (defaults to settings.MIN_BEFORE_KICKOFF).
    """
    s = settings()
    cutoff = min_before_kickoff if min_before_kickoff is not None else s.min_before_kickoff
    client = odds_client()
    if sport_key:
        all_events = {sport_key: client.get_events_with_odds(sport_key)}
    else:
        all_events = client.fetch_all_sports()

    by_sport: dict[str, list[dict]] = {}
    for sk, events in all_events.items():
        kept = filter_upcoming_today(events, cutoff) if today_only else events
        if kept:
            by_sport[sk] = [
                {
                    "event_id": e.get("id"),
                    "sport_key": sk,
                    "home_team": e.get("home_team"),
                    "away_team": e.get("away_team"),
                    "commence_time": e.get("commence_time"),
                    "n_bookmakers": len(e.get("bookmakers", [])),
                }
                for e in kept
            ]
    total = sum(len(v) for v in by_sport.values())
    return {"total": total, "by_sport": by_sport, "today_only": today_only}


# ---------------------------------------------------------------------------
# Tools — team stats
# ---------------------------------------------------------------------------

@mcp.tool()
def get_team_stats(team_name: str, sport_key: str) -> dict | None:
    """
    Look up Poisson team-strength stats (attack/defense home & away).
    Returns None if the team is unknown for that league.
    """
    return db().get_team_stats(team_name, sport_key)


@mcp.tool()
def list_teams(sport_key: str) -> list[str]:
    """Return all team names known in the database for a given league."""
    return [r["team_name"] for r in db().get_all_team_stats_for_league(sport_key)]


@mcp.tool()
def get_league_averages(sport_key: str) -> dict | None:
    """
    Return the league's average goals scored at home and away — needed by the
    Dixon-Coles model for accurate λ scaling.
    """
    avgs = db().get_league_averages(sport_key)
    if not avgs:
        return None
    return {"sport_key": sport_key, "home_avg": avgs[0], "away_avg": avgs[1]}


# ---------------------------------------------------------------------------
# Tools — prediction
# ---------------------------------------------------------------------------

@mcp.tool()
def predict_match(home_team: str, away_team: str, sport_key: str) -> dict:
    """
    Run the Dixon-Coles Poisson model for a fixture and return probabilities
    for home win, draw, away win, and over 2.5 goals.

    Team names are matched fuzzily — "Inter Milan" resolves to "FC Internazionale Milano",
    "Arsenal" to "Arsenal FC", etc. Returns {"ok": false, "reason": ...} if either
    team has no stats in DB even after fuzzy matching.
    """
    from betbot.analysis import _fuzzy_lookup
    from betbot.models import DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG, TeamStats

    rows = db().get_all_team_stats_for_league(sport_key)
    cache: dict[str, TeamStats] = {
        r["team_name"]: TeamStats(
            name=r["team_name"],
            attack_home=r["attack_home"],
            defense_home=r["defense_home"],
            attack_away=r["attack_away"],
            defense_away=r["defense_away"],
            matches_analyzed=r["matches_analyzed"],
        )
        for r in rows
    }
    home_obj, home_match = _fuzzy_lookup(home_team, cache)
    away_obj, away_match = _fuzzy_lookup(away_team, cache)

    if not home_obj or not away_obj:
        missing = [t for t, r in [(home_team, home_obj), (away_team, away_obj)] if not r]
        return {
            "ok": False,
            "reason": "team_stats_missing",
            "missing": missing,
            "hint": "Run --update-stats or check the league supports football-data.org",
        }

    avgs = db().get_league_averages(sport_key) or (DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG)
    league_home_avg, league_away_avg = avgs

    lambda_home = home_obj.attack_home * away_obj.defense_away * league_home_avg
    lambda_away = away_obj.attack_away * home_obj.defense_home * league_away_avg
    lambda_home = max(0.2, min(lambda_home, 5.0))
    lambda_away = max(0.2, min(lambda_away, 5.0))
    probs = poisson_match_probs(lambda_home, lambda_away)

    return {
        "ok": True,
        "model": "dixon_coles_poisson",
        "matched_home": home_match,
        "matched_away": away_match,
        "lambda_home": probs.lambda_home,
        "lambda_away": probs.lambda_away,
        "home_win": probs.home_win,
        "draw": probs.draw,
        "away_win": probs.away_win,
        "over_25": probs.over_25,
    }


# ---------------------------------------------------------------------------
# Tools — value detection / parlays
# ---------------------------------------------------------------------------

@mcp.tool()
def find_value_bets(
    sport_key: str | None = None,
    today_only: bool = True,
    min_value_edge: float | None = None,
    min_model_prob: float | None = None,
    min_book_odds: float | None = None,
    top_n: int = 10,
) -> list[dict]:
    """
    End-to-end pipeline: fetch events → run Poisson → find positive-edge bets.

    Args:
        sport_key:        restrict to a single league (default: all)
        today_only:       only matches kicking off today
        min_value_edge:   override settings.MIN_VALUE_EDGE
        min_model_prob:   override settings.MIN_MODEL_PROB
        min_book_odds:    override settings.MIN_BOOK_ODDS
        top_n:            cap the number of bets returned
    """
    s = settings()
    edge = s.min_value_edge if min_value_edge is None else min_value_edge
    prob = s.min_model_prob if min_model_prob is None else min_model_prob
    odds = s.min_book_odds if min_book_odds is None else min_book_odds

    # Reuse the discovery tool to build events_by_sport with full bookmaker data
    if sport_key:
        events_raw = {sport_key: odds_client().get_events_with_odds(sport_key)}
    else:
        events_raw = odds_client().fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    for sk, ev in events_raw.items():
        kept = filter_upcoming_today(ev, s.min_before_kickoff) if today_only else ev
        if kept:
            events_by_sport[sk] = kept

    # Load Poisson stats from DB in the new {teams, home_avg, away_avg} shape
    prebuilt = load_team_stats_from_db(db(), events_by_sport.keys())

    bets = detect_value_bets(
        events_by_sport=events_by_sport,
        match_history_by_sport={},
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        min_value_edge=edge,
        min_model_prob=prob,
        min_book_odds=odds,
        prebuilt_stats_by_sport=prebuilt,
    )
    ranked = rank_value_bets(bets)[:top_n]
    return [_bet_to_dict(b) for b in ranked]


@mcp.tool()
def build_parlay(
    bets: list[dict],
    n_legs: int = 3,
    top_n: int = 3,
    min_combined_odds: float = 2.0,
) -> list[dict]:
    """
    Combine independent value bets into n-leg parlays ranked by EV.

    Args:
        bets: a list of bets exactly as returned by find_value_bets()
        n_legs: number of legs per parlay (2 or 3 typically)
        top_n: number of parlays to return
        min_combined_odds: minimum combined odds (skip parlays below)
    """
    # Reconstruct ValueBet instances so build_parlays can dedupe by event_id
    rebuilt = [
        ValueBet(
            event_id=b["event_id"],
            sport_key=b["sport_key"],
            home_team=b["home_team"],
            away_team=b["away_team"],
            league_label=b.get("league", ""),
            market=b["market"],
            selection_code=b["selection_code"],
            selection_label=b["selection_label"],
            model_prob=b["model_prob"],
            best_odds=b["best_odds"],
            best_book=b["best_book"],
            value_edge=b["value_edge"],
            kelly_stake=b.get("kelly_stake", 0.0),
            lambda_home=None,
            lambda_away=None,
            model_type=b.get("model_type", "poisson"),
        )
        for b in bets
    ]
    parlays = build_parlays(rebuilt, n_legs=n_legs, top_n=top_n, min_combined_odds=min_combined_odds)
    return [_parlay_to_dict(p) for p in parlays]


# ---------------------------------------------------------------------------
# Tools — tracking / ROI
# ---------------------------------------------------------------------------

@mcp.tool()
def save_predictions(picks: list[dict]) -> dict:
    """
    Persist a list of picks into the predictions table as **proposed** —
    meaning the bot is recommending them but the user hasn't acted yet.
    Bankroll is NOT debited at this stage. The user must explicitly confirm
    each pick (via `confirm_prediction_placed`) once they've placed the bet
    at their bookmaker site, or skip it (via `skip_prediction`).

    Idempotent on (event_id, market, selection) — duplicates return False.
    """
    saved = 0
    duplicate = 0
    for p in picks:
        ok = db().save_prediction(
            event_id=p["event_id"],
            sport_key=p["sport_key"],
            home_team=p["home_team"],
            away_team=p["away_team"],
            market=p["market"],
            selection=p["selection_code"],
            model_prob=p["model_prob"],
            best_odds=p["best_odds"],
            best_book=p["best_book"],
            value_edge=p["value_edge"],
            kelly_stake=p.get("kelly_stake", 0.0),
            lambda_home=p.get("lambda_home"),
            lambda_away=p.get("lambda_away"),
            model_type=p.get("model_type", "poisson"),
            reliability=p.get("reliability"),
        )
        if ok:
            saved += 1
        else:
            duplicate += 1
    return {"saved": saved, "already_existed": duplicate}


@mcp.tool()
def get_pending_predictions() -> list[dict]:
    """Confirmed predictions awaiting match outcome — what the user is on
    the hook for. Use `get_proposed_predictions` for the bot's pending
    recommendations the user hasn't acted on yet."""
    return db().get_pending_predictions()


@mcp.tool()
def get_proposed_predictions() -> list[dict]:
    """Picks the bot has recommended but the user hasn't confirmed or
    skipped yet. These are the picks that should appear in the
    'Picks à valider' validation queue on the dashboard."""
    return db().get_proposed_predictions()


@mcp.tool()
def confirm_prediction_placed(
    prediction_id: int,
    bookmaker: str | None = None,
    unconfirm: bool = False,
) -> dict:
    """
    Confirm the user actually placed this proposed pick at their bookmaker.

    THIS is when the bankroll is debited (atomically with the placement-status
    flip). Pre-flight responsible-betting guards (stop-loss, daily cap,
    exposure) apply here, NOT at scan time. The bot still cannot place
    real bets — bookmakers don't expose APIs for that — so this tool just
    records what the user did manually.

    Args:
        prediction_id: the row to confirm
        bookmaker: optional name of the bookmaker the user placed at
                   (stored for split-by-book ROI / CLV analytics)
        unconfirm: set True to revert a previous confirmation (re-credits
                   the stake and flips back to 'proposed')

    Returns: dict with ok status, prediction_id, and the new placement_status.
    """
    try:
        ok = db().confirm_prediction_placed(prediction_id, bookmaker, unconfirm=unconfirm)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}
    if not ok:
        return {"ok": False, "reason": "not_found"}
    return {
        "ok": True,
        "prediction_id": prediction_id,
        "placement_status": "proposed" if unconfirm else "confirmed",
        "bookmaker": bookmaker,
    }


@mcp.tool()
def skip_prediction(prediction_id: int, reason: str = "user_skipped") -> dict:
    """
    Skip a proposed pick — user passes on the recommendation. No bankroll
    movement. The row is kept in DB for analytics (would-have ROI on
    skipped picks). Refused for picks already confirmed.
    """
    try:
        ok = db().skip_prediction(prediction_id, reason=reason)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}
    if not ok:
        return {"ok": False, "reason": "not_found"}
    return {"ok": True, "prediction_id": prediction_id,
            "placement_status": "skipped"}


@mcp.tool()
def unskip_prediction(prediction_id: int) -> dict:
    """
    Revert a skipped prediction back to 'proposed' — accident recovery.
    Use when the user clicked Skip by mistake. Refused for confirmed picks.
    """
    try:
        ok = db().unskip_prediction(prediction_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}
    if not ok:
        return {"ok": False, "reason": "not_found"}
    return {"ok": True, "prediction_id": prediction_id,
            "placement_status": "proposed"}


@mcp.tool()
def resolve_results(days_from: int = 3) -> dict:
    """
    Match completed games against pending predictions and update win/loss.
    days_from: how many days back to look for finished matches (max 3).
    """
    return resolve_pending(db(), odds_client(), days_from=days_from)


@mcp.tool()
def get_roi_stats(days: int = 30) -> dict:
    """Aggregate ROI stats (n_bets, hit_rate, ROI %, avg_edge) over the last N days."""
    return db().get_roi_stats(days=days)


# ---------------------------------------------------------------------------
# Tools — external data sources
# ---------------------------------------------------------------------------

@mcp.tool()
def get_elo_rating(team_name: str) -> dict:
    """
    Look up the current Club Elo rating of a team.
    Elo is the most stable single metric of club strength — a 100-point gap
    is roughly +12% win probability.
    """
    from betbot.data_sources.club_elo import get_team_elo
    elo = get_team_elo(team_name)
    if elo is None:
        return {"ok": False, "team": team_name, "reason": "not_in_clubelo"}
    return {"ok": True, "team": team_name, "elo": round(elo, 1)}


@mcp.tool()
def compare_elo(home_team: str, away_team: str) -> dict:
    """
    Compute the Elo-derived probability of the home team not losing
    (home_win + draw), useful as a sanity check vs the Poisson prediction.
    """
    from betbot.data_sources.club_elo import elo_win_probability, get_team_elo
    elo_home = get_team_elo(home_team)
    elo_away = get_team_elo(away_team)
    if elo_home is None or elo_away is None:
        return {"ok": False, "missing": [
            t for t, e in [(home_team, elo_home), (away_team, elo_away)] if e is None
        ]}
    p = elo_win_probability(elo_home, elo_away)
    return {
        "ok": True,
        "elo_home": round(elo_home, 1),
        "elo_away": round(elo_away, 1),
        "elo_diff": round(elo_home - elo_away, 1),
        "home_no_loss_prob": round(p, 4),
    }


@mcp.tool()
def get_xg_stats(team_name: str, sport_key: str) -> dict | None:
    """
    Pull season-to-date xG / xGA / xPts for a team via Understat.
    xG (expected goals) outperforms raw goals as a predictive feature.
    """
    from betbot.data_sources.understat import get_team_xg
    t = get_team_xg(team_name, sport_key)
    return dict(t) if t else None


@mcp.tool()
def get_match_weather(home_team: str, kickoff_utc_iso: str) -> dict | None:
    """
    Forecast the weather at the home stadium for an upcoming match.
    Heavy rain (>5mm) or strong wind (>35 km/h) typically shaves ~10-15% off
    expected goals. Returns None if the stadium isn't in our coordinates table.
    """
    from betbot.data_sources.club_elo import _normalize
    from betbot.data_sources.weather import get_match_weather as _w
    res = _w(_normalize(home_team), kickoff_utc_iso)
    return dict(res) if res else None


@mcp.tool()
def get_team_injuries(team_name: str, sport_key: str) -> dict:
    """
    List current injuries / suspensions for a team via API-Football.
    Requires API_FOOTBALL_KEY in .env. Returns {"ok": false, "reason": ...}
    if not configured (the agent should then proceed without this signal).
    """
    try:
        from betbot.data_sources.api_football import (
            APIFootballNotConfigured,
            get_team_injuries as _inj,
            search_team_id,
        )
    except ImportError as exc:
        return {"ok": False, "reason": f"import_error:{exc}"}

    try:
        team_id = search_team_id(team_name)
        if team_id is None:
            return {"ok": False, "reason": "team_not_found"}
        # API-Football needs league_id + season; we pass placeholders the user
        # can override — for now we just return what we'd ask if integrated.
        return {
            "ok": True,
            "team_id": team_id,
            "note": "Pass league_id + season to /injuries — see api_football.get_team_injuries",
        }
    except APIFootballNotConfigured as exc:
        return {"ok": False, "reason": "api_football_not_configured", "hint": str(exc)}


@mcp.tool()
def enrich_database() -> dict:
    """
    Refresh ELO + xG enrichment columns on every team in DB.
    Run after a regular --update-stats. Idempotent.
    """
    from betbot.enrichment import enrich_team_stats
    return enrich_team_stats(db())


@mcp.tool()
def run_backtest_tool(sport_key: str, n_holdout: int = 100) -> dict:
    """
    Replay the last N matches of a league and report Brier score, log-loss
    and a calibration table. Use this to verify model quality before tweaking
    weights — well-calibrated already? don't over-optimize.
    """
    from betbot.backtest import run_backtest
    s = settings()
    result = run_backtest(sport_key, s.football_data_api_key, n_holdout)
    return {
        "sport_key": result.sport_key,
        "n_matches": result.n_matches,
        "brier_score": result.brier_score,
        "log_loss": result.log_loss,
        "calibration": result.calibration,
        "notes": result.notes,
    }


@mcp.tool()
def get_clv_stats(days: int = 30) -> dict:
    """Aggregate CLV (Closing Line Value) over the last N days. The metric pros track."""
    from betbot.clv import aggregate_clv
    return aggregate_clv(days=days)


@mcp.tool()
def search_team_news(
    team_name: str,
    days_back: int = 3,
    max_results: int = 5,
) -> dict:
    """
    Web search for last-minute news about a team via Tavily — injuries,
    suspensions, coach sackings, scandals. Use BEFORE locking in a bet on
    a major favorite to make sure no breaking news invalidates the model.

    Returns {"ok": false} if TAVILY_API_KEY isn't configured (proceed without).
    """
    try:
        from betbot.data_sources.news import (
            TavilyNotConfigured,
            search_team_news as _news,
        )
        hits = _news(team_name, days_back=days_back, max_results=max_results)
        return {"ok": True, "team": team_name, "hits": hits}
    except TavilyNotConfigured as exc:
        return {"ok": False, "reason": "tavily_not_configured", "hint": str(exc)}
    except Exception as exc:
        return {"ok": False, "reason": "search_failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — Multi-sport infrastructure
# ---------------------------------------------------------------------------

@mcp.tool()
def get_active_sports() -> dict:
    """
    List the sports currently in-season per The Odds API's free /v4/sports
    endpoint, intersected with our wishlist (football leagues + tennis
    Grand Slams + NBA / EuroLeague). Out-of-season sports are auto-skipped
    by the scan to save quota.
    """
    from betbot.api import _enabled_sport_keys
    client = odds_client()
    active = client.get_active_sports()
    wishlist = _enabled_sport_keys()
    return {
        "wishlist": wishlist,
        "active": [k for k in wishlist if k in active],
        "skipped_out_of_season": [k for k in wishlist if k not in active],
        "quota_remaining": client.quota_remaining,
    }


# ---------------------------------------------------------------------------
# Tools — Tennis ELO model
# ---------------------------------------------------------------------------

@mcp.tool()
def predict_tennis_match(
    home_player: str,
    away_player: str,
    surface: str = "Hard",
) -> dict:
    """
    Predict an ATP tennis match using surface-aware ELO ratings (Sackmann data).

    Args:
        home_player: e.g. "Carlos Alcaraz"
        away_player: e.g. "Jannik Sinner"
        surface:     "Hard" | "Clay" | "Grass"

    Returns blended ratings (50% surface-specific + 50% overall) and the
    logistic win probability for each player. Returns {"ok": false} if
    either player is absent from the rating file.
    """
    from betbot.tennis_model import predict
    p = predict(home_player, away_player, surface)
    if p is None:
        return {"ok": False, "reason": "player_not_found",
                "hint": "Run refresh_tennis_ratings to ensure recent players are loaded."}
    return {"ok": True, **p.__dict__}


@mcp.tool()
def get_tennis_status() -> dict:
    """
    Diagnostic on the tennis ELO file: how many players, top 5 by ELO,
    most recent match analysed.
    """
    from betbot.tennis_model import status
    return status()


@mcp.tool()
def refresh_tennis_ratings(tour: str = "atp", years: list[int] | None = None) -> dict:
    """
    Recompute ELO ratings from Jeff Sackmann's tennis_atp / tennis_wta CSV repos.

    Args:
        tour:  "atp" | "wta" | "both"
        years: explicit year list (default: last 3 years)
    """
    from betbot.tennis_bootstrap import refresh_ratings
    return refresh_ratings(years=years, tour=tour)


# ---------------------------------------------------------------------------
# Tools — Basketball model (NBA / EuroLeague)
# ---------------------------------------------------------------------------

@mcp.tool()
def predict_basketball_match(
    home_team: str,
    away_team: str,
    league: str = "nba",
) -> dict:
    """
    Predict an NBA / EuroLeague match using pace + offensive / defensive rating.

    Returns expected points for each side, total, margin, and the win prob
    (CDF normale, σ=11). Home advantage is 2.7 pts (NBA) or 1.8 pts (EuroLeague).
    """
    from betbot.basketball_model import predict
    p = predict(home_team, away_team, league=league)
    if p is None:
        return {"ok": False, "reason": "team_not_found",
                "hint": "Run refresh_basketball_stats first."}
    return {"ok": True, **p.__dict__}


@mcp.tool()
def get_basketball_status() -> dict:
    """How many teams are in our basketball stats file, top 5 by net rating."""
    from betbot.basketball_model import status
    return status()


@mcp.tool()
def refresh_basketball_stats(season_year: int | None = None) -> dict:
    """
    Re-scrape basketball-reference.com for the current NBA season pace + ORtg/DRtg
    per team. Pass `season_year` (e.g. 2026 = 2025-26 season) to override.
    """
    from betbot.basketball_bootstrap import refresh_stats
    return refresh_stats(season_year=season_year)


# ---------------------------------------------------------------------------
# Tools — ML probability calibrator
# ---------------------------------------------------------------------------

@mcp.tool()
def get_calibrator_status() -> dict:
    """
    Inspect the Isotonic-regression calibrator: is it fitted, when, on how
    many samples, and how many resolved bets are now available for retraining.
    """
    from betbot.ml import calibrator_status, _collect_training_data, MIN_SAMPLES_TO_TRUST
    s = calibrator_status()
    n_resolved = len(_collect_training_data())
    return {
        **s,
        "n_resolved_bets": n_resolved,
        "min_samples_to_trust": MIN_SAMPLES_TO_TRUST,
        "ready_to_train": n_resolved >= MIN_SAMPLES_TO_TRUST,
    }


@mcp.tool()
def train_ml_calibrator() -> dict:
    """
    Force a retrain of the Isotonic-regression probability calibrator using
    every resolved bet currently in the predictions table. No-op if fewer
    than 50 resolved bets exist (variance too high for reliable fit).
    """
    from betbot.ml import train_calibrator, reset_cache
    result = train_calibrator()
    reset_cache()
    return result


# ---------------------------------------------------------------------------
# Tools — Bankroll management
# ---------------------------------------------------------------------------

@mcp.tool()
def get_bankroll_state() -> dict:
    """Current bankroll snapshot: balance, committed, available, P&L, totals."""
    from betbot.bankroll import get_state
    state = get_state()
    return {
        "balance": state.balance,
        "committed": state.committed,
        "available": state.available,
        "total_deposits": state.total_deposits,
        "total_withdrawals": state.total_withdrawals,
        "total_won": state.total_won,
        "total_lost_stakes": state.total_lost_stakes,
        "pnl": state.pnl,
        "n_entries": state.n_entries,
    }


@mcp.tool()
def bankroll_deposit(amount: float, note: str | None = None) -> dict:
    """Add a manual deposit to the bankroll. Amount must be > 0."""
    from betbot.bankroll import deposit
    if amount <= 0:
        return {"ok": False, "reason": "amount_must_be_positive"}
    deposit(amount, note=note)
    return {"ok": True, "deposited": amount}


@mcp.tool()
def bankroll_withdraw(amount: float, note: str | None = None) -> dict:
    """Record a manual withdrawal from the bankroll. Amount must be > 0."""
    from betbot.bankroll import withdraw
    if amount <= 0:
        return {"ok": False, "reason": "amount_must_be_positive"}
    withdraw(amount, note=note)
    return {"ok": True, "withdrawn": amount}


@mcp.tool()
def get_bankroll_history(limit: int = 50) -> list[dict]:
    """Recent ledger entries (deposits, withdrawals, bet placements, settlements)."""
    from betbot.bankroll import get_history
    return get_history(limit=limit)


# ---------------------------------------------------------------------------
# Tools — Local deterministic agent (rule chain over picks)
# ---------------------------------------------------------------------------

@mcp.tool()
def run_local_agent(
    sport_key: str | None = None,
    today_only: bool = True,
    min_value_edge: float | None = None,
    min_final_edge: float = 0.02,
    fetch_news: bool = True,
    fetch_weather: bool = True,
) -> dict:
    """
    Run the deterministic local agent over a fresh scan: it pulls value bets,
    enriches with Tavily news + Open-Meteo weather, applies the explicit rule
    chain (huge-edge tiers, injury detection, ELO contradictions), then
    returns accepted / flagged / rejected picks with full rationale.

    Cheaper and more transparent than calling Claude — every rule is explicit.
    """
    from betbot.local_agent import evaluate_picks
    from betbot.analysis import detect_value_bets, rank_value_bets

    s = settings()
    edge = s.min_value_edge if min_value_edge is None else min_value_edge

    # Fetch events
    if sport_key:
        all_events = {sport_key: odds_client().get_events_with_odds(sport_key)}
    else:
        all_events = odds_client().fetch_all_sports()

    events_by_sport = {}
    for sk, ev in all_events.items():
        kept = filter_upcoming_today(ev, s.min_before_kickoff) if today_only else ev
        if kept:
            events_by_sport[sk] = kept

    if not events_by_sport:
        return {"n_picks_in": 0, "n_accepted": 0, "n_rejected": 0,
                "picks": [], "rejected": [], "reason": "no_events"}

    prebuilt = load_team_stats_from_db(db(), events_by_sport.keys())
    raw = detect_value_bets(
        events_by_sport=events_by_sport, match_history_by_sport={},
        bankroll=s.bankroll, kelly_fraction=s.kelly_fraction,
        min_value_edge=edge, min_model_prob=s.min_model_prob,
        min_book_odds=s.min_book_odds,
        prebuilt_stats_by_sport=prebuilt,
    )
    raw = rank_value_bets(raw)[: s.top_bets]

    return evaluate_picks(
        picks=[_bet_to_dict(b) for b in raw],
        fetch_news=fetch_news,
        fetch_weather=fetch_weather,
        min_final_edge=min_final_edge,
        bankroll=s.bankroll,
        kelly_fraction=s.kelly_fraction,
        persist_run=False,  # MCP invocation is stateless
        trigger="mcp",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # FastMCP defaults to stdio transport — perfect for Claude Desktop / Agent SDK
    mcp.run()


if __name__ == "__main__":
    main()
