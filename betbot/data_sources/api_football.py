"""
API-Football — https://www.api-football.com  (via RapidAPI)

Provides probable line-ups, injuries, suspensions, and head-to-head records.
Free tier: 100 requests / day — enough for 1-2 scans of upcoming fixtures.

Activated only when API_FOOTBALL_KEY is set in .env. Without it, the rest of
the bot still works and the agent can simply skip these tools.

Reference docs: https://www.api-football.com/documentation-v3
"""
from __future__ import annotations

import logging
import os
from typing import TypedDict

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.data_sources.api_football")

BASE_URL = "https://v3.football.api-sports.io"


class APIFootballNotConfigured(RuntimeError):
    """Raised when API_FOOTBALL_KEY is missing."""


def _headers() -> dict:
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise APIFootballNotConfigured(
            "API_FOOTBALL_KEY not set. Sign up at https://rapidapi.com/api-sports/api/api-football "
            "and put the key in .env (free tier: 100 req/day)."
        )
    return {"x-apisports-key": key}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _get(endpoint: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{BASE_URL}/{endpoint}",
        headers=_headers(),
        params=params or {},
        timeout=15,
    )
    if resp.status_code == 429:
        logger.warning("API-Football rate limit hit")
        return {}
    if resp.status_code != 200:
        logger.warning("API-Football HTTP %s on %s", resp.status_code, endpoint)
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Lineup(TypedDict):
    team_name: str
    formation: str
    starters: list[str]
    coach: str


def get_predicted_lineups(fixture_id: int) -> list[Lineup]:
    """
    Predicted starting XIs for an upcoming fixture (~24h before kick-off).
    fixture_id is the API-Football native ID — use search_fixture_id() to find it.
    """
    data = _get("fixtures/lineups", {"fixture": fixture_id})
    out: list[Lineup] = []
    for raw in data.get("response", []):
        starters = [
            p.get("player", {}).get("name", "")
            for p in raw.get("startXI", [])
        ]
        out.append(Lineup(
            team_name=raw.get("team", {}).get("name", ""),
            formation=raw.get("formation", ""),
            starters=[s for s in starters if s],
            coach=raw.get("coach", {}).get("name", ""),
        ))
    return out


class Injury(TypedDict):
    player: str
    team: str
    type: str       # "Missing Fixture" / "Questionable"
    reason: str


def get_team_injuries(team_id: int, league_id: int, season: int) -> list[Injury]:
    """List players ruled out / doubtful for an upcoming team match."""
    data = _get("injuries", {"team": team_id, "league": league_id, "season": season})
    out: list[Injury] = []
    for raw in data.get("response", []):
        out.append(Injury(
            player=raw.get("player", {}).get("name", ""),
            team=raw.get("team", {}).get("name", ""),
            type=raw.get("player", {}).get("type", ""),
            reason=raw.get("player", {}).get("reason", ""),
        ))
    return out


def get_h2h(team1_id: int, team2_id: int, last: int = 10) -> list[dict]:
    """
    Last N head-to-head matches between two teams (any competition).
    Useful for the agent to spot rivalries or stylistic mismatches.
    """
    data = _get("fixtures/headtohead", {"h2h": f"{team1_id}-{team2_id}", "last": last})
    return [
        {
            "date": f.get("fixture", {}).get("date"),
            "league": f.get("league", {}).get("name"),
            "home": f.get("teams", {}).get("home", {}).get("name"),
            "away": f.get("teams", {}).get("away", {}).get("name"),
            "home_goals": f.get("goals", {}).get("home"),
            "away_goals": f.get("goals", {}).get("away"),
        }
        for f in data.get("response", [])
    ]


def search_team_id(name: str, league_id: int | None = None) -> int | None:
    """Resolve a team name into its API-Football ID. Cached in-process."""
    params = {"search": name}
    if league_id:
        params["league"] = league_id
    data = _get("teams", params)
    rows = data.get("response", [])
    if not rows:
        return None
    return rows[0].get("team", {}).get("id")


# ---------------------------------------------------------------------------
# Expected goals (xG) — reliable, API-sourced. api-football exposes xG only
# PER FIXTURE (fixtures/statistics → {"type":"expected_goals","value":"1.23"}),
# so a team's xG form is aggregated over its recent finished fixtures.
# ---------------------------------------------------------------------------

# sport_key → api-football league id
SPORT_TO_LEAGUE_ID: dict[str, int] = {
    "soccer_epl": 39,
    "soccer_spain_la_liga": 140,
    "soccer_germany_bundesliga": 78,
    "soccer_italy_serie_a": 135,
    "soccer_france_ligue1": 61,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
    "soccer_england_championship": 40,
}

_XG_LEAGUE_CACHE: dict[tuple, list] = {}


def _current_season_year() -> int:
    from datetime import date
    t = date.today()
    return t.year if t.month >= 8 else t.year - 1


def get_fixture_xg(fixture_id: int) -> dict[int, float]:
    """{team_id: expected_goals} for a fixture. Empty when the league has no xG."""
    data = _get("fixtures/statistics", {"fixture": fixture_id})
    out: dict[int, float] = {}
    for raw in data.get("response", []):
        tid = raw.get("team", {}).get("id")
        if tid is None:
            continue
        for stat in raw.get("statistics", []):
            if stat.get("type") == "expected_goals":
                val = stat.get("value")
                try:
                    if val not in (None, ""):
                        out[tid] = float(val)
                except (ValueError, TypeError):
                    pass
    return out


def get_recent_team_xg(
    team_id: int, league_id: int, season: int, last: int = 6,
) -> dict | None:
    """Aggregate a team's xG for/against over its last `last` finished fixtures.
    Returns {matches, xg_per_match, xga_per_match} or None if no xG was found."""
    fx = _get("fixtures", {"team": team_id, "league": league_id,
                           "season": season, "last": last, "status": "FT"})
    xgf = xga = 0.0
    n = 0
    for f in fx.get("response", []):
        fid = f.get("fixture", {}).get("id")
        home_id = f.get("teams", {}).get("home", {}).get("id")
        away_id = f.get("teams", {}).get("away", {}).get("id")
        opp_id = away_id if home_id == team_id else home_id
        if fid is None or opp_id is None:
            continue
        xg = get_fixture_xg(fid)
        if team_id in xg and opp_id in xg:
            xgf += xg[team_id]
            xga += xg[opp_id]
            n += 1
    if n == 0:
        return None
    return {"matches": n, "xg_per_match": round(xgf / n, 3),
            "xga_per_match": round(xga / n, 3)}


def get_league_xg(sport_key: str, year: int | None = None, last: int = 6,
                  max_calls: int = 400) -> list[dict]:
    """Per-team recent-form xG for a whole league, shaped like the Understat
    source ({title, xg_per_match, xga_per_match, matches}). Drop-in replacement.

    Heavy (≈ teams × (1 + last) calls) — guarded by `max_calls` and a 24h
    in-process cache. Returns [] when the league isn't mapped or has no xG.
    """
    league_id = SPORT_TO_LEAGUE_ID.get(sport_key)
    if not league_id:
        return []
    season = year or _current_season_year()
    ck = (sport_key, season, last)
    if ck in _XG_LEAGUE_CACHE:
        return _XG_LEAGUE_CACHE[ck]

    teams = _get("teams", {"league": league_id, "season": season}).get("response", [])
    out: list[dict] = []
    calls = 1
    for row in teams:
        if calls >= max_calls:
            logger.warning("api-football xG: call budget (%d) reached for %s", max_calls, sport_key)
            break
        team = row.get("team", {})
        tid, name = team.get("id"), team.get("name", "")
        if tid is None:
            continue
        agg = get_recent_team_xg(tid, league_id, season, last=last)
        calls += 1 + last
        if agg:
            out.append({"title": name, "matches": agg["matches"],
                        "xg_per_match": agg["xg_per_match"],
                        "xga_per_match": agg["xga_per_match"]})
    if out:
        _XG_LEAGUE_CACHE[ck] = out
        logger.info("api-football xG %s saison %d : %d équipes (%d appels)",
                    sport_key, season, len(out), calls)
    return out


def is_available() -> bool:
    """Cheap liveness check for /health — confirms the key works via /status."""
    try:
        data = _get("status")
    except APIFootballNotConfigured:
        return False
    except Exception:
        return False
    return bool(data.get("response"))
