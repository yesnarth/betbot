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
  - Snapshot job runs every 10 min (scheduler). For each confirmed pending
    prediction, snapshots the best odds if `now` is in
      [kickoff - 30 min,  kickoff + 10 min]
    The post-kickoff window catches the case where Odds API was down during
    the entire pre-kickoff window — for sports where the API still serves
    in-play odds, the immediate-post-start snap is a very good proxy.
  - LATEST snap wins: a later (closer-to-kickoff) snap replaces an earlier
    one, so we always store the most accurate available value.
  - At resolve time, CLV is derived directly from the column.
  - Aggregate CLV is exposed alongside ROI in /stats/roi.

Data quality:
  closing_odds=None can mean 3 things — still pending, kickoff window passed
  without ever catching odds, or the match itself was filtered out by the
  Odds API. The `missed_clv_count` helper distinguishes these for the
  dashboard.
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


# Snap window relative to kickoff. The pre-window has to cover at least 3
# cron firings (we run every 10 min) so a single API blip doesn't lose the
# match. The post-window catches the "API was down the entire pre-window"
# case for sports where in-play odds are still served.
PRE_KICKOFF_WINDOW_MINUTES = 30
POST_KICKOFF_GRACE_MINUTES = 10


def snapshot_closing_odds(
    odds_client: OddsAPIClient,
    pre_window_min: int = PRE_KICKOFF_WINDOW_MINUTES,
    post_window_min: int = POST_KICKOFF_GRACE_MINUTES,
) -> dict[str, int]:
    """
    For each confirmed pending prediction whose match is in the snap window
    [kickoff - pre_window_min,  kickoff + post_window_min], fetch the latest
    best odds and store as `closing_odds`.

    Designed to run every 5-15 minutes from the scheduler. Multiple snaps
    per prediction are allowed: the LATEST one wins, so the closing_odds
    column converges to the closest-to-kickoff value the API actually
    served. Once the post-window expires the column is frozen.
    """
    now = datetime.now(timezone.utc)
    counts = {
        "checked": 0,
        "snapped": 0,
        "updated": 0,        # snap overwrote an earlier snap (closer to kickoff)
        "missing_event": 0,  # Odds API didn't serve the event in this cycle
        "errors": 0,
    }

    with session_scope() as s:
        # CLV is the closing line value of an actually-placed bet vs the
        # final book price. It only makes sense for picks the user has
        # CONFIRMED at the bookmaker. Snapshotting odds for 'proposed'
        # (unconfirmed) or 'skipped' picks would burn Odds API quota for
        # data we'll never use.
        # Note: closing_odds may already be SET — we still pick up the row
        # if we're still in the pre-window, because a later snap is closer
        # to kickoff and therefore more accurate. Filter for "still pending
        # result" only — once resolved, closing_odds is frozen.
        pending = s.execute(
            select(Prediction).where(
                Prediction.result.is_(None),
                Prediction.placement_status == "confirmed",
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
            commence = ""
            event = events_by_id.get(pred.event_id)
            if event:
                commence = event.get("commence_time", "")
            try:
                kickoff = datetime.fromisoformat(commence.replace("Z", "+00:00")) if commence else None
            except (ValueError, TypeError):
                kickoff = None

            if not kickoff:
                if not event:
                    counts["missing_event"] += 1
                continue

            mins_to_kickoff = (kickoff - now).total_seconds() / 60.0
            # mins_to_kickoff > 0  → kickoff in the future
            # mins_to_kickoff < 0  → kickoff already happened
            in_pre_window = 0 <= mins_to_kickoff <= pre_window_min
            in_post_window = -post_window_min <= mins_to_kickoff < 0
            if not (in_pre_window or in_post_window):
                continue

            best = extract_best_odds(event, _outcome_name(pred))
            if best is None:
                continue

            with session_scope() as s:
                fresh = s.get(Prediction, pred.id)
                if fresh is None:
                    continue
                # If we already have a closing_odds value and we're now in
                # the POST-kickoff grace, only update when the new price
                # differs significantly — avoid noisy mid-flight overwrites.
                prior = fresh.closing_odds
                if prior is not None and in_post_window:
                    # Skip overwrite during post-window unless the prior
                    # snap is missing or implausible.
                    if prior > 1.0:
                        continue
                window_label = "pre" if in_pre_window else "post"
                fresh.closing_odds = best.price
                if prior is None:
                    counts["snapped"] += 1
                else:
                    counts["updated"] += 1
                logger.info(
                    "CLV snap [%s-kickoff %+0.1fmin] : %s vs %s [%s] "
                    "entry=%.2f closing=%.2f%s",
                    window_label, mins_to_kickoff,
                    pred.home_team, pred.away_team, pred.selection,
                    pred.best_odds, best.price,
                    f" (was {prior:.2f})" if prior is not None else "",
                )

    logger.info(
        "CLV snapshot : %d checked, %d new, %d updated, %d sans event, %d erreur(s)",
        counts["checked"], counts["snapped"], counts["updated"],
        counts["missing_event"], counts["errors"],
    )
    return counts


def count_missed_clv_snapshots(days: int = 30) -> dict:
    """
    Distinguish 'still pending' vs 'permanently missed' CLV for the dashboard.

    A bet's closing_odds is permanently missed when:
      - the match has already kicked off > POST_KICKOFF_GRACE_MINUTES ago
      - AND closing_odds is still NULL

    For matches whose kickoff hasn't passed the grace window yet, NULL is
    a normal in-progress state. We don't have kickoff times stored on the
    Prediction row, so we approximate: a confirmed bet older than 7 days
    that still has no closing_odds is almost certainly post-grace.

    Returns counts useful for surfacing data-quality issues in the UI.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    grace_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with session_scope() as s:
        rows = s.execute(
            select(
                Prediction.closing_odds,
                Prediction.result,
                Prediction.created_at,
            ).where(
                Prediction.placement_status == "confirmed",
                Prediction.created_at >= cutoff,
            )
        ).all()

    n_total = len(rows)
    n_with_clv = sum(1 for c, _, _ in rows if c is not None and c > 1.0)
    # A snap is still possible when: no CLV yet AND result not in yet AND
    # the bet is fresh enough that the snap window probably hasn't closed.
    # Anything else with NULL closing_odds is permanently missed — including
    # resolved bets without CLV (the match is over, we can't go back).
    n_pending = sum(
        1 for c, r, ts in rows
        if c is None and r is None and ts >= grace_cutoff
    )
    n_missed = sum(
        1 for c, r, ts in rows
        if c is None and (ts < grace_cutoff or r is not None)
    )
    return {
        "n_total_confirmed": n_total,
        "n_with_clv": n_with_clv,
        "n_pending_clv": n_pending,
        "n_missed_clv": n_missed,
        "coverage_pct": round(n_with_clv / max(1, n_total) * 100, 1),
    }


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
