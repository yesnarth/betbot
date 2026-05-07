"""
CLI / function to (re)compute tennis ELO ratings from Sackmann history.

Usage:
    python -m betbot.tennis_bootstrap                  # ATP last 3 years (default)
    python -m betbot.tennis_bootstrap --years 2023 2024 2025
    python -m betbot.tennis_bootstrap --tour wta       # WTA instead of ATP
    python -m betbot.tennis_bootstrap --both           # ATP + WTA combined
"""
from __future__ import annotations

import argparse
import logging
import sys

from betbot.data_sources.tennis_sackmann import (
    fetch_atp_matches,
    fetch_wta_matches,
    default_year_window,
)
from betbot.tennis_model import save_ratings, train_from_matches, reset_cache

logger = logging.getLogger("betbot.tennis_bootstrap")


def refresh_ratings(years: list[int] | None = None, tour: str = "atp") -> dict:
    """
    Pull Sackmann data, train ELO, persist to disk. Returns a status dict.

    `tour` can be "atp", "wta", or "both" (combined ratings — note that ATP/WTA
    don't share a rating space in reality, so we only recommend "both" for
    debugging).
    """
    years = years or default_year_window()
    matches = []
    if tour in ("atp", "both"):
        matches.extend(fetch_atp_matches(years))
    if tour in ("wta", "both"):
        matches.extend(fetch_wta_matches(years))
    if not matches:
        logger.error("No matches downloaded — check network or year list")
        return {"trained": False, "n_matches": 0, "n_players": 0}
    matches.sort(key=lambda m: m.date)
    ratings = train_from_matches(matches)
    save_ratings(ratings)
    reset_cache()
    return {
        "trained": True,
        "n_matches": len(matches),
        "n_players": len(ratings),
        "years": years,
        "tour": tour,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )
    p = argparse.ArgumentParser(description="Refresh tennis ELO ratings from Sackmann CSVs")
    p.add_argument("--years", nargs="+", type=int, default=None,
                   help="Years to include (default: last 3)")
    p.add_argument("--tour", choices=["atp", "wta", "both"], default="atp",
                   help="Which tour to train on")
    args = p.parse_args(argv)
    result = refresh_ratings(years=args.years, tour=args.tour)
    print(result)
    return 0 if result.get("trained") else 1


if __name__ == "__main__":
    sys.exit(main())
