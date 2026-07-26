"""
In-season team-stats refresh via api-football.

The football-data.org path (`main.update_team_stats`) only covers the big
European leagues — all on summer break June-August. This module fills the gap:
it pulls finished fixtures for the currently in-season leagues (Scandinavia,
Brazil, MLS, Asia, South America…) from api-football and runs them through the
EXACT same modelling pipeline (league averages → Poisson attack/defense →
internal ELO → H2H), so `blended_match_probs` runs on those matches instead of
falling back to the noisy market-consensus model.

Requires API_FOOTBALL_KEY (a paid plan is recommended — a full refresh of ~20
leagues is a few hundred cheap /fixtures calls). Safe to run repeatedly:
every write is an idempotent upsert.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from betbot.data_sources import api_football
from betbot.db import Database
from betbot.elo_local import compute_elo_ratings
from betbot.models import build_team_stats, compute_league_averages

logger = logging.getLogger("betbot.stats_inseason")


def refresh_inseason_stats(
    db: Database,
    only_keys: list[str] | None = None,
    min_matches: int = 30,
) -> dict:
    """Populate team_stats for in-season leagues from api-football.

    only_keys : restrict to these sport_keys (default = all mapped leagues).
    min_matches : skip a league whose current season has fewer finished matches
                  than this (too thin for a stable Poisson fit).

    Returns a summary dict {leagues:{...}, teams_upserted, leagues_done,
    leagues_skipped:{key:reason}}.
    """
    if not os.getenv("API_FOOTBALL_KEY", "").strip():
        return {"error": "API_FOOTBALL_KEY non configurée", "leagues": {},
                "teams_upserted": 0, "leagues_done": 0, "leagues_skipped": {}}

    season = datetime.now(timezone.utc).year  # summer leagues are calendar-year
    summary: dict = {"season": season, "leagues": {}, "teams_upserted": 0,
                     "leagues_done": 0, "leagues_skipped": {}}

    targets = api_football.IN_SEASON_LEAGUE_ID.items()
    for sport_key, league_id in targets:
        if only_keys and sport_key not in only_keys:
            continue
        try:
            parsed = api_football.get_finished_matches(league_id, season)
        except Exception as exc:  # noqa: BLE001
            logger.warning("in-season fetch failed for %s (%s): %s",
                           sport_key, league_id, exc)
            summary["leagues_skipped"][sport_key] = f"fetch error: {str(exc)[:120]}"
            continue

        if len(parsed) < min_matches:
            summary["leagues_skipped"][sport_key] = (
                f"trop peu de matchs ({len(parsed)} < {min_matches})")
            continue

        home_avg, away_avg = compute_league_averages(parsed)
        db.upsert_league_averages(sport_key, home_avg, away_avg, len(parsed))
        local_elo = compute_elo_ratings(parsed)
        league_code = f"AF-{league_id}"

        teams = {m["home_team"] for m in parsed} | {m["away_team"] for m in parsed}
        saved = 0
        for team in teams:
            stats = build_team_stats(team, parsed, home_avg, away_avg)
            if not stats:
                continue
            db.upsert_team_stats(
                team_name=stats.name,
                sport_key=sport_key,
                league_code=league_code,
                attack_home=stats.attack_home,
                defense_home=stats.defense_home,
                attack_away=stats.attack_away,
                defense_away=stats.defense_away,
                matches_analyzed=stats.matches_analyzed,
            )
            _elo = local_elo.get(team)
            if _elo is not None:
                db.update_team_enrichment(
                    team_name=stats.name, sport_key=sport_key, elo_rating=_elo)
            saved += 1

        # H2H — cheap derivative of the same match list (reuses main's helper).
        h2h_pairs = 0
        try:
            from betbot.main import _compute_h2h_for_league
            for (team_a, team_b), h in _compute_h2h_for_league(parsed).items():
                db.upsert_head_to_head(
                    sport_key=sport_key, team_a=team_a, team_b=team_b,
                    team_a_wins=h["team_a_wins"], draws=h["draws"],
                    team_b_wins=h["team_b_wins"],
                    team_a_goals_avg=h["team_a_goals_avg"],
                    team_b_goals_avg=h["team_b_goals_avg"])
                h2h_pairs += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("H2H skipped for %s: %s", sport_key, exc)

        summary["leagues"][sport_key] = {
            "matches": len(parsed), "teams": saved, "h2h_pairs": h2h_pairs,
            "home_avg": round(home_avg, 2), "away_avg": round(away_avg, 2),
        }
        summary["teams_upserted"] += saved
        summary["leagues_done"] += 1
        logger.info("in-season %s : %d matchs, %d équipes, moy %.2f/%.2f",
                    sport_key, len(parsed), saved, home_avg, away_avg)

    logger.info("Refresh in-season terminé : %d ligues, %d équipes",
                summary["leagues_done"], summary["teams_upserted"])
    return summary
