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


def _by_key(rows: list[tuple], index: int, label: str) -> list[dict]:
    """Flat-stake performance grouped on ONE column.

    League × market fragments into n=1..5 cells that say nothing. The two
    dimensions that actually carry signal in production are the market and the
    model type — grouping on one at a time keeps n large enough to read:
    on 2026-08-01 that is what separated totals (n=61, ROI -27.4%) and
    draw_no_bet (n=41, -18.7%) from double_chance (n=34, +2.7%) and h2h
    (n=14, +14.8%).

    Sorted WORST first: the loss centres are what the user needs to see.
    """
    buckets: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        key = r[index] if len(r) > index else None
        buckets[str(key or "?")].append(r)
    out = [{label: k, **_perf_stats(v)} for k, v in buckets.items()]
    out.sort(key=lambda d: (d["roi_pct"], -d["n"]))
    return out


def _calibration_buckets(rows: list[tuple]) -> list[dict]:
    """For each model_prob band, the ACTUAL win rate vs what the model claimed.

    `expected_win_rate` is the MEAN model_prob of the picks in the band, not the
    band's midpoint. The midpoint is an artifact of the bucket edges: picks
    clustered at 0.41 inside the 0.40-0.50 band were being scored against 45%,
    overstating the model's error at the bottom and understating it at the top.
    `gap` is the overconfidence in points — the single number that says whether
    the probabilities are honest. Pure.
    """
    out: list[dict] = []
    for lo, hi in _CALIB_BUCKETS:
        decided = [r for r in rows if r[4] in ("win", "loss") and lo <= float(r[2] or 0.0) < hi]
        if not decided:
            continue
        wins = sum(1 for r in decided if r[4] == "win")
        actual = wins / len(decided) * 100
        claimed = sum(float(r[2] or 0.0) for r in decided) / len(decided) * 100
        out.append({
            "bucket": f"{lo:.2f}-{hi if hi <= 1 else 1.0:.2f}",
            "n": len(decided),
            "actual_win_rate": round(actual, 1),
            "expected_win_rate": round(claimed, 1),
            "gap": round(claimed - actual, 1),
        })
    return out


def _brier(rows: list[tuple]) -> dict:
    """Brier score of the model against the Brier score of the raw bookmaker
    price (margin included).

    This is the harshest test there is: if the model cannot beat `1/best_odds`
    — a number that still contains the bookmaker's margin — then it adds no
    information over simply reading the odds. Production 2026-08-01: model
    0.2395 vs market 0.2303, i.e. the model lost that test. Lower is better.
    """
    decided = [r for r in rows if r[4] in ("win", "loss") and r[3]]
    if not decided:
        return {"n": 0, "model": None, "market": None, "model_beats_market": None}
    n = len(decided)
    outcome = [1.0 if r[4] == "win" else 0.0 for r in decided]
    model = sum((float(r[2] or 0.0) - o) ** 2 for r, o in zip(decided, outcome)) / n
    market = sum((1.0 / float(r[3]) - o) ** 2 for r, o in zip(decided, outcome)) / n
    return {
        "n": n,
        "model": round(model, 4),
        "market": round(market, 4),
        "model_beats_market": bool(model < market),
    }


def model_performance(days: int = 90, only_placed: bool = False) -> dict:
    """"Would-have" model performance over the last N days, overall + by segment
    + calibration. only_placed=False (default) = ALL historized picks (the model's
    track record); True = only bets the user confirmed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with session_scope() as s:
        # model_type and selection are appended AFTER the historical 5 columns so
        # the (sport_key, market, model_prob, best_odds, result) tuple layout the
        # pure helpers and their tests rely on stays valid.
        stmt = select(
            Prediction.sport_key, Prediction.market, Prediction.model_prob,
            Prediction.best_odds, Prediction.result,
            Prediction.model_type, Prediction.selection,
        ).where(
            Prediction.result.is_not(None),
            Prediction.created_at >= cutoff,
        )
        if only_placed:
            stmt = stmt.where(Prediction.placement_status == "confirmed")
        rows = s.execute(stmt).all()

        # Notation coverage — a ROI computed on 15% of production is not a track
        # record. Surfacing this next to the ROI is what stops the number from
        # being read as more than it is.
        #
        # The denominator MUST carry the same `created_at >= cutoff` filter as
        # the graded rows, otherwise a 30-day window is compared against the
        # whole table and the ratio is meaningless (observed: 21/297 = 7.1%
        # where the real figure for that window was far higher).
        total_stmt = select(Prediction.id).where(Prediction.created_at >= cutoff)
        if only_placed:
            total_stmt = total_stmt.where(Prediction.placement_status == "confirmed")
        n_total = len(s.execute(total_stmt).all())

    return {
        "days": days,
        "only_placed": only_placed,
        "overall": _perf_stats(rows),
        "segments": _group_performance(rows),
        "by_market": _by_key(rows, 1, "market"),
        "by_model": _by_key(rows, 5, "model_type"),
        "by_selection": _by_key(rows, 6, "selection"),
        "calibration": _calibration_buckets(rows),
        "brier": _brier(rows),
        "coverage": {
            "graded": len(rows),
            "total": n_total,
            "pct": round(len(rows) / n_total * 100, 1) if n_total else 0.0,
        },
    }
