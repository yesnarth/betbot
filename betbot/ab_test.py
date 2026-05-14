"""
A/B testing framework for the local agent's business rules.

Concept: rather than guessing whether `RULE_INJURY_FAVORITE = 0.85` is the
right multiplier, we replay historical resolved predictions through the rule
chain with TWO variants of the parameter and compare which one would have
produced better Brier score / log-loss / hypothetical ROI.

Strict offline replay — no live odds, no API calls. Uses only what's in DB:
  - Prediction.model_prob (raw)
  - Prediction.best_odds, closing_odds
  - Prediction.result (the actual outcome)

Output: side-by-side comparison of the two rule sets across all resolved
predictions in the chosen window.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from betbot.calibration import shrink_toward_market
from betbot.database import session_scope
from betbot.orm_models import Prediction

logger = logging.getLogger("betbot.ab_test")


@dataclass
class RuleVariant:
    """Configurable knobs we want to A/B test against each other."""
    name: str
    market_shrink_soft: float = 0.05
    market_shrink_hard: float = 0.20
    market_shrink_max: float = 0.85
    overconfidence_cap: float = 0.85
    overconfidence_penalty: float = 0.90
    huge_edge_threshold: float = 0.35
    huge_edge_penalty: float = 0.65


@dataclass
class VariantResult:
    name: str
    n_bets: int
    brier_score: float
    log_loss: float
    hit_rate: float                # %
    simulated_roi_pct: float       # ROI if we had bet Kelly with this prob
    avg_calibrated_prob: float
    avg_raw_prob: float


def _apply_variant(raw_prob: float, best_odds: float, variant: RuleVariant) -> float:
    """Run a single (raw_prob, odds) pair through the variant's rule chain.

    Returns the calibrated probability. We DON'T have access to news/weather
    in offline replay — the variant only tunes the deterministic shrinkage
    knobs, which is what we actually want to optimize anyway.
    """
    p = raw_prob
    if p > variant.overconfidence_cap:
        p *= variant.overconfidence_penalty
    p = shrink_toward_market(
        p, best_odds,
        soft_threshold=variant.market_shrink_soft,
        hard_threshold=variant.market_shrink_hard,
        max_shrinkage=variant.market_shrink_max,
    )
    raw_edge = raw_prob * best_odds - 1.0
    if raw_edge > variant.huge_edge_threshold:
        # Without news to confirm, a huge edge gets penalized
        p *= variant.huge_edge_penalty
    return max(0.001, min(p, 0.999))


def _score_variant(
    name: str,
    samples: list[tuple[float, float, str]],
    variant: RuleVariant,
) -> VariantResult:
    """samples: list of (raw_model_prob, best_odds, result)."""
    n = len(samples)
    if n == 0:
        return VariantResult(name, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    brier_sum = 0.0
    log_sum = 0.0
    wins = 0
    pnl = 0.0
    raw_sum = 0.0
    cal_sum = 0.0

    for raw_prob, best_odds, result in samples:
        cal_prob = _apply_variant(raw_prob, best_odds, variant)
        actual = 1 if result == "win" else 0
        brier_sum += (cal_prob - actual) ** 2
        clipped = max(0.001, min(cal_prob, 0.999))
        log_sum -= math.log(clipped if actual else 1 - clipped)
        if actual:
            wins += 1
        # Simulated ROI: bet 1 unit per pick (simple), profit = odds-1 if win, -1 if loss
        if cal_prob * best_odds > 1.0:   # only bet when EV positive after calibration
            if actual:
                pnl += best_odds - 1
            else:
                pnl -= 1
        raw_sum += raw_prob
        cal_sum += cal_prob

    return VariantResult(
        name=name,
        n_bets=n,
        brier_score=round(brier_sum / n, 4),
        log_loss=round(log_sum / n, 4),
        hit_rate=round(wins / n * 100, 1),
        simulated_roi_pct=round(pnl / n * 100, 2) if n else 0.0,
        avg_raw_prob=round(raw_sum / n, 4),
        avg_calibrated_prob=round(cal_sum / n, 4),
    )


def compare_variants(
    variant_a: RuleVariant,
    variant_b: RuleVariant,
    days: int = 90,
    only_placed: bool = False,
) -> dict:
    """
    Replay every resolved win/loss prediction through BOTH variants,
    return their scores side-by-side.

    only_placed=True restricts to bets the user actually played; False (the
    default) uses every recommended bet — gives more data points but mixes
    in non-played picks.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with session_scope() as s:
        stmt = select(
            Prediction.model_prob,
            Prediction.best_odds,
            Prediction.result,
        ).where(
            Prediction.result.in_(("win", "loss")),
            Prediction.created_at >= cutoff,
        )
        if only_placed:
            stmt = stmt.where(Prediction.placement_status == "confirmed")
        rows = s.execute(stmt).all()

    samples = [(float(p), float(o), r) for p, o, r in rows]

    return {
        "variant_a": _score_variant(variant_a.name, samples, variant_a).__dict__,
        "variant_b": _score_variant(variant_b.name, samples, variant_b).__dict__,
        "n_samples": len(samples),
        "days": days,
        "only_placed": only_placed,
        "verdict": _verdict(samples, variant_a, variant_b),
    }


def _verdict(samples: list, a: RuleVariant, b: RuleVariant) -> str:
    """Pick the winner with a simple Brier comparison."""
    if not samples:
        return "Not enough data"
    ra = _score_variant(a.name, samples, a)
    rb = _score_variant(b.name, samples, b)
    if abs(ra.brier_score - rb.brier_score) < 0.005:
        return f"Tie ({a.name} {ra.brier_score:.4f} vs {b.name} {rb.brier_score:.4f})"
    if ra.brier_score < rb.brier_score:
        return f"{a.name} wins (Brier {ra.brier_score:.4f} vs {rb.brier_score:.4f})"
    return f"{b.name} wins (Brier {rb.brier_score:.4f} vs {ra.brier_score:.4f})"
