"""
NBA team statistics from basketball-reference.com.

Why basketball-reference and not nba_api ?
  - bb-ref serves clean static HTML with all season stats on one page,
    parseable directly by pandas.read_html(). One HTTP request → 30 teams.
  - The official NBA stats API (stats.nba.com) is geo-restricted, applies
    aggressive rate-limits (~1 req/2s with auth headers), and changes
    its endpoints without notice.
  - bb-ref's robots.txt allows agentic reads with reasonable delays.

Fields we extract per team (`Per 100 Possessions` table) :
    - pace         : possessions per 48 minutes (NBA average ~99)
    - off_rating   : points scored per 100 possessions (~115)
    - def_rating   : points allowed per 100 possessions (~115)

These three are enough to project both the **moneyline** (h2h winner) and
the **total points** market with a simple normal-distribution model.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

logger = logging.getLogger("betbot.bbref")

USER_AGENT = "BetBot/1.0 (research, polite scraping)"


@dataclass(frozen=True)
class NBATeamStats:
    name: str          # e.g. "Boston Celtics"
    pace: float        # possessions per 48 min
    off_rating: float  # points per 100 possessions
    def_rating: float  # points allowed per 100 possessions
    games: int         # games played this season


def _current_season_year() -> int:
    """The bb-ref URL uses the year of the season's END.

    Example: the 2025-26 season is at /leagues/NBA_2026.html
    The season starts in October N-1 and ends in June N, so we choose:
        - October..December → year+1
        - January..September → current year
    """
    now = datetime.now(timezone.utc)
    return now.year + 1 if now.month >= 10 else now.year


def fetch_team_stats(season_year: int | None = None, timeout: int = 20) -> list[NBATeamStats]:
    """Pull NBA team season stats from basketball-reference.com.

    Returns one NBATeamStats per team (30 expected). Returns [] on failure
    so callers can fall back gracefully.
    """
    import pandas as pd

    season_year = season_year or _current_season_year()
    url = f"https://www.basketball-reference.com/leagues/NBA_{season_year}.html"

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("bb-ref fetch failed: %s", exc)
        return []
    if resp.status_code != 200:
        logger.warning("bb-ref %s -> HTTP %d", url, resp.status_code)
        return []

    # bb-ref hides several tables in HTML comments to bypass legacy parsers
    # — we strip the comment markers so pandas can see them all.
    text = resp.text.replace("<!--", "").replace("-->", "")

    try:
        tables = pd.read_html(io.StringIO(text), flavor="lxml")
    except (ValueError, ImportError) as exc:
        # ValueError: no tables in HTML  •  ImportError: html5lib fallback missing
        logger.warning("bb-ref no tables parsed: %s", exc)
        return []

    # Find the "Per 100 Poss Stats" table — its first column is "Rk", and the
    # data columns include "ORtg" and "DRtg" plus per-100 box stats.
    target = None
    for t in tables:
        cols = [str(c).lower() for c in t.columns.get_level_values(-1)]
        if "ortg" in cols and "drtg" in cols and ("team" in cols or any("team" in c for c in cols)):
            target = t
            break
    if target is None:
        logger.warning("bb-ref: 'Per 100 Poss' table not found in %d tables", len(tables))
        return []

    # Pace lives in a separate "Misc Stats" table; find it
    pace_table = None
    for t in tables:
        cols = [str(c).lower() for c in t.columns.get_level_values(-1)]
        if "pace" in cols and ("team" in cols or any("team" in c for c in cols)):
            pace_table = t
            break

    # Flatten MultiIndex columns if present
    if hasattr(target.columns, "get_level_values"):
        target.columns = [c[-1] if isinstance(c, tuple) else c for c in target.columns]
    if pace_table is not None and hasattr(pace_table.columns, "get_level_values"):
        pace_table.columns = [c[-1] if isinstance(c, tuple) else c for c in pace_table.columns]

    # Index pace by team name
    pace_by_team: dict[str, float] = {}
    games_by_team: dict[str, int] = {}
    if pace_table is not None and "Team" in pace_table.columns:
        for _, row in pace_table.iterrows():
            try:
                name = str(row["Team"]).strip().rstrip("*")
                if not name or name.lower() in ("league average", "team"):
                    continue
                pace_by_team[name] = float(row["Pace"])
                if "G" in pace_table.columns:
                    games_by_team[name] = int(float(row["G"]))
            except (KeyError, ValueError, TypeError):
                continue

    teams: list[NBATeamStats] = []
    if "Team" not in target.columns:
        logger.warning("bb-ref: target table has no 'Team' column")
        return []
    for _, row in target.iterrows():
        try:
            name = str(row["Team"]).strip().rstrip("*")
            if not name or name.lower() in ("league average", "team"):
                continue
            teams.append(NBATeamStats(
                name=name,
                pace=pace_by_team.get(name, 99.0),  # league average fallback
                off_rating=float(row["ORtg"]),
                def_rating=float(row["DRtg"]),
                games=games_by_team.get(name, 0),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    logger.info("bb-ref scraped : %d teams (season %d)", len(teams), season_year)
    return teams
