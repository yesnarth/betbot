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
