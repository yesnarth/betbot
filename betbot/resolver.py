"""
Auto-resolve pending predictions by fetching match scores from The Odds API.

Flow:
  1. Read predictions where result IS NULL
  2. Group by sport_key
  3. Fetch /scores for each sport (last 3 days)
  4. Match completed events to pending predictions by event_id
  5. Decide win/loss/void for each prediction's selection
  6. Update DB
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from betbot.api import OddsAPIClient, QuotaExhaustedError
from betbot.db import Database

logger = logging.getLogger("betbot.resolver")


def _decide_h2h_outcome(
    selection_code: str,
    home_team: str,
    away_team: str,
    scores: list[dict],
) -> str | None:
    """
    Return 'win' / 'loss' / 'void', or None if scores are incomplete.
    selection_code: '1' (home), 'X' (draw), '2' (away).
    scores: [{name: "Home Name", score: "2"}, {name: "Away Name", score: "1"}]
    """
    score_map: dict[str, int] = {}
    for s in scores or []:
        name = s.get("name", "")
        try:
            score_map[name] = int(s["score"])
        except (KeyError, ValueError, TypeError):
            return None

    home_goals = score_map.get(home_team)
    away_goals = score_map.get(away_team)
    if home_goals is None or away_goals is None:
        return None

    if home_goals == away_goals:
        actual = "X"
    elif home_goals > away_goals:
        actual = "1"
    else:
        actual = "2"

    return "win" if selection_code == actual else "loss"


def _decide_totals_outcome(
    selection_code: str,
    scores: list[dict],
) -> str | None:
    """
    Resolve an Over/Under *total goals* bet from the final score.

    selection_code is the letter O/U followed by the line ×10 :
        O25 → Over 2.5,  U25 → Under 2.5,  O15 → Over 1.5,  O35 → Over 3.5 …
    The total is the sum of both teams' scores. Half-lines (x.5) never push, so
    the result is always 'win' or 'loss' once both scores are known; returns None
    if a score is missing (match not fully scored yet → stays pending).

    NOTE: this is why the model only proposes totals on .5 lines — a whole-number
    line (e.g. 2.0) could push, which we don't currently settle.
    """
    if len(selection_code) < 2 or selection_code[0] not in ("O", "U"):
        return None
    try:
        line = int(selection_code[1:]) / 10.0
    except ValueError:
        return None

    total = 0
    seen = 0
    for s in scores or []:
        try:
            total += int(s["score"])
            seen += 1
        except (KeyError, ValueError, TypeError):
            return None
    if seen < 2:
        return None

    if selection_code[0] == "O":
        return "win" if total > line else "loss"
    return "win" if total < line else "loss"


def _actual_1x2(home_team: str, away_team: str, scores: list[dict]) -> str | None:
    """Final 1/X/2 result from the score list, or None if incomplete."""
    score_map: dict[str, int] = {}
    for s in scores or []:
        try:
            score_map[s.get("name", "")] = int(s["score"])
        except (KeyError, ValueError, TypeError):
            return None
    hg, ag = score_map.get(home_team), score_map.get(away_team)
    if hg is None or ag is None:
        return None
    return "X" if hg == ag else ("1" if hg > ag else "2")


def _decide_dc_outcome(selection_code, home_team, away_team, scores) -> str | None:
    """Double Chance: 1X / X2 / 12 → 'win' / 'loss' (never pushes)."""
    actual = _actual_1x2(home_team, away_team, scores)
    if actual is None:
        return None
    covered = {"1X": {"1", "X"}, "X2": {"X", "2"}, "12": {"1", "2"}}.get(selection_code)
    if covered is None:
        return None
    return "win" if actual in covered else "loss"


def _decide_dnb_outcome(selection_code, home_team, away_team, scores) -> str | None:
    """Draw No Bet: DNB1 (home) / DNB2 (away). Draw → 'void' (stake refunded)."""
    actual = _actual_1x2(home_team, away_team, scores)
    if actual is None:
        return None
    if actual == "X":
        return "void"
    side = {"DNB1": "1", "DNB2": "2"}.get(selection_code)
    if side is None:
        return None
    return "win" if actual == side else "loss"


def resolve_pending(
    db: Database,
    odds_client: OddsAPIClient,
    days_from: int = 3,
) -> dict[str, int]:
    """
    Resolve all pending predictions whose match has finished.
    Returns counts: {"resolved": N, "still_pending": M, "errors": E}.
    """
    pending = db.get_pending_predictions()
    if not pending:
        logger.info("Aucune prédiction en attente.")
        return {"resolved": 0, "still_pending": 0, "errors": 0}

    by_sport: dict[str, list[dict]] = defaultdict(list)
    for p in pending:
        by_sport[p["sport_key"]].append(p)

    resolved = 0
    errors = 0
    for sport_key, preds in by_sport.items():
        try:
            scores_events = odds_client.get_scores(sport_key, days_from=days_from)
        except QuotaExhaustedError:
            logger.error("Quota épuisé — arrêt de la résolution")
            break
        except Exception as exc:
            logger.warning("Impossible de récupérer scores pour %s : %s", sport_key, exc)
            errors += 1
            continue

        # Build event_id → completed event lookup
        completed = {
            e["id"]: e for e in scores_events
            if e.get("completed") and e.get("scores")
        }

        for pred in preds:
            event = completed.get(pred["event_id"])
            if not event:
                continue
            market = pred["market"]
            scores = event.get("scores", [])
            if market == "h2h":
                outcome = _decide_h2h_outcome(
                    selection_code=pred["selection"],
                    home_team=pred["home_team"],
                    away_team=pred["away_team"],
                    scores=scores,
                )
            elif market == "totals":
                outcome = _decide_totals_outcome(pred["selection"], scores)
            elif market == "double_chance":
                outcome = _decide_dc_outcome(
                    pred["selection"], pred["home_team"], pred["away_team"], scores)
            elif market == "draw_no_bet":
                outcome = _decide_dnb_outcome(
                    pred["selection"], pred["home_team"], pred["away_team"], scores)
            else:
                logger.debug("Skip marché non géré: %s", market)
                continue
            if outcome is None:
                continue
            db.update_result(
                event_id=pred["event_id"],
                market=pred["market"],
                selection=pred["selection"],
                result=outcome,
            )
            resolved += 1
            logger.info(
                "Résolu : %s vs %s [%s] → %s",
                pred["home_team"], pred["away_team"], pred["selection"], outcome,
            )

    still_pending = len(pending) - resolved
    logger.info(
        "Résolution : %d résolu(s), %d en attente, %d erreur(s)",
        resolved, still_pending, errors,
    )
    return {"resolved": resolved, "still_pending": still_pending, "errors": errors}


def _resolve_from_results(pending: list[dict], parsed: list[dict]) -> list[tuple[str, str, str, str]]:
    """Match stale pending bets to historical results (football-data.org) and
    decide each outcome. Pure → unit-testable.

    Team names differ between the Odds API (the bet) and football-data (the
    result) — e.g. "Bayer Leverkusen"/"Bayer 04 Leverkusen", "Athletic
    Bilbao"/"Athletic Club" — so we reuse the model's full name matcher
    (``_fuzzy_lookup``: exact → normalized → alias → token-set → fuzzy) rather
    than exact matching. Returns [(event_id, market, selection, outcome)].
    """
    from betbot.analysis import _fuzzy_lookup, _invalidate_norm_cache

    # team_cache: canonical football-data name → itself (the _fuzzy_lookup index)
    # match_index: (fd_home, fd_away) → (home_goals, away_goals)
    team_cache: dict[str, str] = {}
    match_index: dict[tuple[str, str], tuple[int, int]] = {}
    for m in parsed:
        h, a = m.get("home_team"), m.get("away_team")
        if not (h and a):
            continue
        try:
            hg, ag = int(m.get("home_goals", 0) or 0), int(m.get("away_goals", 0) or 0)
        except (ValueError, TypeError):
            continue
        team_cache[h] = h
        team_cache[a] = a
        match_index[(h, a)] = (hg, ag)

    # _fuzzy_lookup memoizes its index by id(team_cache); this short-lived dict
    # could collide with a freed dict of the same size, so evict around use.
    _invalidate_norm_cache(team_cache)
    out: list[tuple[str, str, str, str]] = []
    try:
        for p in pending:
            _, fd_home = _fuzzy_lookup(p["home_team"], team_cache)
            _, fd_away = _fuzzy_lookup(p["away_team"], team_cache)
            if not (fd_home and fd_away):
                continue
            sc = match_index.get((fd_home, fd_away))
            if sc is None:
                continue
            hg, ag = sc
            scores = [{"name": p["home_team"], "score": hg}, {"name": p["away_team"], "score": ag}]
            market = p.get("market")
            if market == "h2h":
                outcome = _decide_h2h_outcome(p["selection"], p["home_team"], p["away_team"], scores)
            elif market == "totals":
                outcome = _decide_totals_outcome(p["selection"], scores)
            elif market == "double_chance":
                outcome = _decide_dc_outcome(p["selection"], p["home_team"], p["away_team"], scores)
            elif market == "draw_no_bet":
                outcome = _decide_dnb_outcome(p["selection"], p["home_team"], p["away_team"], scores)
            else:
                continue
            if outcome:
                out.append((p["event_id"], market, p["selection"], outcome))
    finally:
        _invalidate_norm_cache(team_cache)
    return out


def resolve_stale_pending(db: Database, fd_api_key: str, min_age_days: int = 2) -> dict:
    """Fallback resolver for confirmed-pending FOOTBALL bets too old for the Odds
    API /scores window (3 days): resolve them from football-data.org historical
    results, so a bet confirmed but not resolved within ~3 days no longer becomes
    a permanent 'zombie'. Tennis/basket are not covered (football-data is football).
    """
    from datetime import datetime, timezone

    from betbot.football_api import LEAGUE_MAP, FootballDataClient, parse_match_results

    if not fd_api_key or "REMPLACE" in fd_api_key:
        return {"resolved": 0, "reason": "football-data key not configured"}

    pending = db.get_pending_predictions()
    now = datetime.now(timezone.utc)

    def _age_days(iso: str) -> float:
        try:
            return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
        except (ValueError, TypeError, AttributeError):
            return 0.0

    stale_by_sport: dict[str, list[dict]] = {}
    for p in pending:
        if p.get("sport_key") in LEAGUE_MAP and _age_days(p.get("created_at", "")) >= min_age_days:
            stale_by_sport.setdefault(p["sport_key"], []).append(p)

    if not stale_by_sport:
        return {"resolved": 0, "still_pending": len(pending)}

    fd = FootballDataClient(fd_api_key)
    resolved = 0
    for sport_key, preds in stale_by_sport.items():
        comp = LEAGUE_MAP.get(sport_key)
        if not comp:
            continue
        try:
            parsed = parse_match_results(fd.get_recent_matches(comp, limit=300))
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale-resolve: fetch %s échoué : %s", sport_key, exc)
            continue
        for eid, market, selection, outcome in _resolve_from_results(preds, parsed):
            db.update_result(event_id=eid, market=market, selection=selection, result=outcome)
            resolved += 1
            logger.info("stale-resolve : %s [%s] → %s", eid, selection, outcome)

    logger.info("Résolution tardive : %d résolu(s) via football-data.org", resolved)
    return {"resolved": resolved, "still_pending": len(pending) - resolved}


def resolve_proposed_picks(db: Database, fd_api_key: str, min_age_days: int = 1) -> dict:
    """Shadow-grade PROPOSED (never-bet) FOOTBALL picks from football-data.org —
    FREE, no Odds quota — so the model's FULL would-have track record is measured,
    not just the picks the user confirmed. This is what tells us whether the
    predictions are actually good and improving (feeds model_performance + the
    weekly calibrator retrain).

    NO bankroll effect: db.update_result only writes the `result` field for
    non-confirmed picks (its `is_money_at_stake = stake>0 AND confirmed` guard).
    A pick, once graded, leaves the validation queue (filtered on result IS NULL),
    so it can never be confirmed-then-double-settled.
    """
    from datetime import datetime, timezone

    from betbot.football_api import LEAGUE_MAP, FootballDataClient, parse_match_results

    if not fd_api_key or "REMPLACE" in fd_api_key:
        return {"resolved": 0, "reason": "football-data key not configured"}

    proposed = db.get_proposed_predictions()
    now = datetime.now(timezone.utc)

    def _age_days(iso: str) -> float:
        try:
            return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
        except (ValueError, TypeError, AttributeError):
            return 0.0

    by_sport: dict[str, list[dict]] = {}
    for p in proposed:
        if p.get("sport_key") in LEAGUE_MAP and _age_days(p.get("created_at", "")) >= min_age_days:
            by_sport.setdefault(p["sport_key"], []).append(p)
    if not by_sport:
        return {"resolved": 0, "still_proposed": len(proposed)}

    fd = FootballDataClient(fd_api_key)
    resolved = 0
    for sport_key, preds in by_sport.items():
        comp = LEAGUE_MAP.get(sport_key)
        if not comp:
            continue
        try:
            parsed = parse_match_results(fd.get_recent_matches(comp, limit=300))
        except Exception as exc:  # noqa: BLE001
            logger.warning("shadow-resolve: fetch %s échoué : %s", sport_key, exc)
            continue
        for eid, market, selection, outcome in _resolve_from_results(preds, parsed):
            db.update_result(event_id=eid, market=market, selection=selection, result=outcome)
            resolved += 1
    logger.info("Résolution shadow (proposed) : %d pick(s) notés via football-data.org", resolved)
    return {"resolved": resolved, "still_proposed": len(proposed) - resolved}
