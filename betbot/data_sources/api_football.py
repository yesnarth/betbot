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
