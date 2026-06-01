"""ML probability calibrator endpoints — Isotonic regression on resolved bets."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from betbot.config import load_settings
from betbot_api.auth import require_auth
from betbot_api.deps import limiter

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/calibrator/status")
def ml_calibrator_status(_: str = Depends(require_auth)) -> dict:
    """
    Show whether a calibrator is fitted and ready, plus how many resolved bets
    are available for the next training run.
    """
    from betbot.ml import calibrator_status, _collect_training_data, MIN_SAMPLES_TO_TRUST
    status = calibrator_status()
    samples = _collect_training_data()
    n_resolved = len(samples)
    return {
        **status,
        "n_resolved_bets": n_resolved,
        "min_samples_to_trust": MIN_SAMPLES_TO_TRUST,
        "ready_to_train": n_resolved >= MIN_SAMPLES_TO_TRUST,
    }


@router.post("/calibrator/train")
@limiter.limit("5/minute")
def ml_calibrator_train(
    request: Request,
    _: str = Depends(require_auth),
) -> dict:
    """Force a retrain of the calibrator on whatever resolved bets are available."""
    from betbot.ml import train_calibrator, reset_cache
    result = train_calibrator()
    reset_cache()  # so the next scan picks up the new model
    return result


@router.post("/calibrator/cold-start")
@limiter.limit("2/minute")  # protects football-data.org quota
def ml_calibrator_cold_start(
    request: Request,
    _: str = Depends(require_auth),
) -> dict:
    """
    Bootstrap the calibrator from historical backtests on 5 major leagues.

    Use this on fresh installs to skip the 50-bet warm-up: the calibrator
    is fitted on synthetic (model_prob, won) pairs drawn from walk-forward
    backtests instead of real placed bets. Once enough real bets accumulate,
    the regular `train_calibrator()` job overwrites this cold-start fit.

    Slow (~30-60 s) because it runs 5 backtests sequentially. Rate-limited
    to 2/min.
    """
    from betbot.ml import cold_start_train
    s = load_settings()
    return cold_start_train(s.football_data_api_key)


@router.post("/blend/tune")
@limiter.limit("2/minute")  # runs a walk-forward backtest per call (~10-30 s)
def ml_tune_blend(
    request: Request,
    sport_key: str,
    n_holdout: int = 200,
    _: str = Depends(require_auth),
) -> dict:
    """
    Grid-search the per-league blend weights (elo_weight / xg_weight) on a
    walk-forward backtest to MINIMIZE log-loss, replacing the hardcoded guesses.
    Persists the result ONLY when it beats today's defaults — so a tune can never
    make a league's predictions worse. See betbot.tuning for the look-ahead caveat.
    """
    from datetime import datetime, timezone

    from betbot import blend_params
    from betbot.tuning import optimize_blend

    s = load_settings()
    res = optimize_blend(sport_key, s.football_data_api_key, n_holdout=n_holdout)
    if res.get("tuned"):
        blend_params.save_weights(
            sport_key, res["elo_weight"], res["xg_weight"],
            res["log_loss_after"], datetime.now(timezone.utc).isoformat(),
        )
    return res


@router.get("/blend/status")
def ml_blend_status(_: str = Depends(require_auth)) -> dict:
    """Which leagues have data-fit blend weights (vs the hardcoded defaults)."""
    from betbot import blend_params
    return blend_params.status()


@router.post("/blend/tune-all")
@limiter.limit("1/minute")  # very heavy : a walk-forward backtest PER league
def ml_tune_all(
    request: Request,
    n_holdout: int = 200,
    _: str = Depends(require_auth),
) -> dict:
    """Tune blend weights for ALL football-data leagues in one pass, persisting
    each league whose fit beats its defaults (others are left on the defaults)."""
    from datetime import datetime, timezone

    from betbot import blend_params
    from betbot.tuning import tune_all_leagues

    s = load_settings()
    results = tune_all_leagues(s.football_data_api_key, n_holdout=n_holdout)
    now = datetime.now(timezone.utc).isoformat()
    saved = []
    for sk, res in results.items():
        if res.get("tuned"):
            blend_params.save_weights(sk, res["elo_weight"], res["xg_weight"],
                                      res["log_loss_after"], now)
            saved.append(sk)
    return {"results": results, "saved": saved, "n_saved": len(saved)}
