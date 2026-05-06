"""Unit tests for the market-shrinkage calibration."""
from betbot.calibration import (
    _market_implied_prob,
    is_edge_suspicious,
    shrink_toward_market,
)


def test_no_shrinkage_when_model_agrees_with_market():
    # Model says 50%, market odds 2.00 → implied 50% → gap 0 → no shrinkage
    assert shrink_toward_market(0.50, 2.00) == 0.50


def test_no_shrinkage_when_gap_below_soft_threshold():
    # Model 53%, market implied 50% → gap 3pp → below 5pp threshold → identity
    assert shrink_toward_market(0.53, 2.00) == 0.53


def test_partial_shrinkage_in_interpolation_zone():
    # Model 60%, market implied 50% → gap 10pp → midway between soft (5pp) and hard (20pp)
    # ratio = (10-5)/(20-5) = 0.333, weight = 0.85 * 0.333 = 0.283
    # result ≈ 0.717 * 0.60 + 0.283 * 0.50 = 0.5717
    result = shrink_toward_market(0.60, 2.00)
    assert 0.55 < result < 0.60   # closer to 50% than the original 60%


def test_max_shrinkage_at_extreme_disagreement():
    # Model 90%, market implied 50% (odds 2.00) → gap 40pp → hard zone, max 85% shrink
    # Expected: 0.15 * 0.90 + 0.85 * 0.50 = 0.135 + 0.425 = 0.56
    result = shrink_toward_market(0.90, 2.00)
    assert abs(result - 0.56) < 0.001


def test_shrinkage_reduces_edge():
    """The whole point: a 90% model prob at 2.00 odds claims +80% edge.
    After aggressive shrinkage, the implied edge drops below 20%."""
    raw_edge = 0.90 * 2.00 - 1
    calibrated = shrink_toward_market(0.90, 2.00)
    cal_edge = calibrated * 2.00 - 1
    assert raw_edge > 0.75
    assert cal_edge < 0.20


def test_market_implied_prob():
    assert _market_implied_prob(2.00) == 0.50
    assert _market_implied_prob(4.00) == 0.25
    assert _market_implied_prob(1.0) == 0.0   # invalid odds


def test_is_edge_suspicious():
    assert not is_edge_suspicious(0.05)    # 5% — normal
    assert not is_edge_suspicious(0.15)    # 15% — borderline ok
    assert is_edge_suspicious(0.25)        # 25% — suspect
    assert is_edge_suspicious(0.80)        # 80% — clearly fictitious
