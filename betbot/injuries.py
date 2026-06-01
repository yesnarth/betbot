"""Injury → attack-strength penalty for the blended model.

Turns API-Football injuries (players ruled out) into a multiplicative ATTACK
factor (≤ 1.0) on the affected team's expected goals (λ). This captures info the
market prices but the statistical model ignores.

SAFE BY DESIGN:
  - OFF by default (FETCH_INJURIES=0) → returns 1.0 → model unchanged.
  - Fully graceful: no API key / quota hit / error / unmapped league /
    off-season → 1.0 (never raises).
  - Quota-aware (API-Football free tier = 100 req/day): in-process caches for
    team-id and computed factors (TTL 12h) + a per-run lookup budget.

Modelling choice (documented simplification): we only model the dominant,
reliably-signed effect — absences REDUCE the team's attack. We do NOT try to
infer "defender out → opponent scores more" (needs reliable position data).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("betbot.injuries")

# sport_key → API-Football league id (v3). Only mapped leagues are queried;
# unmapped (consensus-only) leagues simply get factor 1.0.
_LEAGUE_ID: dict[str, int] = {
    "soccer_epl": 39,
    "soccer_spain_la_liga": 140,
    "soccer_germany_bundesliga": 78,
    "soccer_italy_serie_a": 135,
    "soccer_france_ligue1": 61,
    "soccer_uefa_champs_league": 2,
    "soccer_efl_champ": 40,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
}

PENALTY_PER_ABSENCE = 0.035    # −3.5 % attack per confirmed absence
MAX_ABSENCES_COUNTED = 5       # cap the penalty → at most −17.5 %
MIN_FACTOR = 0.80              # never cut a team's attack by more than 20 %
CACHE_TTL_SEC = 12 * 3600      # injuries don't change minute-to-minute
_DEFAULT_BUDGET = 40           # max API team-lookups per scan run (quota guard)

_team_id_cache: dict[tuple, int | None] = {}      # (name, league_id) → team_id
_factor_cache: dict[tuple, tuple[float, float]] = {}  # (name, sport_key) → (factor, ts)
_budget_used = 0


def _enabled() -> bool:
    """Read each call so .env toggles take effect without a restart."""
    return os.getenv("FETCH_INJURIES", "0") == "1"


def _current_season(now: datetime) -> int:
    """API-Football season = the starting year (Aug-Jul cycle)."""
    return now.year if now.month >= 7 else now.year - 1


def reset_run_budget() -> None:
    """Reset the per-scan API-lookup budget. Called at the start of a scan so a
    long-running worker keeps fetching on subsequent scans (cache covers repeats)."""
    global _budget_used
    _budget_used = 0


def get_injury_factor(team_name: str, sport_key: str | None) -> float:
    """Multiplicative attack factor in [MIN_FACTOR, 1.0]. Returns 1.0 (no-op) when
    disabled, unavailable, unmapped, or on any error. Never raises."""
    global _budget_used
    if not _enabled() or not team_name or not sport_key:
        return 1.0
    league_id = _LEAGUE_ID.get(sport_key)
    if not league_id:
        return 1.0

    now = time.time()
    cache_key = (team_name, sport_key)
    cached = _factor_cache.get(cache_key)
    if cached is not None and (now - cached[1]) < CACHE_TTL_SEC:
        return cached[0]

    budget = _DEFAULT_BUDGET
    try:
        budget = max(0, int(os.getenv("INJURY_LOOKUP_BUDGET", str(_DEFAULT_BUDGET))))
    except ValueError:
        pass
    if _budget_used >= budget:
        # Protect the daily quota — cache neutral so we don't retry this run.
        _factor_cache[cache_key] = (1.0, now)
        return 1.0

    try:
        from betbot.data_sources import api_football

        season = _current_season(datetime.now(timezone.utc))
        tid_key = (team_name, league_id)
        if tid_key in _team_id_cache:
            tid = _team_id_cache[tid_key]
        else:
            _budget_used += 1
            tid = api_football.search_team_id(team_name, league_id)
            _team_id_cache[tid_key] = tid
        if not tid:
            _factor_cache[cache_key] = (1.0, now)
            return 1.0

        _budget_used += 1
        injuries = api_football.get_team_injuries(tid, league_id, season)
        n_out = sum(
            1 for i in injuries
            if (i.get("type") or "").lower().startswith("missing")  # "Missing Fixture"
        )
        factor = max(MIN_FACTOR, 1.0 - PENALTY_PER_ABSENCE * min(n_out, MAX_ABSENCES_COUNTED))
        _factor_cache[cache_key] = (factor, now)
        if n_out:
            logger.info("injuries %s: %d absent(s) → attaque ×%.3f", team_name, n_out, factor)
        return factor
    except api_football.APIFootballNotConfigured:
        return 1.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("injury factor for %s failed: %s", team_name, exc)
        return 1.0


def injury_factor_from_counts(n_out: int) -> float:
    """Pure heuristic (testable without the API): absences → attack factor."""
    return max(MIN_FACTOR, 1.0 - PENALTY_PER_ABSENCE * min(max(n_out, 0), MAX_ABSENCES_COUNTED))
