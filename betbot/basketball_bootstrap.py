"""
CLI to refresh NBA team stats from basketball-reference.com.

Usage:
    python -m betbot.basketball_bootstrap                # current season NBA
    python -m betbot.basketball_bootstrap --season 2025  # 2024-25 season
"""
from __future__ import annotations

import argparse
import logging
import sys

from betbot.data_sources.bbref_scraper import fetch_team_stats
from betbot.basketball_model import TeamSnapshot, save_teams, reset_cache

logger = logging.getLogger("betbot.basketball_bootstrap")


def refresh_stats(season_year: int | None = None) -> dict:
    raw = fetch_team_stats(season_year=season_year)
    if not raw:
        return {"trained": False, "n_teams": 0, "reason": "no data fetched"}
    teams = {
        t.name: TeamSnapshot(
            name=t.name,
            pace=t.pace,
            off_rating=t.off_rating,
            def_rating=t.def_rating,
            games=t.games,
            league="nba",
        )
        for t in raw
    }
    save_teams(teams)
    reset_cache()
    return {"trained": True, "n_teams": len(teams), "season_year": season_year}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )
    p = argparse.ArgumentParser(description="Refresh NBA team stats from basketball-reference")
    p.add_argument("--season", type=int, default=None,
                   help="Season end-year (e.g. 2026 for 2025-26 season)")
    args = p.parse_args(argv)
    result = refresh_stats(season_year=args.season)
    print(result)
    return 0 if result.get("trained") else 1


if __name__ == "__main__":
    sys.exit(main())
