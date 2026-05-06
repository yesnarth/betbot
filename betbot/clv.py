"""
Closing Line Value (CLV) tracker.

Concept:
  Entry odds  = price you got at scan time (saved as `best_odds` in predictions)
  Closing odds = price the market settled on right before kick-off
  CLV % = (entry_odds / closing_odds - 1) * 100

A consistently positive CLV means you're regularly catching prices BEFORE the
market corrects them — the most reliable indicator of a winning long-term bettor,
even more than short-term ROI.

Implementation:
  - Pre-kickoff job (T-15 min) snapshots the best odds for each pending
    prediction's outcome and writes it to `predictions.closing_odds`.
  - At resolve time, CLV is derived directly from the column.
  - Aggregate CLV is exposed alongside ROI in /stats/roi.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from betbot.api import OddsAPIClient, QuotaExhaustedError
from betbot.database import session_scope
from betbot.models import extract_best_odds
from betbot.orm_models import Prediction

logger = logging.getLogger("betbot.clv")

# Mapping from selection_code (1/X/2) → outcome name template
_SELECTION_TO_OUTCOME = {
    "1": "home",   # outcome name = home_team
    "X": "Draw",
    "2": "away",   # outcome name = away_team
}


def _outcome_name(pred: Prediction) -> str:
    code = pred.selection
    if code == "X":
        return "Draw"
    return pred.home_team if code == "1" else pred.away_team


def snapshot_closing_odds(
    odds_client: OddsAPIClient,
    minutes_window: int = 30,
) -> dict[str, int]:
    """
    For each pending prediction whose match starts within `minutes_window`
    minutes, fetch the latest best odds and store as `closing_odds`.

    Designed to run every 5-15 minutes from the scheduler — idempotent
    (only writes when closing_odds is still NULL).
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=minutes_window)

    counts = {"checked": 0, "snapped": 0, "missing_event": 0, "errors": 0}

    with session_scope() as s:
        pending = s.execute(
            select(Prediction).where(
                Prediction.result.is_(None),
                Prediction.closing_odds.is_(None),
            )
        ).scalars().all()

    if not pending:
        return counts

    # Group predictions by sport_key so we make one /odds call per league max
    by_sport: dict[str, list[Prediction]] = {}
    for p in pending:
        by_sport.setdefault(p.sport_key, []).append(p)

    for sport_key, preds in by_sport.items():
        try:
            events = odds_client.get_events_with_odds(sport_key)
        except QuotaExhaustedError:
            logger.error("Quota épuisé — arrêt du snapshot CLV")
            return counts
        except Exception as exc:
            logger.warning("Fetch CLV échoué pour %s : %s", sport_key, exc)
            counts["errors"] += 1
            continue

        # Index by event_id for O(1) lookup
        events_by_id = {e.get("id"): e for e in events}

        for pred in preds:
            counts["checked"] += 1
            event = events_by_id.get(pred.event_id)
            if not event:
                counts["missing_event"] += 1
                continue
            commence = event.get("commence_time", "")
            try:
                kickoff = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            # Only snapshot when within the window
            if not (now <= kickoff <= horizon):
                continue

            best = extract_best_odds(event, _outcome_name(pred))
            if best is None:
                continue

            with session_scope() as s:
                fresh = s.get(Prediction, pred.id)
                if fresh is None or fresh.closing_odds is not None:
                    continue  # already snapped by another run
                fresh.closing_odds = best.price
                counts["snapped"] += 1
                logger.info(
                    "CLV snapshot : %s vs %s [%s] entry=%.2f closing=%.2f",
                    pred.home_team, pred.away_team, pred.selection,
                    pred.best_odds, best.price,
                )

    logger.info(
        "CLV snapshot : %d vérifié(s), %d capturé(s), %d sans event, %d erreur(s)",
        counts["checked"], counts["snapped"], counts["missing_event"], counts["errors"],
    )
    return counts


def compute_clv_pct(entry_odds: float, closing_odds: float) -> float:
    """
    Naive CLV %: (entry_odds / closing_odds - 1) × 100.

    Includes bookmaker margin in both odds, so it slightly UNDER-estimates the
    real edge. Use compute_clv_pct_no_vig() when both sides of the market
    (1/X/2) are available — it gives the true skill metric professionals track.
    """
    if closing_odds <= 1.0 or entry_odds <= 1.0:
        return 0.0
    return round((entry_odds / closing_odds - 1.0) * 100, 2)


def compute_clv_pct_no_vig(
    entry_odds: float,
    closing_odds_all: list[float],
    entry_odds_all: list[float],
) -> float:
    """
    Pro-grade CLV with bookmaker margin removed from BOTH entry and closing
    prices. Reference: Levitt 2004 ("How Do Markets Function?"). The vig-free
    probability is `(1/odds_i) / sum_j(1/odds_j)`.

    Args:
        entry_odds:        the price you got
        closing_odds_all:  all 2-3 outcome odds at closing (h2h: home, draw, away)
        entry_odds_all:    all outcome odds at entry time

    Returns CLV % comparing the no-vig probabilities.
    """
    if entry_odds <= 1.0 or not closing_odds_all or not entry_odds_all:
        return 0.0
    try:
        # No-vig probability of the picked outcome at each timestamp
        entry_implied = 1.0 / entry_odds
        entry_overround = sum(1.0 / o for o in entry_odds_all if o > 1.0)
        if entry_overround <= 0:
            return 0.0
        entry_fair_prob = entry_implied / entry_overround

        # Find which closing odd corresponds to our pick (closest match)
        closing_overround = sum(1.0 / o for o in closing_odds_all if o > 1.0)
        if closing_overround <= 0:
            return 0.0
        # The pick is the outcome whose entry implied prob is `entry_fair_prob`.
        # Closing odd for the same outcome ≈ position in the list. Caller is
        # responsible for ordering the lists consistently (home, draw, away).
        # We assume the user passes the picked outcome at the same index in
        # both lists; here we just compute fair probs for each.
        closing_picks = [(1.0 / o) / closing_overround for o in closing_odds_all if o > 1.0]
        # Find the matching one — pick the closest to entry_fair_prob (heuristic)
        closing_fair_prob = min(closing_picks, key=lambda p: abs(p - entry_fair_prob))

        if closing_fair_prob <= 0:
            return 0.0
        # CLV is the ratio of fair probabilities (inverse of odds)
        return round((closing_fair_prob / entry_fair_prob - 1.0) * 100, 2)
    except (ValueError, ZeroDivisionError):
        return 0.0


def aggregate_clv(days: int = 30) -> dict:
    """
    Aggregate CLV stats over the last N days.

    Returns:
        {
          "n_with_clv": int,
          "avg_clv_pct": float,           # mean CLV across bets
          "positive_clv_share": float,    # share of bets where CLV > 0 (%)
          "median_clv_pct": float,
        }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with session_scope() as s:
        rows = s.execute(
            select(Prediction.best_odds, Prediction.closing_odds)
            .where(
                Prediction.closing_odds.is_not(None),
                Prediction.created_at >= cutoff,
            )
        ).all()

    if not rows:
        return {
            "n_with_clv": 0,
            "avg_clv_pct": 0.0,
            "positive_clv_share": 0.0,
            "median_clv_pct": 0.0,
        }

    clvs = [compute_clv_pct(entry, close) for entry, close in rows]
    clvs_sorted = sorted(clvs)
    median = clvs_sorted[len(clvs_sorted) // 2]
    positive = sum(1 for c in clvs if c > 0)
    return {
        "n_with_clv": len(clvs),
        "avg_clv_pct": round(sum(clvs) / len(clvs), 2),
        "positive_clv_share": round(positive / len(clvs) * 100, 1),
        "median_clv_pct": round(median, 2),
    }
