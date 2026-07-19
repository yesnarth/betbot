"""
Pipeline that enriches every team_stats row in the DB with external signals:

  - Club Elo rating (free, no key)
  - Understat xG / xGA / xPts (free scrape)

Run via:
    python -m betbot.main --enrich

Idempotent: each call refreshes Elo + xG for every team in DB. Failures on a
single team are logged but don't abort the rest of the run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from betbot.data_sources import club_elo, xg
from betbot.db import Database

logger = logging.getLogger("betbot.enrichment")


def enrich_team_stats(db: Database) -> dict[str, int]:
    """
    Walk every (team_name, sport_key) row in team_stats and fill in the
    enrichment columns. Returns a counters dict.
    """
    counts = {"teams_seen": 0, "elo_filled": 0, "xg_filled": 0, "errors": 0}

    # 1) Pre-fetch the global Elo snapshot once (1 HTTP call total)
    try:
        elo_snapshot = club_elo.get_all_elo_ratings()
    except Exception as exc:
        logger.warning("Club Elo unavailable: %s — Elo enrichment skipped", exc)
        elo_snapshot = {}

    # 2) For each league, pre-fetch xG in one shot (api-football when configured,
    #    else Understat). Facade degrades to [] so enrichment never breaks.
    from betbot.api import SPORT_KEYS
    xg_by_league: dict[str, dict[str, dict]] = {}
    for sport_key in SPORT_KEYS:
        try:
            teams = xg.get_league_xg(sport_key)
            if teams:
                xg_by_league[sport_key] = {t["title"].lower(): t for t in teams}
        except Exception as exc:
            logger.warning("xG source unavailable for %s : %s", sport_key, exc)

    # 3) Iterate rows + upsert
    now = datetime.now(timezone.utc).isoformat()
    for sport_key in SPORT_KEYS:
        rows = db.get_all_team_stats_for_league(sport_key)
        for row in rows:
            counts["teams_seen"] += 1
            team_name = row["team_name"]

            # -------- Elo --------
            elo_value: float | None = None
            try:
                norm = club_elo._normalize(team_name)
                if norm in elo_snapshot:
                    elo_value = elo_snapshot[norm]
                else:
                    # Substring / fuzzy fallback
                    for k, v in elo_snapshot.items():
                        if len(norm) >= 5 and (norm in k or k in norm):
                            elo_value = v
                            break
                if elo_value is not None:
                    counts["elo_filled"] += 1
            except Exception as exc:
                logger.debug("Elo lookup failed for %s : %s", team_name, exc)
                counts["errors"] += 1

            # -------- xG --------
            xg_for = xg_against = npxg_for = npxg_against = xpts = None
            xg_map = xg_by_league.get(sport_key, {})
            if xg_map:
                # Match by lowercased team title with permissive contains
                needle = team_name.lower()
                match = None
                for title, t in xg_map.items():
                    if title == needle or title in needle or needle in title:
                        match = t
                        break
                if match:
                    # Core signal (all sources provide it). npxG / xPts are
                    # Understat-only extras — api-football exposes plain xG per
                    # fixture, so guard them with .get() (stay None when absent).
                    xg_for = match.get("xg_per_match")
                    xg_against = match.get("xga_per_match")
                    m = max(match.get("matches", 1), 1)
                    if "npxg" in match:
                        npxg_for = match["npxg"] / m
                        npxg_against = match.get("npxga", 0.0) / m
                    if "xpts" in match:
                        xpts = match["xpts"] / m
                    if xg_for is not None:
                        counts["xg_filled"] += 1

            # -------- Persist --------
            db.update_team_enrichment(
                team_name=team_name,
                sport_key=sport_key,
                elo_rating=elo_value,
                xg_for=xg_for,
                xg_against=xg_against,
                npxg_for=npxg_for,
                npxg_against=npxg_against,
                xpts_per_match=xpts,
                sources_updated_at=now,
            )

    logger.info(
        "Enrichissement terminé : %d équipes, %d ELO, %d xG, %d erreurs",
        counts["teams_seen"], counts["elo_filled"], counts["xg_filled"], counts["errors"],
    )
    return counts
