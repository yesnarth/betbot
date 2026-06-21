"""
Model performance tracker — "would-have" ROI / win-rate / calibration on ALL
historized picks (proposed + confirmed + skipped), not just bankroll-confirmed
bets. This is how we MEASURE the predictions over time, by segment.

Distinct from get_roi_stats (bankroll, only_placed=True) and from CLV (clv.py):
  - here every resolved pick counts at a FLAT 1u stake, so segments are
    comparable regardless of Kelly sizing;
  - calibration buckets show whether a model_prob of X actually wins ~X% — the
    direct test of whether the probabilities are honest.

All aggregation is pure (rows → dicts) so it's trivially unit-testable; only
``model_performance`` touches the DB.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from betbot.database import session_scope
from betbot.orm_models import Prediction

# model_prob buckets for the calibration view (lo ≤ p < hi).
_CALIB_BUCKETS = [(0.0, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.01)]


def _flat_profit(odds: float, result: str) -> float:
    """Profit of a flat 1u stake: win → odds−1, loss → −1, void/push → 0."""
    if result == "win":
        return float(odds) - 1.0
    if result == "loss":
        return -1.0
    return 0.0


def _perf_stats(rows: list[tuple]) -> dict:
    """Aggregate (sport_key, market, model_prob, best_odds, result) rows into a
    flat-stake performance summary. Pure."""
    n = len(rows)
    if not n:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "roi_pct": 0.0,
                "avg_model_prob": 0.0, "avg_implied_prob": 0.0}
    wins = sum(1 for r in rows if r[4] == "win")
    losses = sum(1 for r in rows if r[4] == "loss")
    decided = wins + losses
    profit = sum(_flat_profit(r[3], r[4]) for r in rows)
    avg_p = sum(float(r[2] or 0.0) for r in rows) / n
    avg_imp = sum((1.0 / float(r[3])) for r in rows if r[3]) / n
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
        "roi_pct": round(profit / n * 100, 1),
        "avg_model_prob": round(avg_p, 3),
        "avg_implied_prob": round(avg_imp, 3),
    }


def _group_performance(rows: list[tuple]) -> list[dict]:
    """Per-segment (league × market) flat-stake performance. Pure. Sorted best
    ROI first — a positive ROI means that segment's picks actually made money."""
    buckets: dict[tuple, list[tuple]] = defaultdict(list)
    for r in rows:
        buckets[(r[0] or "?", r[1] or "?")].append(r)
    out: list[dict] = []
    for (sport_key, market), seg_rows in buckets.items():
        out.append({"sport_key": sport_key, "market": market, **_perf_stats(seg_rows)})
    out.sort(key=lambda d: (d["roi_pct"], d["n"]), reverse=True)
    return out


def _calibration_buckets(rows: list[tuple]) -> list[dict]:
    """For each model_prob band, the ACTUAL win rate vs what the model implied.
    A well-calibrated model wins ≈ the band's midpoint. Pure."""
    out: list[dict] = []
    for lo, hi in _CALIB_BUCKETS:
        decided = [r for r in rows if r[4] in ("win", "loss") and lo <= float(r[2] or 0.0) < hi]
        if not decided:
            continue
        wins = sum(1 for r in decided if r[4] == "win")
        out.append({
            "bucket": f"{lo:.2f}-{hi if hi <= 1 else 1.0:.2f}",
            "n": len(decided),
            "actual_win_rate": round(wins / len(decided) * 100, 1),
            "expected_win_rate": round((lo + min(hi, 1.0)) / 2 * 100, 1),
        })
    return out


def model_performance(days: int = 90, only_placed: bool = False) -> dict:
    """"Would-have" model performance over the last N days, overall + by segment
    + calibration. only_placed=False (default) = ALL historized picks (the model's
    track record); True = only bets the user confirmed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with session_scope() as s:
        stmt = select(
            Prediction.sport_key, Prediction.market, Prediction.model_prob,
            Prediction.best_odds, Prediction.result,
        ).where(
            Prediction.result.is_not(None),
            Prediction.created_at >= cutoff,
        )
        if only_placed:
            stmt = stmt.where(Prediction.placement_status == "confirmed")
        rows = s.execute(stmt).all()

    return {
        "days": days,
        "only_placed": only_placed,
        "overall": _perf_stats(rows),
        "segments": _group_performance(rows),
        "calibration": _calibration_buckets(rows),
    }
