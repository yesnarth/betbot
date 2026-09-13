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


def _leagues_with_stats(db: Database) -> list[str]:
    """Every sport_key that actually has team_stats rows.

    Enrichment used to walk the hard-coded `SPORT_KEYS` wishlist — 10 leagues —
    while the scan covers 46 and the database holds stats for 45. Every league
    outside that list was enriched with nothing, which is most of why only
    11.5% of teams carried xG.
    """
    from sqlalchemy import distinct, select

    from betbot.database import session_scope
    from betbot.orm_models import TeamStat

    with session_scope() as s:
        rows = s.execute(select(distinct(TeamStat.sport_key))).scalars().all()
    return sorted(k for k in rows if k)


def _inherit_xg_across_leagues() -> int:
    """Fill missing xG from the SAME club's row in another competition.

    A cross-league competition has no statistics of its own: measured
    2026-08-10, the Champions League (36 teams) and its qualifying rounds (12)
    carried no xG at all, while every one of those clubs already had xG under
    its domestic league. Nothing needs fetching — the numbers are in the table,
    one sport_key away. Zero API calls.

    Two rules keep this safe, and they are the whole design:

    * EXACT name match, never fuzzy. Cross-competition fuzzy matching is
      precisely where clubs get confused, and a wrong xG is silent.
    * Only when EXACTLY ONE other competition supplies it. If a name appears
      with xG in two places we cannot tell which club is meant, so we decline
      rather than guess.

    An existing xG is never overwritten: this only fills holes.
    """
    from collections import defaultdict

    from sqlalchemy import select

    from betbot.database import session_scope
    from betbot.orm_models import TeamStat

    filled = 0
    with session_scope() as s:
        rows = s.execute(
            select(TeamStat.team_name, TeamStat.sport_key,
                   TeamStat.xg_for, TeamStat.xg_against)
        ).all()

        donors: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
        needy: list[tuple[str, str]] = []
        for name, sport_key, xgf, xga in rows:
            if xgf is not None:
                donors[name].append((sport_key, xgf, xga))
            else:
                needy.append((name, sport_key))

        for name, sport_key in needy:
            candidates = [d for d in donors.get(name, []) if d[0] != sport_key]
            if len(candidates) != 1:
                continue          # unknown, or ambiguous — decline either way
            _, xgf, xga = candidates[0]
            row = s.get(TeamStat, (name, sport_key))
            if row is None or row.xg_for is not None:
                continue
            row.xg_for = xgf
            row.xg_against = xga
            filled += 1

    if filled:
        logger.info("xG hérité entre compétitions : %d équipe(s)", filled)
    return filled


def enrich_team_stats(db: Database, xg_call_budget: int | None = None) -> dict[str, int]:
    """
    Walk every (team_name, sport_key) row in team_stats and fill in the
    enrichment columns. Returns a counters dict.

    xg_call_budget : total api-football calls this run may spend on xG across
        ALL leagues (default `XG_CALL_BUDGET`, else 5000). xG costs roughly
        `teams x 7` calls per league, so 45 leagues is ~5,600 — affordable on
        the Pro plan's 7,500/day, but only if it is budgeted globally. The
        per-league cap alone would allow 45 x 400.
    """
    counts = {"teams_seen": 0, "elo_filled": 0, "xg_filled": 0, "errors": 0,
              "leagues_seen": 0, "xg_calls": 0, "xg_budget_exhausted": 0,
              "xg_inherited": 0}

    # 1) Pre-fetch the global Elo snapshot once (1 HTTP call total)
    try:
        elo_snapshot = club_elo.get_all_elo_ratings()
    except Exception as exc:
        logger.warning("Club Elo unavailable: %s — Elo enrichment skipped", exc)
        elo_snapshot = {}

    # 2) Iterate leagues: fetch that league's xG, then upsert its rows IMMEDIATELY.
    #    (Was: pre-fetch ALL leagues then persist — which lost everything if the
    #    fetch died mid-way on a flaky link. Per-league persist keeps progress.)
    import os

    from betbot.data_sources import api_football as _af

    if xg_call_budget is None:
        try:
            xg_call_budget = int(os.getenv("XG_CALL_BUDGET", "5000"))
        except ValueError:
            xg_call_budget = 5000
    spent_at_start = _af.calls_made()

    now = datetime.now(timezone.utc).isoformat()
    for sport_key in _leagues_with_stats(db):
        counts["leagues_seen"] += 1
        xg_map: dict[str, dict] = {}
        remaining = xg_call_budget - (_af.calls_made() - spent_at_start)
        if remaining <= 0:
            # Out of budget: keep going so Elo still gets filled for the rest.
            counts["xg_budget_exhausted"] += 1
        else:
            try:
                teams = xg.get_league_xg(sport_key, max_calls=remaining)
                # Keyed by the ORIGINAL title: _fuzzy_lookup does its own
                # normalization and needs the real spelling to tokenize.
                xg_map = {t["title"]: t for t in teams}
            except Exception as exc:
                logger.warning("xG source unavailable for %s : %s", sport_key, exc)
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
            if xg_map:
                # Same hardened matcher as the model uses. This used to be
                # `title in needle or needle in title`, the very substring rule
                # that handed Dundee United's numbers to Dundee: "dundee" is a
                # substring of "dundee united". Attaching one club's xG to
                # another is the same silent corruption, one layer down.
                from betbot.analysis import _fuzzy_lookup
                match, _matched_name = _fuzzy_lookup(team_name, xg_map)
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

        if xg_map:
            # _fuzzy_lookup memoizes its index by id(dict); these per-league
            # maps are short-lived, so a freed id could be reused by the next
            # league's map of the same size and serve a stale index.
            from betbot.analysis import _invalidate_norm_cache
            _invalidate_norm_cache(xg_map)
            logger.info("  %s : xG persisté (%d équipes en source)", sport_key, len(xg_map))

    counts["xg_inherited"] = _inherit_xg_across_leagues()
    counts["xg_calls"] = _af.calls_made() - spent_at_start
    logger.info(
        "Enrichissement terminé : %d ligue(s), %d équipes, %d ELO, %d xG, "
        "%d erreurs (%d appels api-football, budget %d%s)",
        counts["leagues_seen"], counts["teams_seen"], counts["elo_filled"],
        counts["xg_filled"], counts["errors"], counts["xg_calls"], xg_call_budget,
        f", {counts['xg_budget_exhausted']} ligue(s) sans xG faute de budget"
        if counts["xg_budget_exhausted"] else "",
    )
    return counts
