"""Rest / fixture-congestion → attack-strength penalty for the blended model.

A team on short rest — or playing its 3rd match in ~12 days (incl. midweek Europe)
— underperforms slightly. This turns recent-fixture spacing into a multiplicative
ATTACK factor (≤ 1.0) on expected goals (λ), the SAME mechanism as injuries, and
captures a real effect the market prices but the pure Poisson/xG model ignores.

SAFE BY DESIGN:
  - OFF by default (FETCH_FATIGUE=0) → returns 1.0 → model unchanged.
  - Fully graceful: no API key / quota / error / unmapped league / no fixtures /
    bad date → 1.0 (never raises).
  - Quota-aware: team-id + factor caches (6h TTL) + a per-scan lookup budget.

Deliberately CONSERVATIVE (max −10 % attack): fatigue is a modest, noisy effect;
over-penalising would add variance, not signal.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("betbot.fatigue")

# sport_key → API-Football league id (for team-id resolution only).
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

CONGESTION_WINDOW_DAYS = 12
FATIGUE_MIN_FACTOR = 0.90          # never cut attack by more than 10 % for fatigue
CACHE_TTL_SEC = 6 * 3600           # fixtures change midweek → shorter TTL than injuries
_DEFAULT_BUDGET = 40

_team_id_cache: dict[tuple, int | None] = {}
_factor_cache: dict[tuple, tuple[float, float]] = {}
_budget_used = 0


def _enabled() -> bool:
    return os.getenv("FETCH_FATIGUE", "0") == "1"


def _current_season(now: datetime) -> int:
    return now.year if now.month >= 7 else now.year - 1


def reset_run_budget() -> None:
    """Reset the per-scan API-lookup budget (called at scan start)."""
    global _budget_used
    _budget_used = 0


def _parse_iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def freshness_factor_from_dates(recent_dates: list, upcoming: datetime) -> float:
    """PURE, testable: recent finished-match datetimes + the upcoming kickoff →
    attack factor in [FATIGUE_MIN_FACTOR, 1.0]. Well-rested → 1.0.

    Penalise short rest since the last match, plus congestion (matches in the last
    CONGESTION_WINDOW_DAYS beyond the normal 2). Bounded and conservative.
    """
    try:
        past = sorted(d for d in recent_dates if d and d < upcoming)
    except TypeError:  # naive/aware mismatch → don't guess
        return 1.0
    if not past:
        return 1.0

    rest_days = (upcoming - past[-1]).days
    if rest_days <= 2:
        rest_pen = 0.06
    elif rest_days == 3:
        rest_pen = 0.03
    elif rest_days == 4:
        rest_pen = 0.015
    else:
        rest_pen = 0.0

    window_start = upcoming - timedelta(days=CONGESTION_WINDOW_DAYS)
    n_recent = sum(1 for d in past if d >= window_start)
    cong_pen = min(0.02 * max(0, n_recent - 2), 0.06)

    return round(max(FATIGUE_MIN_FACTOR, 1.0 - rest_pen - cong_pen), 3)


def get_fatigue_factor(team_name: str, sport_key: str | None, commence_iso) -> float:
    """Multiplicative attack factor in [FATIGUE_MIN_FACTOR, 1.0]. Returns 1.0
    (no-op) when disabled, unavailable, unmapped, or on any error. Never raises."""
    global _budget_used
    if not _enabled() or not team_name or not sport_key or not commence_iso:
        return 1.0
    league_id = _LEAGUE_ID.get(sport_key)
    if not league_id:
        return 1.0
    upcoming = _parse_iso(commence_iso)
    if upcoming is None:
        return 1.0

    now = time.time()
    cache_key = (team_name, sport_key)
    cached = _factor_cache.get(cache_key)
    if cached is not None and (now - cached[1]) < CACHE_TTL_SEC:
        return cached[0]

    try:
        budget = max(0, int(os.getenv("FATIGUE_LOOKUP_BUDGET", str(_DEFAULT_BUDGET))))
    except ValueError:
        budget = _DEFAULT_BUDGET
    if _budget_used >= budget:
        _factor_cache[cache_key] = (1.0, now)
        return 1.0

    try:
        from betbot.data_sources import api_football

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
        dates = api_football.get_recent_fixture_dates(tid, last=6)
        factor = freshness_factor_from_dates(dates, upcoming)
        _factor_cache[cache_key] = (factor, now)
        if factor < 1.0:
            logger.info("fatigue %s: repos/congestion → attaque ×%.3f", team_name, factor)
        return factor
    except api_football.APIFootballNotConfigured:
        return 1.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("fatigue factor for %s failed: %s", team_name, exc)
        return 1.0
