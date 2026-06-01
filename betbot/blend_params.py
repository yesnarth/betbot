"""Persisted per-league blend weights (elo_weight, xg_weight), fit by betbot.tuning.

Stored as JSON at data/blend_params.json :
    {sport_key: {elo_weight, xg_weight, log_loss, tuned_at}}

Purely additive / opt-in : if the file is absent, or a league has no entry, or it
fails to load, callers fall back to the hardcoded defaults in models.py — so
behaviour is unchanged until the optimizer runs and finds something better.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("betbot.blend_params")

BLEND_PARAMS_PATH = Path(os.getenv("BLEND_PARAMS_PATH", "data/blend_params.json"))

_cache: dict | None = None
_cache_mtime: float | None = None


def _load() -> dict:
    """Load + cache the params, re-reading automatically when the file's mtime
    changes — so the API process picks up a re-tune written by the worker
    process (shared volume) without needing a restart."""
    global _cache, _cache_mtime
    if not BLEND_PARAMS_PATH.exists():
        _cache, _cache_mtime = {}, None
        return _cache
    try:
        mtime = BLEND_PARAMS_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _cache is not None and _cache_mtime == mtime:
        return _cache
    try:
        loaded = json.loads(BLEND_PARAMS_PATH.read_text(encoding="utf-8"))
        _cache = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load blend params: %s", exc)
        _cache = {}
    _cache_mtime = mtime
    return _cache


def reset_cache() -> None:
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = None


def get_weights(sport_key: str | None) -> tuple[float, float] | None:
    """Return (elo_weight, xg_weight) for the league, or None if not tuned.
    Defensive: a malformed row or out-of-range weights return None (→ defaults)."""
    if not sport_key:
        return None
    row = _load().get(sport_key)
    if not isinstance(row, dict):
        return None
    try:
        ew = float(row["elo_weight"])
        xw = float(row["xg_weight"])
    except (KeyError, TypeError, ValueError):
        return None
    # Honour the same invariant blended_match_probs enforces (leave room for DC).
    if ew < 0 or xw < 0 or (ew + xw) > 0.95:
        return None
    return ew, xw


def save_weights(sport_key: str, elo_weight: float, xg_weight: float,
                 log_loss: float, tuned_at: str) -> None:
    data = dict(_load())
    data[sport_key] = {
        "elo_weight": round(float(elo_weight), 3),
        "xg_weight": round(float(xg_weight), 3),
        "log_loss": round(float(log_loss), 4),
        "tuned_at": tuned_at,
    }
    BLEND_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLEND_PARAMS_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    reset_cache()


def status() -> dict:
    d = _load()
    return {"available": bool(d), "path": str(BLEND_PARAMS_PATH),
            "leagues": sorted(d.keys())}
