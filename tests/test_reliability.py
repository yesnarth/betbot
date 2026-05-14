"""
Unit tests for the per-pick reliability score.

The function is a deterministic heuristic with no side effects — tests
can be ordinary unit tests (no DB needed).
"""
from __future__ import annotations

import pytest

from betbot.reliability import compute_reliability, reliability_label


def test_baseline_pick_has_high_reliability():
    """Reasonable inputs → score in the high band."""
    s = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=15,
    )
    assert s >= 0.85
    assert reliability_label(s) == "haute"


def test_small_sample_drops_reliability():
    s_big = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=15,
    )
    s_small = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=4,
    )
    assert s_small < s_big


def test_huge_edge_drops_reliability():
    """An edge > 25% is suspicious — score should land in the low band."""
    s = compute_reliability(
        model_prob=0.60, value_edge=0.30, model_type="poisson", n_matches=15,
    )
    assert s <= 0.40
    assert reliability_label(s) == "faible"


def test_extreme_probability_drops_reliability():
    """A model probability outside [0.20, 0.80] gets a 0.7× penalty."""
    s_central = compute_reliability(
        model_prob=0.50, value_edge=0.05, model_type="poisson", n_matches=15,
    )
    s_extreme = compute_reliability(
        model_prob=0.85, value_edge=0.05, model_type="poisson", n_matches=15,
    )
    assert s_extreme < s_central


def test_consensus_model_penalty():
    """Consensus has no independent signal → multiplicative penalty."""
    s_poisson = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=15,
    )
    s_consensus = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="consensus", n_matches=15,
    )
    assert s_consensus < s_poisson


def test_none_n_matches_skips_sample_penalty():
    """When n_matches is None (e.g. consensus / tennis without sample notion),
    the sample-size factor is 1 and only the other penalties apply."""
    s = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=None,
    )
    assert s >= 0.85  # no sample-size penalty


def test_zero_n_matches_is_treated_same_as_unknown():
    """A 0-match team is effectively a 'no sample' signal — caller sometimes
    passes 0 instead of None. We treat it the same way (no penalty);
    detect_value_bets in analysis.py routes 0 to None at the call site."""
    s_zero = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=0,
    )
    s_none = compute_reliability(
        model_prob=0.55, value_edge=0.05, model_type="poisson", n_matches=None,
    )
    # Both should give identical results because the helper ignores n=0.
    assert s_zero == s_none


def test_score_in_unit_interval():
    """Whatever the inputs, score stays in [0, 1]."""
    for prob in (0.01, 0.5, 0.99):
        for edge in (-0.5, 0.0, 0.5, 5.0):
            for n in (None, 0, 1, 10, 100):
                s = compute_reliability(
                    model_prob=prob, value_edge=edge,
                    model_type="poisson", n_matches=n,
                )
                assert 0.0 <= s <= 1.0


@pytest.mark.parametrize("score, expected", [
    (0.95, "haute"),
    (0.70, "haute"),     # boundary
    (0.69, "moyenne"),
    (0.50, "moyenne"),
    (0.40, "moyenne"),   # boundary
    (0.39, "faible"),
    (0.00, "faible"),
])
def test_reliability_label_buckets(score, expected):
    assert reliability_label(score) == expected
