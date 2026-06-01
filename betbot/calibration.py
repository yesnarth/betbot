"""
Market shrinkage — pulls our model probabilities toward the consensus bookmaker
probability when the gap is suspiciously large.

Rationale (well-known result in betting research, Kuypers 2000, Levitt 2004):
  The bookmaker market is broadly well-calibrated for liquid leagues. A
  proprietary model is a USEFUL signal when it slightly disagrees with the
  market (5-15% disagreement → genuine value). When it disagrees by more than
  ~20-25%, the model is almost always wrong, not the market — usually because
  the model misses qualitative info (injuries, news, motivation).

This module shrinks the model probability toward the market probability with a
weight that grows as the disagreement grows. Concretely:

    if |model_prob - market_prob| < 0.10:  no shrinkage (we trust the model)
    if |model_prob - market_prob| > 0.30:  ~70% shrinkage toward market
    in between: smooth interpolation

Result: we still capture genuine 3-10% edges, but we cap fictitious 50-100%
edges that would otherwise build absurd parlays.
"""
from __future__ import annotations


def _market_implied_prob(decimal_odds: float) -> float:
    """Naive market-implied probability from a single bookmaker price.

    For a precise no-vig estimate we'd need all 3 outcome odds; here we use
    the simple 1/odds, which is biased upward by the bookmaker margin (~5-7%
    for soccer EU). That's actually fine for shrinkage — slightly over-weighting
    the market is conservative.
    """
    if decimal_odds <= 1.0:
        return 0.0
    return 1.0 / decimal_odds


def shrink_toward_market(
    model_prob: float,
    book_odds: float,
    soft_threshold: float = 0.05,
    hard_threshold: float = 0.20,
    max_shrinkage: float = 0.85,
) -> float:
    """
    Apply progressive shrinkage to a model probability based on its disagreement
    with the bookmaker-implied probability.

    Args:
        model_prob:    raw probability output by the Poisson/ELO model
        book_odds:     decimal odds available at the best bookmaker
        soft_threshold: gap below which no shrinkage is applied (default 10pp)
        hard_threshold: gap at which max shrinkage applies (default 30pp)
        max_shrinkage:  maximum weight given to the market (default 70%)

    Returns:
        the calibrated probability — always between min(model, market) and
        max(model, market). Identity when book_odds is invalid.
    """
    if book_odds <= 1.0:
        return model_prob
    market_prob = _market_implied_prob(book_odds)
    gap = abs(model_prob - market_prob)

    if gap <= soft_threshold:
        return model_prob

    if gap >= hard_threshold:
        weight = max_shrinkage
    else:
        # Linear interpolation between soft and hard thresholds. Guard the
        # denominator so a misconfigured soft==hard never divides by zero.
        span = hard_threshold - soft_threshold
        ratio = (gap - soft_threshold) / span if span > 0 else 1.0
        weight = max_shrinkage * ratio

    # Pull model_prob toward market_prob by `weight`
    return (1.0 - weight) * model_prob + weight * market_prob


def is_edge_suspicious(value_edge: float, threshold: float = 0.20) -> bool:
    """Returns True when an edge is large enough to be a probable model artifact."""
    return value_edge > threshold
