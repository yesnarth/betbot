"""
Shared helpers used across multiple top-level entrypoints (worker `main.py`,
FastAPI `betbot_api/main.py`, MCP server `betbot_mcp/server.py`).

Keeping them here avoids the awkward cross-imports we used to have
(API importing from MCP server, MCP server importing from worker CLI, etc.).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from betbot.db import Database
from betbot.models import TeamStats


def filter_upcoming_today(events: list[dict], min_before_kickoff: int = 60) -> list[dict]:
    """
    Keep only events that are:
      - scheduled for today (UTC)
      - starting at least `min_before_kickoff` minutes from now

    The kickoff buffer prevents placing bets on matches that are about to start
    or already in progress (some bookmakers freeze odds in the final minutes).
    """
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    cutoff = now_utc + timedelta(minutes=min_before_kickoff)

    result = []
    for event in events:
        commence = event.get("commence_time", "")
        if not commence.startswith(today_str):
            continue
        try:
            event_time = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if event_time >= cutoff:
                result.append(event)
        except (ValueError, TypeError):
            pass
    return result


def load_team_stats_from_db(db: Database, sport_keys: object) -> dict[str, dict]:
    """
    Load football team stats + H2H pair history from Postgres into the shape
    consumed by `detect_value_bets` and the model layer:

        {sport_key: {
            "teams":    {name: TeamStats},
            "home_avg": float,
            "away_avg": float,
            "h2h":      {(team_a, team_b): {team_a_wins, draws, team_b_wins,
                                             team_a_goals_avg, team_b_goals_avg}},
        }}

    H2H keys are alphabetical (team_a < team_b) — callers must orient when
    looking up.

    Falls back to default league averages (1.35 / 1.10) if `league_averages`
    row is missing for a given league. H2H section is `{}` when no pairs
    are stored yet (fresh install or before the first stats refresh).
    """
    from betbot.models import DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG

    result: dict[str, dict] = {}
    for sport_key in sport_keys:
        rows = db.get_all_team_stats_for_league(sport_key)
        if not rows:
            continue
        teams: dict[str, TeamStats] = {}
        for row in rows:
            teams[row["team_name"]] = TeamStats(
                name=row["team_name"],
                attack_home=row["attack_home"],
                defense_home=row["defense_home"],
                attack_away=row["attack_away"],
                defense_away=row["defense_away"],
                matches_analyzed=row["matches_analyzed"],
                elo_rating=row.get("elo_rating"),
                xg_for=row.get("xg_for"),
                xg_against=row.get("xg_against"),
            )
        avgs = db.get_league_averages(sport_key)
        home_avg, away_avg = avgs if avgs else (DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG)
        h2h = db.get_all_h2h_for_league(sport_key)
        result[sport_key] = {
            "teams": teams,
            "home_avg": home_avg,
            "away_avg": away_avg,
            "h2h": h2h,
        }
    return result
