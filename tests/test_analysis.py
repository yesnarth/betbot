"""Tests for Kelly stake, value detection, and parlay building."""
import pytest
from betbot.analysis import kelly_stake, build_parlays, rank_value_bets, ValueBet


# ---------------------------------------------------------------------------
# Kelly Criterion
# ---------------------------------------------------------------------------

def test_kelly_zero_for_negative_edge():
    # 40% model prob at 2.00 odds → edge = 0.40*2.00-1 = -0.20 → don't bet
    stake = kelly_stake(model_prob=0.40, decimal_odds=2.00, bankroll=100.0)
    assert stake == 0.0


def test_kelly_zero_for_zero_prob():
    stake = kelly_stake(model_prob=0.0, decimal_odds=2.00, bankroll=100.0)
    assert stake == 0.0


def test_kelly_positive_for_genuine_edge():
    # 60% model prob at 2.00 odds → edge = +20%
    stake = kelly_stake(model_prob=0.60, decimal_odds=2.00, bankroll=100.0)
    assert stake > 0.0


def test_kelly_capped_at_max_fraction():
    # Even massive edge should not exceed 5% of bankroll
    stake = kelly_stake(model_prob=0.95, decimal_odds=2.00, bankroll=100.0, max_fraction=0.05)
    assert stake <= 5.0


def test_kelly_respects_kelly_fraction():
    # Use a small edge so max_fraction cap doesn't interfere
    # 0.53 prob at 2.00 odds → full Kelly = 6%, quarter = 1.5% < 5% cap
    full = kelly_stake(0.53, 2.00, 100.0, kelly_fraction=1.0, max_fraction=0.10)
    quarter = kelly_stake(0.53, 2.00, 100.0, kelly_fraction=0.25, max_fraction=0.10)
    assert quarter < full


def test_kelly_scales_with_bankroll():
    s1 = kelly_stake(0.60, 2.00, 100.0)
    s2 = kelly_stake(0.60, 2.00, 200.0)
    assert abs(s2 - 2 * s1) < 0.02


def test_kelly_invalid_odds_returns_zero():
    stake = kelly_stake(0.60, 1.00, 100.0)  # b = 0 → division by zero risk
    assert stake == 0.0


# ---------------------------------------------------------------------------
# Parlay builder
# ---------------------------------------------------------------------------

def _make_bet(event_id: str, model_prob: float, odds: float, edge: float) -> ValueBet:
    return ValueBet(
        event_id=event_id,
        sport_key="soccer_epl",
        home_team="A",
        away_team="B",
        league_label="Premier League",
        market="h2h",
        selection_code="1",
        selection_label="Victoire domicile",
        model_prob=model_prob,
        best_odds=odds,
        best_book="Bet365",
        value_edge=edge,
        kelly_stake=3.0,
        lambda_home=1.4,
        lambda_away=1.0,
        model_type="poisson",
    )


BET_A = _make_bet("evt1", 0.60, 1.90, 0.14)
BET_B = _make_bet("evt2", 0.55, 2.10, 0.155)
BET_C = _make_bet("evt3", 0.65, 1.80, 0.17)
BET_D = _make_bet("evt4", 0.50, 2.50, 0.25)


def test_parlay_no_same_match_twice():
    # Two bets on the same event should never appear in a parlay
    dup = _make_bet("evt1", 0.40, 3.50, 0.40)  # same event_id as BET_A
    parlays = build_parlays([BET_A, dup, BET_B, BET_C, BET_D], n_legs=3)
    for p in parlays:
        event_ids = [b.event_id for b in p.bets]
        assert len(event_ids) == len(set(event_ids)), "Duplicate event in parlay"


def test_parlay_combined_odds_correct():
    parlays = build_parlays([BET_A, BET_B, BET_C], n_legs=3)
    assert len(parlays) == 1
    expected = round(BET_A.best_odds * BET_B.best_odds * BET_C.best_odds, 2)
    assert abs(parlays[0].combined_odds - expected) < 0.01


def test_parlay_min_combined_odds_filter():
    # All bets have ~1.8 odds, combined ~5.8 > 2.0 threshold
    parlays = build_parlays([BET_A, BET_B, BET_C], n_legs=3, min_combined_odds=10.0)
    assert len(parlays) == 0


def test_parlay_sorted_by_ev():
    parlays = build_parlays([BET_A, BET_B, BET_C, BET_D], n_legs=3, top_n=3)
    evs = [p.combined_ev for p in parlays]
    assert evs == sorted(evs, reverse=True)


def test_parlay_returns_top_n():
    bets = [_make_bet(f"evt{i}", 0.55, 1.90, 0.045) for i in range(10)]
    parlays = build_parlays(bets, n_legs=3, top_n=3)
    assert len(parlays) <= 3


# ---------------------------------------------------------------------------
# rank_value_bets
# ---------------------------------------------------------------------------

def test_rank_by_edge_descending():
    bets = [BET_A, BET_B, BET_C, BET_D]
    ranked = rank_value_bets(bets)
    edges = [b.value_edge for b in ranked]
    assert edges == sorted(edges, reverse=True)


def test_rank_empty_list():
    assert rank_value_bets([]) == []
