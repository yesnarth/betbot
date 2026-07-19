"""xG source facade — one interface, best available backend.

Priority:
  1. api-football (reliable, API-sourced) when API_FOOTBALL_KEY is set — the
     recommended path once you subscribe.
  2. Understat (free scraper) as fallback. NOTE: Understat's public HTML no
     longer exposes the xG blob (broken since late-2025), so this returns [] —
     which is why the model has been running without xG. api-football fixes that.

All functions degrade gracefully to [] / None / identity, so nothing breaks
when neither backend is available (the blend simply runs without the xG signal).

Interface mirrors data_sources.understat so callers are a straight swap:
  get_league_xg(sport_key, year) -> [{title, xg_per_match, xga_per_match, ...}]
  get_team_xg(team_name, sport_key, year) -> {...} | None
  is_available() -> bool
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("betbot.data_sources.xg")


def _apifootball_configured() -> bool:
    return bool(os.getenv("API_FOOTBALL_KEY", "").strip())


def get_league_xg(sport_key: str, year: int | None = None) -> list[dict]:
    if _apifootball_configured():
        try:
            from betbot.data_sources import api_football as af
            res = af.get_league_xg(sport_key, year)
            if res:
                return res
            logger.info("api-football xG vide pour %s — repli Understat", sport_key)
        except Exception as exc:  # network / quota / shape — never crash enrichment
            logger.warning("api-football xG a échoué (%s) — repli Understat", exc)
    from betbot.data_sources import understat
    return understat.get_league_xg(sport_key, year=year)


def get_team_xg(team_name: str, sport_key: str, year: int | None = None) -> dict | None:
    needle = (team_name or "").lower().strip()
    for t in get_league_xg(sport_key, year=year):
        title = (t.get("title") or "").lower()
        if title and (title == needle or needle in title or title in needle):
            return t
    return None


def is_available() -> bool:
    if _apifootball_configured():
        try:
            from betbot.data_sources import api_football as af
            if af.is_available():
                return True
        except Exception:
            pass
    from betbot.data_sources import understat
    return understat.is_available()
