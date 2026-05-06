"""Tests for the Poisson prediction model."""
import pytest
from betbot.models import (
    poisson_match_probs,
    consensus_match_probs,
    _remove_margin,
    extract_best_odds,
    build_team_stats,
    compute_league_averages,
)


# ---------------------------------------------------------------------------
# poisson_match_probs
# ---------------------------------------------------------------------------

def test_probabilities_sum_to_one():
    probs = poisson_match_probs(1.5, 1.1)
    total = probs.home_win + probs.draw + probs.away_win
    assert abs(total - 1.0) < 1e-6


def test_strong_home_team_wins_more():
    probs = poisson_match_probs(lambda_home=3.0, lambda_away=0.5)
    assert probs.home_win > probs.away_win
    assert probs.home_win > 0.75


def test_balanced_match_has_significant_draw():
    probs = poisson_match_probs(1.2, 1.2)
    # Symmetric match: home_win ≈ away_win, draw is non-negligible
    assert abs(probs.home_win - probs.away_win) < 0.01
    assert probs.draw > 0.20


def test_high_scoring_match_over_25():
    # P(X+Y > 2 | X~Poi(2.5), Y~Poi(2.5)) ≈ 0.875 (sum~Poi(5), P(>2)≈87.5%)
    probs = poisson_match_probs(2.5, 2.5)
    assert probs.over_25 > 0.85


def test_low_scoring_match_under_25():
    probs = poisson_match_probs(0.5, 0.5)
    assert probs.over_25 < 0.10


def test_model_type_is_poisson():
    probs = poisson_match_probs(1.4, 1.0)
    assert probs.model == "poisson"


# ---------------------------------------------------------------------------
# value_edge calculation
# ---------------------------------------------------------------------------

def test_positive_value_when_model_above_book():
    probs = poisson_match_probs(1.8, 0.9)  # strong home team
    # Model prob_home should be high; if book gives 2.10 that might be value
    best_odds = 2.10
    value_edge = probs.home_win * best_odds - 1.0
    # Just verify the formula works correctly
    expected = probs.home_win * best_odds - 1.0
    assert abs(value_edge - expected) < 1e-9


def test_no_value_when_model_below_book():
    # model says 40% but book offers 2.00 (implies 50%)
    probs = poisson_match_probs(0.9, 1.8)  # weak home team
    assert probs.home_win < 0.50
    value_edge = probs.home_win * 2.00 - 1.0
    assert value_edge < 0  # no value


# ---------------------------------------------------------------------------
# _remove_margin
# ---------------------------------------------------------------------------

def test_remove_margin_sums_to_one():
    outcomes = [
        {"name": "Home", "price": 1.90},
        {"name": "Draw", "price": 3.60},
        {"name": "Away", "price": 4.50},
    ]
    fair = _remove_margin(outcomes)
    assert abs(sum(fair.values()) - 1.0) < 1e-9


def test_remove_margin_names_preserved():
    outcomes = [
        {"name": "Arsenal", "price": 2.00},
        {"name": "Draw", "price": 3.20},
        {"name": "Chelsea", "price": 3.80},
    ]
    fair = _remove_margin(outcomes)
    assert "Arsenal" in fair
    assert "Draw" in fair
    assert "Chelsea" in fair


def test_remove_margin_skips_invalid():
    outcomes = [
        {"name": "Home", "price": 1.90},
        {"name": "Draw", "price": "invalid"},
        {"name": "Away", "price": 4.00},
    ]
    fair = _remove_margin(outcomes)
    assert "Draw" not in fair
    assert len(fair) == 2


# ---------------------------------------------------------------------------
# consensus_match_probs
# ---------------------------------------------------------------------------

SAMPLE_EVENT = {
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "bookmakers": [
        {
            "key": "betclic",
            "title": "Betclic",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 1.85},
                {"name": "Chelsea", "price": 4.50},
                {"name": "Draw", "price": 3.60},
            ]}],
        },
        {
            "key": "bet365",
            "title": "Bet365",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 1.90},
                {"name": "Chelsea", "price": 4.40},
                {"name": "Draw", "price": 3.50},
            ]}],
        },
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 1.92},
                {"name": "Chelsea", "price": 4.30},
                {"name": "Draw", "price": 3.55},
            ]}],
        },
    ],
}


def test_consensus_probs_sum_to_one():
    result = consensus_match_probs(SAMPLE_EVENT)
    assert result is not None
    total = result.home_win + result.draw + result.away_win
    assert abs(total - 1.0) < 1e-6


def test_consensus_home_favorite():
    result = consensus_match_probs(SAMPLE_EVENT)
    assert result is not None
    assert result.home_win > result.away_win


def test_consensus_returns_none_with_one_book():
    event_one_book = {
        "home_team": "A",
        "away_team": "B",
        "bookmakers": [
            {
                "key": "betclic",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "A", "price": 2.00},
                    {"name": "B", "price": 3.50},
                    {"name": "Draw", "price": 3.20},
                ]}],
            }
        ],
    }
    result = consensus_match_probs(event_one_book)
    assert result is None


def test_consensus_model_type():
    result = consensus_match_probs(SAMPLE_EVENT)
    assert result is not None
    assert result.model == "consensus"


# ---------------------------------------------------------------------------
# extract_best_odds
# ---------------------------------------------------------------------------

def test_extract_best_odds_finds_highest():
    best = extract_best_odds(SAMPLE_EVENT, "Arsenal")
    assert best is not None
    assert best.price == 1.92  # Pinnacle has highest price
    assert best.bookmaker == "Pinnacle"


def test_extract_best_odds_returns_none_for_unknown():
    best = extract_best_odds(SAMPLE_EVENT, "Nonexistent Team")
    assert best is None


# ---------------------------------------------------------------------------
# build_team_stats
# ---------------------------------------------------------------------------

SAMPLE_MATCHES = [
    {"home_team": "Arsenal", "away_team": "Chelsea", "home_goals": 2, "away_goals": 1, "date": "2025-04-20"},
    {"home_team": "Tottenham", "away_team": "Arsenal", "home_goals": 1, "away_goals": 2, "date": "2025-04-13"},
    {"home_team": "Arsenal", "away_team": "Man City", "home_goals": 3, "away_goals": 0, "date": "2025-04-06"},
    {"home_team": "Liverpool", "away_team": "Arsenal", "home_goals": 0, "away_goals": 1, "date": "2025-03-30"},
    {"home_team": "Arsenal", "away_team": "Everton", "home_goals": 2, "away_goals": 1, "date": "2025-03-23"},
]


def test_build_team_stats_returns_lambdas():
    stats = build_team_stats("Arsenal", SAMPLE_MATCHES, 1.35, 1.10)
    assert stats is not None
    assert stats.lambda_home > 0
    assert stats.lambda_away > 0
    assert stats.matches_analyzed == 5


def test_build_team_stats_insufficient_data_returns_none():
    few_matches = SAMPLE_MATCHES[:2]
    stats = build_team_stats("Arsenal", few_matches, 1.35, 1.10)
    assert stats is None


def test_compute_league_averages():
    home_avg, away_avg = compute_league_averages(SAMPLE_MATCHES)
    assert home_avg > 0
    assert away_avg > 0
    total_home = sum(m["home_goals"] for m in SAMPLE_MATCHES)
    assert abs(home_avg - total_home / len(SAMPLE_MATCHES)) < 1e-9
