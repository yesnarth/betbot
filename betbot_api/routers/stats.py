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


@router.get("/model-performance")
def model_performance(
    days: int = Query(default=90, ge=1, le=365),
    only_placed: bool = Query(default=False),
    _: str = Depends(require_auth),
) -> dict:
    """"Would-have" model performance on ALL historized picks (proposed +
    confirmed + skipped) at a flat 1u stake — the model's track record, not the
    bankroll. Returns overall + per-segment (league × market) ROI/win-rate +
    calibration buckets (does a model_prob of X actually win ~X%?). Set
    only_placed=true to restrict to bets the user confirmed. Wider default
    window (90 d) since per-segment samples are small."""
    from betbot.perf import model_performance as _mp
    return _mp(days=days, only_placed=only_placed)


@router.get("/coverage")
def coverage(
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Team-stats coverage — how many teams / leagues have modelled stats and
    ELO. Low coverage during the European off-season is why in-season leagues
    fall back to the consensus model; the /stats/refresh-inseason endpoint fills
    that gap from api-football."""
    return db.team_stats_coverage()


@router.post("/refresh-inseason")
@limiter.limit("2/minute")  # each league = 1-2 api-football calls; keep it gentle
def refresh_inseason(
    request: Request,
    body: dict | None = None,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Populate team_stats for the currently in-season leagues (Scandinavia,
    Brazil, MLS, Asia…) from api-football, so the blended Dixon-Coles model runs
    on them instead of the market-consensus fallback.

    Body (all optional):
      only_keys:   list[str] of sport_keys to restrict to (default = all mapped)
      min_matches: skip leagues thinner than this (default 30)

    Requires API_FOOTBALL_KEY. Synchronous — typically 20-60 s. Idempotent."""
    from betbot.stats_inseason import refresh_inseason_stats
    body = body or {}
    only = body.get("only_keys") or None
    min_matches = int(body.get("min_matches", 30))
    return refresh_inseason_stats(db, only_keys=only, min_matches=min_matches)


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
