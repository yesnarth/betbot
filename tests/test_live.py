"""In-play (live) probability models — football / basketball / tennis."""
from datetime import datetime, timezone

import pytest

from betbot.live import (
    _per_set_prob_from_match,
    filter_live,
    inplay_basketball_probs,
    inplay_football_probs,
    inplay_tennis_probs,
)


# ---- Football -------------------------------------------------------------

def test_football_near_end_locks_the_result():
    p = inplay_football_probs(1.5, 1.2, frac_left=0.01, goals_home=2, goals_away=0)
    assert p.home_win > 0.95
    assert p.away_win < 0.02


def test_football_over25_already_reached():
    p = inplay_football_probs(1.0, 1.0, frac_left=0.5, goals_home=2, goals_away=1)  # 3 goals
    assert p.over_25 == 1.0
    assert p.under_25 == 0.0


def test_football_kickoff_is_open_and_normalized():
    p = inplay_football_probs(1.4, 1.1, frac_left=1.0, goals_home=0, goals_away=0)
    assert abs((p.home_win + p.draw + p.away_win) - 1.0) < 0.01
    assert 0.2 < p.home_win < 0.70


# ---- Basketball -----------------------------------------------------------

def test_basket_big_lead_late_is_near_certain():
    p = inplay_basketball_probs(exp_home=110, exp_away=108, frac_left=0.02,
                                cur_home=100, cur_away=80, sport_key="basketball_nba")
    assert p.home_win > 0.95


def test_basket_tied_start_tracks_expected_margin():
    p = inplay_basketball_probs(exp_home=112, exp_away=108, frac_left=1.0,
                                cur_home=0, cur_away=0, sport_key="basketball_nba")
    assert 0.50 < p.home_win < 0.75
    assert abs(p.home_win + p.away_win - 1.0) < 1e-6


# ---- Tennis ---------------------------------------------------------------

def test_tennis_two_sets_up_bo3_is_a_win():
    assert inplay_tennis_probs(0.55, sets_home=2, sets_away=0, best_of=3) == 1.0


def test_tennis_one_one_bo3_equals_per_set_prob():
    p = _per_set_prob_from_match(0.60, 3)
    assert abs(inplay_tennis_probs(0.60, 1, 1, 3) - round(p, 4)) < 0.01


def test_tennis_per_set_inversion_is_sane():
    assert _per_set_prob_from_match(0.5, 3) == pytest.approx(0.5, abs=0.02)
    assert _per_set_prob_from_match(0.8, 3) > 0.6


# ---- filter_live ----------------------------------------------------------

def test_filter_live_keeps_only_started():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        {"id": "a", "commence_time": "2026-06-01T11:30:00Z"},  # in progress
        {"id": "b", "commence_time": "2026-06-01T13:00:00Z"},  # not started
    ]
    assert [e["id"] for e in filter_live(events, now=now)] == ["a"]
