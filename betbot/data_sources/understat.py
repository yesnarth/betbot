"""
Understat — https://understat.com

Scrapes per-team xG (expected goals) and xGA (expected goals against) for the
top-5 European leagues. xG is a vastly better predictor than raw goals because
it strips out finishing variance.

Data is embedded in the page as a JSON literal assigned to JavaScript vars
(`var teamsData = JSON.parse('...')`). We extract and parse it.

Cached for 24 hours since end-of-season tables don't move that fast.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import TypedDict

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.data_sources.understat")

LEAGUE_URL = "https://understat.com/league/{league}/{year}"

# Map our internal sport keys → Understat's URL slug
SPORT_TO_UNDERSTAT: dict[str, str] = {
    "soccer_epl":                "EPL",
    "soccer_spain_la_liga":      "La_Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a":      "Serie_A",
    "soccer_france_ligue1":      "Ligue_1",
    "soccer_uefa_champs_league": None,   # Understat doesn't cover CL stand-alone
}

_CACHE: dict[tuple[str, int], dict] = {}


class TeamXG(TypedDict):
    team_id: str
    title: str
    matches: int
    goals: int
    xg: float
    goals_against: int
    xga: float
    npxg: float            # non-penalty xG
    npxga: float
    xpts: float            # expected points
    pts: int
    xg_per_match: float
    xga_per_match: float


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _fetch_html(league_slug: str, year: int) -> str:
    url = LEAGUE_URL.format(league=league_slug, year=year)
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BetBot/1.0)"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def _extract_teams_data(html: str) -> dict | None:
    """
    Try several patterns to extract the JSON-encoded teamsData blob.

    Understat has historically inlined `var teamsData = JSON.parse('…\\x…')`
    in the page HTML. As of late-2025 the public-facing HTML no longer
    contains this blob (likely moved to client-side fetch + bot detection).

    We keep this function defensive: every known pattern is tried, and we
    log a CLEAR warning when none matches so health checks can surface the
    breakage. The caller treats `None` as "xG temporarily unavailable" and
    the model falls back to Dixon-Coles + ELO.
    """
    patterns = [
        # Historical pattern (single-quoted JS string)
        r"var\s+teamsData\s*=\s*JSON\.parse\('([^']+)'\)",
        # Defensive: double-quoted variant in case Understat changes
        r"var\s+teamsData\s*=\s*JSON\.parse\(\"([^\"]+)\"\)",
        # Newer pattern: assigned without `var`
        r"teamsData\s*=\s*JSON\.parse\(['\"]([^'\"]+)['\"]\)",
    ]
    for pat in patterns:
        match = re.search(pat, html)
        if not match:
            continue
        raw = match.group(1)
        decoded = raw.encode("utf-8").decode("unicode_escape")
        try:
            return json.loads(decoded)
        except json.JSONDecodeError as exc:
            logger.warning("Understat: matched pattern but JSON parse failed: %s", exc)
            continue

    logger.warning(
        "Understat: no teamsData blob in HTML (length=%d). Source likely changed "
        "or returned a stripped page. xG features will be unavailable; model "
        "will degrade gracefully to Dixon-Coles + ELO.", len(html),
    )
    return None


def is_available() -> bool:
    """Quick liveness check — used by /health/sources. Tries a single fetch
    on the EPL page and confirms the parser still works."""
    try:
        html = _fetch_html("EPL", date.today().year if date.today().month >= 8 else date.today().year - 1)
        return _extract_teams_data(html) is not None
    except Exception:
        return False


def get_league_xg(sport_key: str, year: int | None = None) -> list[TeamXG]:
    """
    Return per-team aggregated xG / xGA / xPts for the requested season.
    year: ending year of the season (e.g. 2025 for 2024-2025). Defaults to the
    current ongoing season (Aug-Jul cycle).
    """
    league_slug = SPORT_TO_UNDERSTAT.get(sport_key)
    if not league_slug:
        return []

    if year is None:
        today = date.today()
        # Season starts in August → if before Aug, use previous year as start
        year = today.year if today.month >= 8 else today.year - 1

    cache_key = (sport_key, year)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    html = _fetch_html(league_slug, year)
    teams_raw = _extract_teams_data(html)
    if not teams_raw:
        return []

    out: list[TeamXG] = []
    for team_id, team in teams_raw.items():
        history = team.get("history", [])
        if not history:
            continue
        n = len(history)
        agg_xg = sum(float(h.get("xG", 0)) for h in history)
        agg_xga = sum(float(h.get("xGA", 0)) for h in history)
        agg_npxg = sum(float(h.get("npxG", 0)) for h in history)
        agg_npxga = sum(float(h.get("npxGA", 0)) for h in history)
        agg_xpts = sum(float(h.get("xpts", 0)) for h in history)
        agg_g = sum(int(h.get("scored", 0)) for h in history)
        agg_ga = sum(int(h.get("missed", 0)) for h in history)
        agg_pts = sum(int(h.get("pts", 0)) for h in history)
        out.append(TeamXG(
            team_id=team_id,
            title=team.get("title", ""),
            matches=n,
            goals=agg_g,
            xg=round(agg_xg, 2),
            goals_against=agg_ga,
            xga=round(agg_xga, 2),
            npxg=round(agg_npxg, 2),
            npxga=round(agg_npxga, 2),
            xpts=round(agg_xpts, 2),
            pts=agg_pts,
            xg_per_match=round(agg_xg / n, 3) if n else 0.0,
            xga_per_match=round(agg_xga / n, 3) if n else 0.0,
        ))

    _CACHE[cache_key] = out
    logger.info("Understat %s %d: %d équipes", league_slug, year, len(out))
    return out


def get_team_xg(team_name: str, sport_key: str, year: int | None = None) -> TeamXG | None:
    """Lookup a single team's xG stats. Fuzzy on title (case-insensitive contains)."""
    teams = get_league_xg(sport_key, year=year)
    needle = team_name.lower().strip()
    for t in teams:
        title = t["title"].lower()
        if title == needle or needle in title or title in needle:
            return t
    return None
