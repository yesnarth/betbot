"""Statistics endpoints — ROI, CLV coverage, A/B tests, backtest."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from betbot.config import load_settings
from betbot.db import Database
from betbot_api.auth import require_auth
from betbot_api.deps import get_db, limiter
from betbot_api.schemas import (
    BacktestCalibrationBucket,
    BacktestRequest,
    BacktestResponse,
    ROIStats,
)

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/roi", response_model=ROIStats)
def roi(
    days: int = Query(default=30, ge=1, le=365),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> ROIStats:
    return ROIStats(**db.get_roi_stats(days=days))


@router.get("/clv-coverage")
def clv_coverage(
    days: int = Query(default=30, ge=1, le=365),
    _: str = Depends(require_auth),
) -> dict:
    """
    Data-quality view on CLV: how many confirmed bets have a closing-odds
    snapshot, how many are still pending the snap window, and how many
    were permanently missed (kickoff window passed without a successful
    snapshot — usually means Odds API was down at the wrong moment).

    Surfaces the silent NaN holes the user can't otherwise see.
    """
    from betbot.clv import count_missed_clv_snapshots
    return count_missed_clv_snapshots(days=days)


@router.get("/clv-by-segment")
def clv_by_segment(
    days: int = Query(default=90, ge=1, le=365),
    _: str = Depends(require_auth),
) -> dict:
    """Per-segment (league × market) CLV — which leagues/markets actually beat the
    closing line. Positive avg = the model's edge there is real (favour it);
    persistently negative = deprioritise. The decision signal behind eventual
    auto-pruning. Wider default window (90 d) since per-segment samples are small."""
    from betbot.clv import aggregate_clv, aggregate_clv_by_segment
    return {
        "segments": aggregate_clv_by_segment(days=days),
        "overall": aggregate_clv(days=days),
    }


@router.post("/ab-test")
@limiter.limit("5/minute")
def ab_test(
    request: Request,
    body: dict,
    _: str = Depends(require_auth),
) -> dict:
    """
    Compare two rule variants on resolved historical predictions.

    Body:
      variant_a: {"name": str, ... knobs ...}
      variant_b: {"name": str, ... knobs ...}
      days:      lookback window (default 90)
      only_placed: only count bets the user actually played (default false)

    Knobs (each variant):
      market_shrink_soft, market_shrink_hard, market_shrink_max,
      overconfidence_cap, overconfidence_penalty,
      huge_edge_threshold, huge_edge_penalty
    """
    from betbot.ab_test import RuleVariant, compare_variants
    a = RuleVariant(**(body.get("variant_a") or {"name": "A"}))
    b = RuleVariant(**(body.get("variant_b") or {"name": "B"}))
    return compare_variants(
        a, b,
        days=int(body.get("days", 90)),
        only_placed=bool(body.get("only_placed", False)),
    )


@router.post("/backtest", response_model=BacktestResponse)
@limiter.limit("5/minute")
def backtest(
    request: Request,
    body: BacktestRequest,
    _: str = Depends(require_auth),
) -> BacktestResponse:
    """
    Walk-forward backtest on the most-recent matches of the given league.

    Returns Brier score, log-loss, and calibration buckets. Synchronous —
    typically 5-15 s depending on league size. Rate-limited to 5/min to
    protect the football-data.org quota.

    `use_enrichment=True` snapshots today's ELO/xG and applies them to
    historical predictions — gives an OPTIMISTIC upper bound but introduces
    look-ahead bias. Default OFF (strict walk-forward).
    """
    import time
    from betbot.backtest import run_backtest

    s = load_settings()
    t0 = time.monotonic()
    result = run_backtest(
        body.sport_key,
        s.football_data_api_key,
        n_holdout=body.n_holdout,
        use_enrichment=body.use_enrichment,
    )
    duration = round(time.monotonic() - t0, 2)
    return BacktestResponse(
        sport_key=result.sport_key,
        n_matches=result.n_matches,
        brier_score=result.brier_score,
        log_loss=result.log_loss,
        calibration=[BacktestCalibrationBucket(**b) for b in result.calibration],
        notes=result.notes,
        duration_seconds=duration,
        roi_pct=result.roi_pct,
        n_value_bets=result.n_value_bets,
        avg_ev_pct=result.avg_ev_pct,
    )
