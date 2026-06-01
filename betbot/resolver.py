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
