"""Self-computed Elo from match results — F1 (removes the ClubElo dependency)."""
import pytest

from betbot.elo_local import BASE_ELO, compute_elo_ratings, expected_home_score


def _m(h, a, hg, ag, date="2026-01-01"):
    return {"home_team": h, "away_team": a, "home_goals": hg, "away_goals": ag, "date": date}


def test_home_win_is_zero_sum_and_moves_both_sides():
    r = compute_elo_ratings([_m("A", "B", 1, 0)], mov=False)
    assert r["A"] > BASE_ELO and r["B"] < BASE_ELO
    # Elo is zero-sum per match → pool average preserved.
    assert r["A"] + r["B"] == pytest.approx(2 * BASE_ELO, abs=1e-6)
    # Equal teams, home advantage 65 → home expected ≈0.59, so a win adds ≈8.15.
    assert r["A"] == pytest.approx(1508.15, abs=0.1)


def test_draw_between_equals_favours_the_away_side():
    # With a home advantage the home team is EXPECTED to do better, so a draw
    # underperforms for them → home dips below base, away rises above it.
    r = compute_elo_ratings([_m("Home", "Away", 1, 1)], mov=False)
    assert r["Home"] < BASE_ELO < r["Away"]


def test_margin_of_victory_amplifies_the_update():
    big = compute_elo_ratings([_m("A", "B", 4, 0)], mov=True)["A"]
    small = compute_elo_ratings([_m("A", "B", 1, 0)], mov=True)["A"]
    assert big > small > BASE_ELO


def test_repeated_winner_outranks_loser():
    matches = [_m("Strong", "Weak", 2, 0, f"2026-01-0{i}") for i in range(1, 6)]
    r = compute_elo_ratings(matches)
    assert r["Strong"] > BASE_ELO > r["Weak"]
    assert r["Strong"] > r["Weak"] + 50   # a clear, accumulated gap


def test_applied_in_chronological_order_regardless_of_input_order():
    a = _m("A", "B", 2, 0, "2026-01-01")
    b = _m("B", "A", 3, 0, "2026-02-01")
    assert compute_elo_ratings([a, b]) == compute_elo_ratings([b, a])


def test_empty_and_malformed_are_safe():
    assert compute_elo_ratings([]) == {}
    # Missing goals / teams → skipped, no crash.
    bad = [{"home_team": "A", "away_team": None, "home_goals": 1, "away_goals": 0, "date": "x"},
           {"home_team": "A", "away_team": "B", "home_goals": None, "away_goals": 0, "date": "y"}]
    assert compute_elo_ratings(bad) == {}


def test_expected_home_score_bounds():
    # Symmetric around 0.5 once home advantage is removed.
    assert expected_home_score(1500, 1500, home_adv=0) == pytest.approx(0.5)
    assert expected_home_score(1900, 1500, home_adv=0) > 0.85   # strong favourite
    assert expected_home_score(1500, 1900, home_adv=0) < 0.15
