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
# Isotonic regression is NON-PARAMETRIC: it fits a step function with as many
# steps as the data suggests, and on a small sample those steps land on 0.0 and
# 1.0 at the extremes. That is not a corner case here, it is the failure that
# already happened twice on this very dataset:
#
#   * fitted on 114 clean samples, the global map sends 0.80 -> 0.997 — the
#     certainty-fabrication that produced 59 picks at model_prob = 1.000;
#   * fitted on the polluted set, the football_h2h segment flattened EVERYTHING
#     above 0.496 to 0.529, which made the 0.70 confidence floor unreachable on
#     1X2 and silently reduced the bot to derived markets alone (h2h: 2 picks
#     against 38 draw-no-bet and 32 double-chance).
#
# 50 resolved bets cannot support this estimator. Several hundred can. Until
# then the honest behaviour is to pass the model's own probability through
# untouched — see `calibrate()` — rather than to correct it with noise.
MIN_SAMPLES_TO_TRUST = 300

# Derived markets (Double Chance / Draw No Bet) are computed FROM the already
# calibrated 1X2 — they are never calibrated themselves at scan time. Training a
# map on them would only pool a different probability distribution (0.7–0.9) into
# the football_h2h/global fit and bias it, so they're excluded from training.
_DERIVED_MARKETS = ("double_chance", "draw_no_bet")

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

# Probabilities this close to 0 or 1 are not model output, they are damage.
# The 05/08-10/08 calibration bug stamped model_prob = 1.000 on 59 picks; left
# in the training set they would teach the next calibrator that "certainty
# means a coin flip", and the corruption would outlive its own fix.
_DEGENERATE_EPS = 1e-3

# Hard bounds on anything the calibrator is allowed to output. A probability of
# 0 or 1 is a claim no statistical fit on a few hundred football matches can
# support, and it is the shape the group renormalisation turns into a fabricated
# certainty.
_CAL_OUTPUT_MIN = 0.02
_CAL_OUTPUT_MAX = 0.98


def _is_usable(prob: float) -> bool:
    return _DEGENERATE_EPS < float(prob) < 1.0 - _DEGENERATE_EPS


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
                ~Prediction.market.in_(_DERIVED_MARKETS),
                # Belt and braces: `_is_usable` already drops degenerate
                # probabilities, but a quarantined pick must never train the
                # model that produced it.
                Prediction.excluded_reason.is_(None),
            )
        ).all()
    return [(float(p), 1 if r == "win" else 0) for p, r in rows if _is_usable(p)]


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
                ~Prediction.market.in_(_DERIVED_MARKETS),
                Prediction.excluded_reason.is_(None),
            )
        ).all()
    return [
        (float(p), 1 if r == "win" else 0, segment_for(sk, mk))
        for p, r, sk, mk in rows if _is_usable(p)
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
# Cold-start on REAL closing odds (football-data.co.uk) — preferred
# ---------------------------------------------------------------------------

def _walk_forward_real_samples(matches: list[dict]) -> list[tuple[float, int]]:
    """Walk a league's matches chronologically; for each, predict from STRICTLY
    prior results, shrink each outcome toward its REAL closing line exactly as
    production does, and emit (shrunk_prob, won) samples for home/draw/away.

    The sample's probability is the PER-OUTCOME shrunk value (not renormalized) —
    that is precisely the domain production passes to calibrate() (analysis.py
    shrinks, then calibrates, then renormalizes), so the fitted map applies cleanly.
    """
    from betbot.models import (
        build_team_stats, compute_league_averages, blended_match_probs,
        DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG,
    )
    from betbot.calibration import shrink_toward_market

    samples: list[tuple[float, int]] = []
    last_date: str | None = None
    cache: dict = {}
    lh = la = 0.0
    for m in matches:
        date = m["date"]
        if date != last_date:
            train = [x for x in matches if x["date"] < date]   # no same-day leakage
            last_date = date
            if len(train) < 40:
                cache = {}
                continue
            lh, la = compute_league_averages(train)
            teams = {x["home_team"] for x in train} | {x["away_team"] for x in train}
            cache = {t: ts for t in teams if (ts := build_team_stats(t, train, lh, la))}
        if not cache:
            continue
        h, a = cache.get(m["home_team"]), cache.get(m["away_team"])
        if not h or not a:
            continue
        try:
            probs = blended_match_probs(
                home_stats=h, away_stats=a,
                league_home_avg=lh or DEFAULT_HOME_AVG,
                league_away_avg=la or DEFAULT_AWAY_AVG,
            )
        except Exception:
            continue
        hg, ag = m["home_goals"], m["away_goals"]
        samples.append((shrink_toward_market(probs.home_win, m["close_home"]), 1 if hg > ag else 0))
        samples.append((shrink_toward_market(probs.draw,     m["close_draw"]), 1 if hg == ag else 0))
        samples.append((shrink_toward_market(probs.away_win, m["close_away"]), 1 if ag > hg else 0))
    return samples


def cold_start_train_real(
    sport_keys: tuple[str, ...] = DEFAULT_COLD_START_LEAGUES,
    n_seasons: int = 2,
) -> dict:
    """Bootstrap the calibrator from REAL closing-odds history (football-data.co.uk).

    Superior to cold_start_train (synthetic base-rate market): probabilities are
    shrunk toward genuine Pinnacle/market closing lines, so the isotonic map is
    fitted on the same kind of probability production actually emits at bet time.
    """
    from betbot.data_sources import football_data_co_uk as fdc

    all_samples: list[tuple[float, int]] = []
    per_league: dict[str, int] = {}
    notes: list[str] = []
    for sk in sport_keys:
        try:
            matches = fdc.get_matches_with_odds(sk, n_seasons=n_seasons)
        except Exception as exc:
            notes.append(f"{sk}: error {exc}")
            per_league[sk] = 0
            continue
        if len(matches) < 100:
            notes.append(f"{sk}: only {len(matches)} matchs with closing odds")
            per_league[sk] = 0
            continue
        s = _walk_forward_real_samples(matches)
        all_samples.extend(s)
        per_league[sk] = len(s)
        notes.append(f"{sk}: {len(matches)} matchs → {len(s)} samples")

    if len(all_samples) < MIN_SAMPLES_TO_TRUST:
        return {"trained": False, "n_samples": len(all_samples), "per_league": per_league,
                "reason": f"only {len(all_samples)} real-odds samples — need {MIN_SAMPLES_TO_TRUST}",
                "notes": notes}

    payload = _build_payload(
        all_samples, {"football_h2h": all_samples}, MIN_SAMPLES_TO_TRUST,
        source="cold_start_real_odds", extra={"per_league": per_league},
    )
    if payload is None:
        return {"trained": False, "n_samples": len(all_samples), "per_league": per_league,
                "reason": "sklearn unavailable"}
    _persist(payload)
    logger.info("Cold-start (REAL odds) on %d samples, Brier %.4f → %.4f",
                len(all_samples), payload["brier_before"], payload["brier_after"])
    return {"trained": True, "n_samples": len(all_samples), "per_league": per_league,
            "path": str(CALIBRATOR_PATH), "brier_before": payload["brier_before"],
            "brier_after": payload["brier_after"], "notes": notes,
            "source": "cold_start_real_odds"}


def bootstrap_calibrator(fd_api_key: str = "") -> dict:
    """Best-available cold-start: try REAL closing odds (football-data.co.uk,
    keyless) first; fall back to the synthetic-market backtest (football-data.org,
    needs fd_api_key) only if the real path can't gather enough samples."""
    real = cold_start_train_real()
    if real.get("trained"):
        return real
    if not fd_api_key:
        return real
    logger.info("Cold-start réel indisponible (%s) → repli backtest synthétique.",
                real.get("reason"))
    synth = cold_start_train(fd_api_key)
    synth["real_attempt"] = real.get("reason")
    return synth


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
            # A map already on disk must clear the SAME sample floor as a new
            # one. Raising MIN_SAMPLES_TO_TRUST only governs future training,
            # so without this the pathological map stays live for as long as
            # the file exists — and it was fitted on picks we have since
            # quarantined as invalid. Enforcing the rule at load time makes it
            # self-correcting instead of a one-off cleanup.
            g = payload.get("global")
            if g and g.get("x") and g.get("y"):
                if int(g.get("n") or 0) >= MIN_SAMPLES_TO_TRUST:
                    glob = (list(g["x"]), list(g["y"]))
                else:
                    logger.info("Calibrateur global ignoré : %s échantillons "
                                "< %d requis", g.get("n"), MIN_SAMPLES_TO_TRUST)
            for seg, m in (payload.get("segments") or {}).items():
                if not (m.get("x") and m.get("y")):
                    continue
                if int(m.get("n") or 0) < MIN_SAMPLES_TO_TRUST:
                    logger.info("Segment '%s' ignoré : %s échantillons < %d requis",
                                seg, m.get("n"), MIN_SAMPLES_TO_TRUST)
                    continue
                segments[seg] = (list(m["x"]), list(m["y"]))
        elif fmt == "isotonic-thresholds-v1":  # legacy single map → global
            if payload.get("x_thresholds") and payload.get("y_thresholds"):
                if int(payload.get("n_samples") or 0) >= MIN_SAMPLES_TO_TRUST:
                    glob = (list(payload["x_thresholds"]),
                            list(payload["y_thresholds"]))
                else:
                    logger.info("Calibrateur legacy ignoré : %s échantillons "
                                "< %d requis", payload.get("n_samples"),
                                MIN_SAMPLES_TO_TRUST)
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
    xs, ys = xy[0], xy[1]
    # OUTSIDE THE TRAINED DOMAIN, DO NOT CALIBRATE.
    #
    # Both sklearn's `out_of_bounds="clip"` and `np.interp` answer an
    # out-of-range input with the nearest endpoint's y — extrapolation wearing
    # interpolation's clothes. It is not harmless here: the calibrator is
    # fitted on RESOLVED PICKS, which only exist above MIN_MODEL_PROB, so its
    # domain started at 0.454 while it was applied to every outcome of the
    # 1X2 distribution. A draw at 0.28 and an underdog at 0.17 were both handed
    # the first point's y — which was 0.0 — and the group renormalisation that
    # follows then turned "two zeros and one survivor" into a 100% certainty.
    # Measured 2026-08-10: 59 of the 60 picks made since 05/08 carried
    # model_prob = 1.000 on matches the market priced near 1.75.
    if prob < xs[0] or prob > xs[-1]:
        return prob
    try:
        import numpy as np
        out = float(np.interp(prob, xs, ys))
    except (ValueError, TypeError):
        return prob
    # NEVER let a calibrator assert certainty.
    #
    # An isotonic map legitimately contains 0.0 and 1.0 knots — those are the
    # extreme bins of its training sample, not statements about the world. Once
    # such a value reaches the group renormalisation, "two zeros and one
    # survivor" becomes a 100% probability on a match the market prices near
    # 1.75. That is exactly how 59 counted bets were fabricated.
    #
    # The clamp costs nothing when the fit is sane and is the last line of
    # defence when it is not: no amount of data justifies a bookmaker-beating
    # certainty from a step function.
    return max(_CAL_OUTPUT_MIN, min(out, _CAL_OUTPUT_MAX))


def in_domain(prob: float, segment: str | None = None) -> bool:
    """True when `prob` falls inside the range the calibrator was FITTED on.

    Callers that calibrate a coherent group of outcomes (1/X/2, Over/Under)
    must calibrate all of it or none of it: correcting the favourite while
    leaving the draw untouched, then renormalising, silently reshapes the
    distribution by an amount nobody chose. And since the calibrator is fitted
    on selected PICKS, its domain never reaches down to a draw's probability —
    so for a full 1X2 the honest answer is almost always "none of it".
    """
    cal = _load_calibrator()
    if cal is None:
        return False
    xy = None
    if segment and segment in cal["segments"]:
        xy = cal["segments"][segment]
    elif cal["global"] is not None:
        xy = cal["global"]
    if not xy or not xy[0]:
        return False
    return xy[0][0] <= float(prob) <= xy[0][-1]


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
