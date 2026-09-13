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


def filter_by_kickoff_hour(events: list[dict], hour: int,
                           tz_name: str = "Europe/Paris") -> list[dict]:
    """Keep only events whose kickoff falls in the given LOCAL hour slot.

    The user's manual workflow: pick one time slot and bet a batch of matches
    that all start together — they resolve together, one visit to the bookmaker,
    one visit back. `hour` is the Paris-local hour (0-23): hour=20 keeps
    kickoffs from 20:00:00 to 20:59:59 Paris time.

    Events whose commence_time is missing or unparsable are dropped — a match
    we cannot place in a slot does not belong to a slot-filtered scan.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    kept: list[dict] = []
    for event in events:
        raw = event.get("commence_time", "")
        try:
            ko = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        if ko.astimezone(tz).hour == hour:
            kept.append(event)
    return kept


def _disambiguate(name: str, candidates: list[dict], target: str) -> dict | None:
    """Pick the one row that is unmistakably this club, or none at all.

    One candidate is easy. Several is where clubs get confused, so the rule is
    narrow: a club that plays a continental competition also plays domestically,
    and its continental row is thin by construction (a handful of fixtures)
    while the domestic one carries a season. So continental rows are dropped
    first — Boca Juniors sits under both Argentina (21 matches) and the
    Libertadores (6), and those are the same club.

    What survives that filter must still be a SINGLE domestic league. Two
    countries fielding a "Nacional" are two different clubs, and no amount of
    sample size tells them apart: we decline and let the consensus model price
    the match rather than lend one club the other's form.
    """
    from betbot.sport_keys import confederation_of, is_continental

    pool = [c for c in candidates if c.get("sport_key") != target]
    # Stats may cross a border, never an ocean: a UEFA fixture only fields
    # clubs from UEFA leagues. Without this filter the Champions League pool
    # carried Barcelona SC (Ecuador), Sporting Kansas City and Atletico-MG as
    # fuzzy bait — and "Barcelona" was priced with the Ecuadorian club's form.
    confed = confederation_of(target)
    pool = [c for c in pool
            if confederation_of(c.get("sport_key", "")) == confed]
    if not pool:
        return None
    domestic = [c for c in pool if not is_continental(c.get("sport_key", ""))]
    if len(domestic) == 1:
        return domestic[0]
    if not domestic and len(pool) == 1:
        return pool[0]
    return None            # genuinely ambiguous, or nothing to borrow


def _build_global_team_pool(db: Database) -> dict[str, list[dict]]:
    """Every team_stats row indexed by team name, across all competitions.

    Used only to lend stats to continental competitions. Names carrying more
    than one entry are kept in the index so callers can SEE the ambiguity and
    decline — they are never silently collapsed to one club.
    """
    from collections import defaultdict

    from sqlalchemy import select

    from betbot.database import session_scope
    from betbot.orm_models import TeamStat

    pool: dict[str, list[dict]] = defaultdict(list)
    with session_scope() as s:
        rows = s.execute(
            select(TeamStat.team_name, TeamStat.sport_key,
                   TeamStat.attack_home, TeamStat.defense_home,
                   TeamStat.attack_away, TeamStat.defense_away,
                   TeamStat.matches_analyzed, TeamStat.elo_rating,
                   TeamStat.xg_for, TeamStat.xg_against)
        ).all()
    for r in rows:
        pool[r[0]].append({
            "team_name": r[0], "sport_key": r[1],
            "attack_home": r[2], "defense_home": r[3],
            "attack_away": r[4], "defense_away": r[5],
            "matches_analyzed": r[6], "elo_rating": r[7],
            "xg_for": r[8], "xg_against": r[9],
        })
    return dict(pool)


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
    from betbot.sport_keys import is_continental, parent_leagues

    result: dict[str, dict] = {}
    _global_pool: dict[str, list[dict]] | None = None   # built lazily, once
    for sport_key in sport_keys:
        rows = db.get_all_team_stats_for_league(sport_key)
        # A cup has no squad of its own — its entrants are stored under their
        # domestic league. Without this, every FA Cup / DFB Pokal / Leagues Cup
        # tie fell through to the market consensus while the stats sat in the
        # database one key away.
        parents = parent_leagues(sport_key)
        if parents:
            rows = list(rows or [])
            seen = {r["team_name"]: r for r in rows}
            for parent in parents:
                for row in db.get_all_team_stats_for_league(parent) or []:
                    prev = seen.get(row["team_name"])
                    # A club promoted or relegated appears in two tiers; keep
                    # whichever rating rests on more matches.
                    if prev is None or (row.get("matches_analyzed") or 0) > (
                            prev.get("matches_analyzed") or 0):
                        seen[row["team_name"]] = row
            rows = list(seen.values())

        # A continental competition's own squad list only holds clubs that
        # already played a round in it. Its entrants' real stats live under
        # their domestic leagues, so borrow from there.
        #
        # ONLY names that are globally UNAMBIGUOUS are borrowed. The table
        # holds "Viking" (Norway) and "Vikingur Reykjavik" (Iceland); it holds
        # a "Boca Juniors" in Argentina and another under Libertadores. Pulling
        # 800 candidates into one pool and fuzzy-matching across borders is how
        # a club gets priced with a foreign namesake's form — the Dundee bug
        # with a passport. The competition's own rows always win.
        if is_continental(sport_key):
            if _global_pool is None:
                _global_pool = _build_global_team_pool(db)
            own = {r["team_name"] for r in (rows or [])}
            rows = list(rows or [])
            for name, candidates in _global_pool.items():
                if name in own:
                    continue
                pick = _disambiguate(name, candidates, sport_key)
                if pick is not None:
                    rows.append(pick)

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
        if parents:
            # Scoring level for a cup comes from the FEEDER LEAGUES, always —
            # never from the cup's own row, even when one exists. A cup plays
            # a few dozen tier-mixed ties a season: measured 2026-08-07, the
            # EFL Cup's own row read 1.31 home / 1.51 away, i.e. an INVERTED
            # home advantage, which is a sampling artefact rather than a
            # property of cup football. Its feeder leagues rest on full
            # seasons and describe the same clubs.
            parent_avgs = [a for a in (db.get_league_averages(p) for p in parents) if a]
            if parent_avgs:
                avgs = (sum(a[0] for a in parent_avgs) / len(parent_avgs),
                        sum(a[1] for a in parent_avgs) / len(parent_avgs))
        home_avg, away_avg = avgs if avgs else (DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG)
        h2h = dict(db.get_all_h2h_for_league(sport_key) or {})
        for parent in parents:
            for pair, rec in (db.get_all_h2h_for_league(parent) or {}).items():
                h2h.setdefault(pair, rec)   # league meetings inform cup ties
        result[sport_key] = {
            "teams": teams,
            "home_avg": home_avg,
            "away_avg": away_avg,
            "h2h": h2h,
        }
    return result
