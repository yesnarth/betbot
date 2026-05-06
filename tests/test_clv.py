"""Unit tests for CLV calculation. Pure-Python — no DB, no network."""
from betbot.clv import compute_clv_pct


def test_clv_positive_when_entry_better_than_closing():
    # Got 2.20 entry, market settled at 2.00 → +10% CLV
    assert compute_clv_pct(2.20, 2.00) == 10.0


def test_clv_negative_when_market_moves_in_favor():
    # Got 1.80 entry, market settled at 2.00 → -10% CLV
    assert compute_clv_pct(1.80, 2.00) == -10.0


def test_clv_zero_when_no_movement():
    assert compute_clv_pct(2.00, 2.00) == 0.0


def test_clv_zero_when_invalid_odds():
    assert compute_clv_pct(0.5, 2.00) == 0.0
    assert compute_clv_pct(2.00, 1.0) == 0.0
    assert compute_clv_pct(2.00, 0.0) == 0.0


def test_clv_rounding():
    # 2.13 / 1.97 - 1 = 0.0812... → +8.12%
    assert compute_clv_pct(2.13, 1.97) == 8.12
