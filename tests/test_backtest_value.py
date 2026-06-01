"""Backtest base-rate helper for the odds-free value metric — Wave 3, item 3.3."""
from betbot.backtest import _base_outcome_rates


def test_base_outcome_rates_counts_correctly():
    matches = [
        {"home_goals": 2, "away_goals": 0},  # home
        {"home_goals": 1, "away_goals": 1},  # draw
        {"home_goals": 0, "away_goals": 3},  # away
        {"home_goals": 2, "away_goals": 1},  # home
    ]
    h, d, a = _base_outcome_rates(matches)
    assert (h, d, a) == (0.5, 0.25, 0.25)


def test_base_outcome_rates_empty_falls_back():
    assert _base_outcome_rates([]) == (0.45, 0.27, 0.28)
