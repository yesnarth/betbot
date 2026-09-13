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

# How much of a carried-over rating we keep when the CURRENT season is still
# too thin to stand on its own. Squads change between seasons, so last year's
# level is informative but not as certain as this year's — we inherit the
# level, not the confidence. Coefficients are multiplicative around 1.0, so
# shrinking toward 1.0 is shrinking toward "league average".
CARRYOVER_SHRINK = 0.75


def _shrink(coef: float, weight: float) -> float:
    """Pull a multiplicative attack/defense coefficient toward 1.0."""
    return 1.0 + (coef - 1.0) * weight


def _load_matches(league_id: int, season: int, min_matches: int) -> tuple[list[dict], str, float]:
    """Finished matches for a league, falling back to the previous season.

    Every autumn-spring league is unusable for the first ~2 months of its
    season: measured 2026-08-07, Belgium/Greece/Italy-B had ZERO finished
    matches in season 2026 against 319/236/390 in 2025, so the `min_matches`
    guard skipped them outright and they silently degraded to the market
    consensus. The data was one parameter away the whole time.

    Concatenating both seasons is safe because `build_team_stats` sorts by
    date descending and time-weights: the current season's few matches
    dominate naturally, last season only supplies depth.

    Returns (matches, label, shrink_weight).
    """
    current = api_football.get_finished_matches(league_id, season)
    if len(current) >= min_matches:
        return current, str(season), 1.0

    previous = api_football.get_finished_matches(league_id, season - 1)
    if not previous:
        return current, str(season), 1.0

    # The more of the current season we already have, the less we shrink.
    filled = min(1.0, len(current) / float(min_matches)) if min_matches > 0 else 0.0
    weight = CARRYOVER_SHRINK + (1.0 - CARRYOVER_SHRINK) * filled
    return previous + current, f"{season - 1}+{season}", weight


def _hours_since_last_refresh() -> float | None:
    """Age in hours of the freshest row this pipeline wrote, or None if never.

    Only rows stamped with an `AF-` league code count: those are the ones this
    module produces, so a football-data refresh of the European leagues cannot
    make the in-season data look fresher than it is.
    """
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from betbot.database import session_scope
    from betbot.orm_models import TeamStat

    with session_scope() as s:
        newest = s.execute(
            select(func.max(TeamStat.updated_at))
            .where(TeamStat.league_code.like("AF-%"))
        ).scalar()
    if not newest:
        return None
    try:
        when = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def refresh_inseason_stats(
    db: Database,
    only_keys: list[str] | None = None,
    min_matches: int = 30,
    max_age_hours: float | None = None,
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

    # Freshness gate. This job costs roughly 34 leagues x 2 seasons x pagination
    # of api-football calls, and it used to fire unconditionally at EVERY worker
    # boot. On 2026-08-18 a day of deployments restarted the worker ~10 times
    # and drained the 7,500/day allowance, after which every league silently
    # refreshed to "0 leagues, 0 teams". Same shape as the Odds API drain fixed
    # earlier: a catch-up job that re-pays on every restart.
    #
    # The SCHEDULED daily run passes no ceiling and always refreshes; only the
    # boot call asks to be skipped when the data is already recent.
    if max_age_hours is not None:
        age = _hours_since_last_refresh()
        if age is not None and age < max_age_hours:
            logger.info("Stats in-season fraîches (%.1f h < %.1f h) — "
                        "rafraîchissement au démarrage ignoré", age, max_age_hours)
            return {"skipped": "fresh", "age_hours": round(age, 1), "leagues": {},
                    "teams_upserted": 0, "leagues_done": 0, "leagues_skipped": {}}

    season = datetime.now(timezone.utc).year  # summer leagues are calendar-year
    summary: dict = {"season": season, "leagues": {}, "teams_upserted": 0,
                     "leagues_done": 0, "leagues_skipped": {}}

    targets = api_football.IN_SEASON_LEAGUE_ID.items()
    for sport_key, league_id in targets:
        if only_keys and sport_key not in only_keys:
            continue
        try:
            parsed, season_label, shrink_w = _load_matches(
                league_id, season, min_matches)
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
                attack_home=_shrink(stats.attack_home, shrink_w),
                defense_home=_shrink(stats.defense_home, shrink_w),
                attack_away=_shrink(stats.attack_away, shrink_w),
                defense_away=_shrink(stats.defense_away, shrink_w),
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
            "season": season_label, "carryover_weight": round(shrink_w, 3),
        }
        summary["teams_upserted"] += saved
        summary["leagues_done"] += 1
        logger.info("in-season %s : %d matchs (saison %s, poids %.2f), "
                    "%d équipes, moy %.2f/%.2f",
                    sport_key, len(parsed), season_label, shrink_w,
                    saved, home_avg, away_avg)

    logger.info("Refresh in-season terminé : %d ligues, %d équipes",
                summary["leagues_done"], summary["teams_upserted"])
    return summary
