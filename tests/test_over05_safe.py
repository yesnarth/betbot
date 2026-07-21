"""Over 0.5 market + early-resolving classification (safe & fast preset)."""
from betbot.models import poisson_match_probs
from betbot_dashboard.components.picks import is_early_resolving, group_picks_by_match


def test_over05_computed_and_monotonic():
    p = poisson_match_probs(1.5, 1.2)
    # fewer goals required → higher probability
    assert p.over_05 > p.over_15 > p.over_25 > p.over_35
    assert 0.80 < p.over_05 < 1.0                       # ≥1 goal is very likely
    assert abs(p.over_05 + p.under_05 - 1.0) < 1e-6


def test_is_early_resolving():
    for c in ("O05", "O15", "O25", "O35", "BTTSY"):
        assert is_early_resolving(c), c
    for c in ("1", "X", "2", "1X", "X2", "12", "DNB1", "DNB2", "U25", "U05", "BTTSN"):
        assert not is_early_resolving(c), c


def test_group_by_prob_keeps_highest_probability():
    picks = [
        {"event_id": "e1", "home_team": "A", "away_team": "B", "selection_label": "1",
         "selection_code": "1", "value_edge": 0.09, "model_prob": 0.55},
        {"event_id": "e1", "home_team": "A", "away_team": "B", "selection_label": "Over 0.5",
         "selection_code": "O05", "value_edge": 0.02, "model_prob": 0.93},
    ]
    primary, _ = group_picks_by_match(picks, rank_by="prob")
    assert primary[0]["selection_code"] == "O05"        # highest prob wins in safe mode
