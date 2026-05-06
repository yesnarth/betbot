"""
ML probability calibration — learns a correction map from MODEL probabilities
to OBSERVED win rates, using the predictions table once enough are resolved.

Design choice: **Isotonic Regression** (Niculescu-Mizil & Caruana, 2005).
  - Non-parametric: makes no assumption about the shape of the correction
  - Monotone: a higher model_prob always maps to a higher calibrated_prob
  - Robust on ~100-1000 samples, which is the realistic range for a
    personal bot in the first 6 months
  - Proven to outperform Platt scaling on long-tail betting data

Workflow:
  1. Worker calls `train_calibrator()` weekly (or on demand)
  2. The fitted model is persisted to `data/calibrator.joblib`
  3. At scan time, `calibrate(p)` adjusts the raw model probability before
     edge computation. If the calibrator is missing or stale (< MIN_SAMPLES
     resolved bets), `calibrate(p) == p` (no-op)

This module DEGRADES gracefully:
  - sklearn missing → calibrator returns identity
  - file missing → identity
  - too few resolved bets → identity (forced)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from betbot.database import session_scope
from betbot.orm_models import Prediction

logger = logging.getLogger("betbot.ml")

# How many resolved bets we want before we trust the calibrator. Below this
# the isotonic fit is high-variance — better to ship the raw probability.
MIN_SAMPLES_TO_TRUST = 50

CALIBRATOR_PATH = Path(os.getenv("CALIBRATOR_PATH", "data/calibrator.joblib"))


# ---------------------------------------------------------------------------
# Train
# ---------------------------------------------------------------------------

def _collect_training_data() -> list[tuple[float, int]]:
    """
    Pull (model_prob, won_or_lost) pairs from resolved predictions.
    Filters out 'void' results (push) — they don't tell us whether the
    model was right or wrong about the outcome.
    """
    with session_scope() as s:
        rows = s.execute(
            select(Prediction.model_prob, Prediction.result)
            .where(
                Prediction.result.is_not(None),
                Prediction.result.in_(("win", "loss")),
            )
        ).all()
    return [(float(p), 1 if r == "win" else 0) for p, r in rows]


def train_calibrator(min_samples: int = MIN_SAMPLES_TO_TRUST) -> dict:
    """
    Fit an Isotonic Regression on resolved predictions and persist it.

    Returns a status dict with:
      - n_samples         : how many resolved predictions were used
      - trained           : True if calibrator was fitted and saved
      - reason            : explanation when training was skipped
      - brier_before/after: improvement on the training set (information only;
                            not a true validation score)
    """
    samples = _collect_training_data()
    if len(samples) < min_samples:
        return {
            "trained": False,
            "n_samples": len(samples),
            "reason": f"need at least {min_samples} resolved bets, have {len(samples)}",
        }

    try:
        from sklearn.isotonic import IsotonicRegression
        import joblib
    except ImportError as exc:
        return {"trained": False, "n_samples": len(samples), "reason": f"sklearn unavailable: {exc}"}

    probs = [s[0] for s in samples]
    outcomes = [s[1] for s in samples]

    # Brier on raw probabilities (before)
    brier_before = sum((p - y) ** 2 for p, y in samples) / len(samples)

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(probs, outcomes)

    # Brier on calibrated probabilities (training set — overfit-biased,
    # for information only; real validation would use cross-validation)
    calibrated = iso.predict(probs)
    brier_after = sum((c - y) ** 2 for c, y in zip(calibrated, outcomes)) / len(samples)

    CALIBRATOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"model": iso, "trained_at": datetime.now(timezone.utc).isoformat(),
         "n_samples": len(samples)},
        CALIBRATOR_PATH,
    )
    logger.info(
        "Calibrator trained on %d samples, Brier %.4f → %.4f, saved to %s",
        len(samples), brier_before, brier_after, CALIBRATOR_PATH,
    )
    return {
        "trained": True,
        "n_samples": len(samples),
        "path": str(CALIBRATOR_PATH),
        "brier_before": round(brier_before, 4),
        "brier_after": round(brier_after, 4),
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

_cached_calibrator = None  # tuple[IsotonicRegression, str] | None


def _load_calibrator():
    """Lazily load and cache the persisted calibrator. Returns None if absent."""
    global _cached_calibrator
    if _cached_calibrator is not None:
        return _cached_calibrator
    if not CALIBRATOR_PATH.exists():
        return None
    try:
        import joblib
        payload = joblib.load(CALIBRATOR_PATH)
        _cached_calibrator = (payload["model"], payload.get("trained_at", "?"))
        return _cached_calibrator
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load calibrator: %s", exc)
        return None


def reset_cache() -> None:
    """Force the calibrator to be reloaded on next call (e.g. after retraining)."""
    global _cached_calibrator
    _cached_calibrator = None


def calibrate(prob: float) -> float:
    """
    Apply the persisted Isotonic calibration to a raw model probability.
    Returns `prob` unchanged when no calibrator is available.
    """
    cal = _load_calibrator()
    if cal is None:
        return prob
    iso, _ = cal
    try:
        return float(iso.predict([prob])[0])
    except Exception:  # noqa: BLE001
        return prob


def calibrator_status() -> dict:
    """Diagnostic: is the calibrator present, and from when."""
    cal = _load_calibrator()
    if cal is None:
        return {"available": False, "path": str(CALIBRATOR_PATH)}
    _, trained_at = cal
    return {
        "available": True,
        "path": str(CALIBRATOR_PATH),
        "trained_at": trained_at,
    }
