"""
Per-pick reliability score.

A 0–1 score that captures how trustworthy a value-bet estimate is. It does
NOT replace the value_edge — it *qualifies* it. A pick with edge=+15% but
reliability=0.3 deserves less stake than the same edge at reliability=0.9.

Heuristic (not full Bayesian — kept tractable and explainable):

    score = 1.0
    sample-size penalty (when n_matches is known)
        n <  6  → ×0.30  ("rookie team / start of season")
        n <  10 → ×0.60
        n <  15 → ×0.85
    edge-magnitude penalty (huge edges are almost always model artifacts)
        |edge| > 0.25 → ×0.30
        |edge| > 0.15 → ×0.60
        |edge| > 0.10 → ×0.85
    extreme-probability penalty (super-confident model on long-shot, or vice versa)
        p < 0.20  or  p > 0.80 → ×0.70
    model-type penalty (consensus is less informative than statistical models)
        consensus → ×0.75

Why heuristic and not bootstrap:
  Bootstrapping the team stats and recomputing the score matrix B times per
  pick would be the principled approach. Cost: ~50× the current per-pick
  prediction time. We can revisit if the heuristic underperforms in practice.

Reference points from a typical EPL match scan:
  - Mid-table side after 12 matches with a +6% edge: reliability ≈ 0.85
  - Promoted side after 4 matches with a +12% edge: reliability ≈ 0.18
  - Tennis ELO 1.50 favorite at +20% edge: reliability ≈ 0.60
"""
from __future__ import annotations

from typing import Literal

ReliabilityLabel = Literal["haute", "moyenne", "faible"]


def compute_reliability(
    *,
    model_prob: float,
    value_edge: float,
    model_type: str,
    n_matches: int | None = None,
    skip_extreme_prob_penalty: bool = False,
) -> float:
    """
    Return a reliability score in [0, 1] — higher is more trustworthy.

    `n_matches` is the minimum sample size across the inputs that produced
    `model_prob` (typically the weaker of the two teams' match histories).
    Pass None when the model doesn't have a clean sample notion
    (consensus model, tennis ELO).

    `skip_extreme_prob_penalty`: set for Double Chance / Draw No Bet, where a
    high probability (>0.80) is the *nature* of the market — the favorite-or-draw
    combined outcome is meant to be near-certain — not an overconfidence artifact.
    """
    score = 1.0

    # Sample-size penalty (only when applicable)
    if n_matches is not None and n_matches > 0:
        if n_matches < 6:
            score *= 0.30
        elif n_matches < 10:
            score *= 0.60
        elif n_matches < 15:
            score *= 0.85

    # Edge-magnitude penalty — a +25% edge in a liquid market is almost
    # always the model being wrong, not the market.
    abs_edge = abs(value_edge)
    if abs_edge > 0.25:
        score *= 0.30
    elif abs_edge > 0.15:
        score *= 0.60
    elif abs_edge > 0.10:
        score *= 0.85

    # Extreme-probability penalty — models tend to be over-confident at
    # the tails. A 0.92 model probability on a 5.0 underdog or 0.10 on
    # a 1.10 favorite is suspicious before it's edge-able.
    if not skip_extreme_prob_penalty and (model_prob < 0.20 or model_prob > 0.80):
        score *= 0.70

    # Model-type penalty — consensus is just a vig-corrected average of
    # bookmaker prices, so it has no independent signal beyond the
    # market. Strong tennis ELO or pace-based basketball are statistical
    # models with sample evidence behind them.
    if model_type == "consensus":
        score *= 0.75

    return round(max(0.0, min(score, 1.0)), 3)


def reliability_label(score: float) -> ReliabilityLabel:
    """Map a reliability score to a 3-bucket label for the dashboard."""
    if score >= 0.70:
        return "haute"
    if score >= 0.40:
        return "moyenne"
    return "faible"
