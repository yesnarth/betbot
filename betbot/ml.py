"""ML probability calibration — learns a correction map from MODEL probabilities
to OBSERVED win rates, using the predictions table once enough are resolved.

Design choice: **Isotonic Regression** (Niculescu-Mizil & Caruana, 2005).
  - Non-parametric: makes no assumption about the shape of the correction
  - Monotone: a higher model_prob always maps to a higher calibrated_prob
  - Robust on ~100-1000 samples, the realistic range for a personal bot

**Per-segment calibration (v2):** a single calibrator trained mostly on football
h2h does NOT transfer to tennis ELO, basketball pace, or Over/Under totals — those
have different miscalibration shapes. So we fit ONE map per *segment*
(football_h2h, football_totals, tennis, basketball) plus a *global* map. At scan
time `calibrate(p, segment)` uses the segment map when available, else the global
map, else identity. Old single-map files (v1) are still read as the global map.

This module DEGRADES gracefully:
  - sklearn missing → identity
  - file missing / unparseable → identity
  - segment + global both below MIN_SAMPLES → identity for that segment
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

# How many resolved bets we want before we trust a calibrator (per segment, and
# for the global map). Below this the isotonic fit is high-variance.
MIN_SAMPLES_TO_TRUST = 50

# Beyond this age (days) the calibrator is considered stale — model drift over a
# season means an old fit can quietly mis-calibrate. The dashboard surfaces this.
STALE_AFTER_DAYS = 30

CALIBRATOR_PATH = Path(os.getenv("CALIBRATOR_PATH", "data/calibrator.json"))


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

def segment_for(sport_key: str | None, market: str | None) -> str:
    """Map a (sport_key, market) to a calibration segment.

    Tennis and basketball use fundamentally different models from football, and
    Over/Under totals miscalibrate differently from 1X2 — so each gets its own
    isotonic map.
    """
    sk = sport_key or ""
    if sk.startswith("tennis_"):
        return "tennis"
    if sk.startswith("basketball_"):
        return "basketball"
    if market == "totals":
        return "football_totals"
    return "football_h2h"


# ---------------------------------------------------------------------------
# Training-data collection
# ---------------------------------------------------------------------------

def _collect_training_data() -> list[tuple[float, int]]:
    """Pull (model_prob, won_or_lost) pairs from resolved predictions (all
    segments pooled). Used for the resolved-bet COUNT and the global fit.
    'void' (push) rows are excluded — they don't tell us if the model was right.
    """
    with session_scope() as s:
        rows = s.execute(
            select(Prediction.model_prob, Prediction.result).where(
                Prediction.result.is_not(None),
                Prediction.result.in_(("win", "loss")),
            )
        ).all()
    return [(float(p), 1 if r == "win" else 0) for p, r in rows]


def _collect_segmented_training_data() -> list[tuple[float, int, str]]:
    """Like _collect_training_data but tagged with the segment (sport/market)."""
    with session_scope() as s:
        rows = s.execute(
            select(
                Prediction.model_prob, Prediction.result,
                Prediction.sport_key, Prediction.market,
            ).where(
                Prediction.result.is_not(None),
                Prediction.result.in_(("win", "loss")),
            )
        ).all()
    return [
        (float(p), 1 if r == "win" else 0, segment_for(sk, mk))
        for p, r, sk, mk in rows
    ]


# ---------------------------------------------------------------------------
# Isotonic fit helpers
# ---------------------------------------------------------------------------

def _fit_isotonic(samples: list[tuple[float, int]]) -> tuple[list[float], list[float]] | None:
    """Fit an IsotonicRegression on (prob, won) pairs; return (x_thresholds,
    y_thresholds) as plain lists (JSON-safe — no pickle/RCE), or None on failure.
    """
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        return None
    if not samples:
        return None
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit([p for p, _ in samples], [y for _, y in samples])
    return iso.X_thresholds_.tolist(), iso.y_thresholds_.tolist()


def _brier(samples: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in samples) / len(samples) if samples else 0.0


def _brier_after(samples: list[tuple[float, int]], xy: tuple[list[float], list[float]]) -> float:
    import numpy as np
    x_t, y_t = xy
    return sum(
        (float(np.interp(p, x_t, y_t)) - y) ** 2 for p, y in samples
    ) / len(samples) if samples else 0.0


def _build_payload(
    all_samples: list[tuple[float, int]],
    by_segment: dict[str, list[tuple[float, int]]],
    min_samples: int,
    source: str,
    extra: dict | None = None,
) -> dict | None:
    """Fit the global map + one map per segment that has enough samples.
    Returns the JSON payload, or None if even the global fit is impossible.
    """
    g_xy = _fit_isotonic(all_samples)
    if g_xy is None:
        return None
    segments: dict[str, dict] = {}
    seg_counts: dict[str, int] = {}
    for seg, samples in by_segment.items():
        seg_counts[seg] = len(samples)
        if len(samples) >= min_samples:
            xy = _fit_isotonic(samples)
            if xy is not None:
                segments[seg] = {"x": xy[0], "y": xy[1], "n": len(samples)}
    payload = {
        "format": "isotonic-segmented-v1",
        "global": {"x": g_xy[0], "y": g_xy[1], "n": len(all_samples)},
        "segments": segments,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "n_samples": len(all_samples),
        "brier_before": round(_brier(all_samples), 4),
        "brier_after": round(_brier_after(all_samples, g_xy), 4),
        "segment_counts": seg_counts,
    }
    if extra:
        payload.update(extra)
    return payload


def _persist(payload: dict) -> None:
    CALIBRATOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATOR_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    reset_cache()


# ---------------------------------------------------------------------------
# Train (on real resolved bets)
# ---------------------------------------------------------------------------

def train_calibrator(min_samples: int = MIN_SAMPLES_TO_TRUST) -> dict:
    """Fit isotonic maps (global + per-segment) on resolved predictions and
    persist them as JSON. Returns a status dict (trained, n_samples, brier_*,
    segments, ...)."""
    rows = _collect_segmented_training_data()
    if len(rows) < min_samples:
        return {
            "trained": False,
            "n_samples": len(rows),
            "reason": f"need at least {min_samples} resolved bets, have {len(rows)}",
        }

    all_samples = [(p, w) for p, w, _ in rows]
    by_segment: dict[str, list[tuple[float, int]]] = {}
    for p, w, seg in rows:
        by_segment.setdefault(seg, []).append((p, w))

    payload = _build_payload(all_samples, by_segment, min_samples, source="resolved_bets")
    if payload is None:
        return {"trained": False, "n_samples": len(all_samples),
                "reason": "sklearn unavailable"}
    _persist(payload)
    logger.info(
        "Calibrator trained on %d samples (global) ; segments=%s ; Brier %.4f → %.4f",
        len(all_samples), list(payload["segments"]),
        payload["brier_before"], payload["brier_after"],
    )
    return {
        "trained": True,
        "n_samples": len(all_samples),
        "path": str(CALIBRATOR_PATH),
        "brier_before": payload["brier_before"],
        "brier_after": payload["brier_after"],
        "segments": list(payload["segments"]),
        "segment_counts": payload["segment_counts"],
    }


# ---------------------------------------------------------------------------
# Cold-start training
# ---------------------------------------------------------------------------

DEFAULT_COLD_START_LEAGUES = (
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue1",
)


def cold_start_train(
    fd_api_key: str,
    sport_keys: tuple[str, ...] = DEFAULT_COLD_START_LEAGUES,
    n_per_sport: int = 150,
) -> dict:
    """Bootstrap the calibrator from walk-forward backtests on historical
    matches (synthetic but realistic (model_prob, won) pairs). All cold-start
    samples are football h2h, so they fill both the global map and the
    `football_h2h` segment. The weekly train_calibrator() later overwrites this
    with real per-segment fits.
    """
    from betbot.backtest import run_backtest

    all_samples: list[tuple[float, int]] = []
    per_league: dict[str, int] = {}
    notes: list[str] = []

    for sport in sport_keys:
        try:
            r = run_backtest(sport, fd_api_key, n_holdout=n_per_sport, use_enrichment=False)
        except Exception as exc:
            notes.append(f"{sport}: error {exc}")
            per_league[sport] = 0
            continue
        if r.n_matches == 0:
            notes.append(f"{sport}: skipped ({r.notes})")
            per_league[sport] = 0
            continue
        all_samples.extend(r.samples)
        per_league[sport] = len(r.samples)
        notes.append(f"{sport}: {r.n_matches} matchs → {len(r.samples)} samples")

    if len(all_samples) < MIN_SAMPLES_TO_TRUST:
        return {
            "trained": False,
            "n_samples": len(all_samples),
            "per_league": per_league,
            "reason": f"only {len(all_samples)} samples after backtests across "
                      f"{len(sport_keys)} leagues — need {MIN_SAMPLES_TO_TRUST}",
            "notes": notes,
        }

    # All cold-start samples are football h2h → seed both global and that segment.
    payload = _build_payload(
        all_samples,
        {"football_h2h": all_samples},
        MIN_SAMPLES_TO_TRUST,
        source="cold_start_backtest",
        extra={"per_league": per_league},
    )
    if payload is None:
        return {"trained": False, "n_samples": len(all_samples),
                "per_league": per_league, "reason": "sklearn unavailable"}
    _persist(payload)
    logger.info(
        "Cold-start calibrator on %d samples across %d leagues, Brier %.4f → %.4f",
        len(all_samples), len(sport_keys), payload["brier_before"], payload["brier_after"],
    )
    return {
        "trained": True,
        "n_samples": len(all_samples),
        "per_league": per_league,
        "path": str(CALIBRATOR_PATH),
        "brier_before": payload["brier_before"],
        "brier_after": payload["brier_after"],
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

# Cached as {"global": (x, y) | None, "segments": {seg: (x, y)},
#            "trained_at": str, "source": str}
_cached_calibrator: dict | None = None


def _load_calibrator() -> dict | None:
    """Load and cache the persisted calibrator. Handles both the segmented v2
    format and the legacy single-map v1 format (treated as the global map).
    Returns None when no usable map is present."""
    global _cached_calibrator
    if _cached_calibrator is not None:
        return _cached_calibrator
    if not CALIBRATOR_PATH.exists():
        return None
    try:
        payload = json.loads(CALIBRATOR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load calibrator: %s", exc)
        return None

    fmt = payload.get("format")
    glob: tuple[list, list] | None = None
    segments: dict[str, tuple[list, list]] = {}
    try:
        if fmt == "isotonic-segmented-v1":
            g = payload.get("global")
            if g and g.get("x") and g.get("y"):
                glob = (list(g["x"]), list(g["y"]))
            for seg, m in (payload.get("segments") or {}).items():
                if m.get("x") and m.get("y"):
                    segments[seg] = (list(m["x"]), list(m["y"]))
        elif fmt == "isotonic-thresholds-v1":  # legacy single map → global
            if payload.get("x_thresholds") and payload.get("y_thresholds"):
                glob = (list(payload["x_thresholds"]), list(payload["y_thresholds"]))
        else:
            logger.warning("Calibrator file format mismatch: %s", fmt)
            return None
    except (KeyError, TypeError) as exc:
        logger.warning("Could not parse calibrator maps: %s", exc)
        return None

    if glob is None and not segments:
        return None
    _cached_calibrator = {
        "global": glob,
        "segments": segments,
        "trained_at": payload.get("trained_at", "?"),
        "source": payload.get("source", "resolved_bets"),
    }
    return _cached_calibrator


def reset_cache() -> None:
    """Force the calibrator to be reloaded on next call (e.g. after retraining)."""
    global _cached_calibrator
    _cached_calibrator = None


def calibrate(prob: float, segment: str | None = None) -> float:
    """Apply the persisted isotonic calibration to a raw model probability.

    Uses the `segment` map when available, else the global map, else returns
    `prob` unchanged. Pure JSON + numpy interpolation — no pickle deserialization.
    """
    cal = _load_calibrator()
    if cal is None:
        return prob
    xy = None
    if segment and segment in cal["segments"]:
        xy = cal["segments"][segment]
    elif cal["global"] is not None:
        xy = cal["global"]
    if not xy or not xy[0] or not xy[1]:
        return prob
    try:
        import numpy as np
        return max(0.0, min(float(np.interp(prob, xy[0], xy[1])), 1.0))
    except (ValueError, TypeError):
        return prob


def calibrator_status() -> dict:
    """Diagnostic: presence, age/staleness, source, and which segments are fitted."""
    cal = _load_calibrator()
    if cal is None:
        return {"available": False, "path": str(CALIBRATOR_PATH)}
    out = {
        "available": True,
        "path": str(CALIBRATOR_PATH),
        "trained_at": cal["trained_at"],
        "source": cal["source"],  # "resolved_bets" | "cold_start_backtest"
        "has_global": cal["global"] is not None,
        "segments": sorted(cal["segments"].keys()),
    }
    try:
        ts = datetime.fromisoformat(cal["trained_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts).days
        out["age_days"] = age_days
        out["stale"] = age_days > STALE_AFTER_DAYS
    except (ValueError, TypeError):
        out["age_days"] = None
        out["stale"] = False
    return out
