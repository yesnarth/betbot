"""Unit tests for betbot/basketball_model.py — pace + ORtg/DRtg projection."""
from __future__ import annotations

import pytest

from betbot import basketball_model
from betbot.basketball_model import (
    LEAGUE_AVG_PACE,
    LEAGUE_AVG_RATING,
    NBA_HOME_ADVANTAGE,
    EUROLEAGUE_HOME_ADVANTAGE,
    TeamSnapshot,
    _name_lookup,
    _normal_cdf,
    predict,
    predict_total_over,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    p = tmp_path / "basketball_teams.json"
    monkeypatch.setattr(basketball_model, "STATS_PATH", p)
    basketball_model.reset_cache()
    yield p
    basketball_model.reset_cache()


def _avg_team(name: str) -> TeamSnapshot:
    return TeamSnapshot(
        name=name,
        pace=LEAGUE_AVG_PACE,
        off_rating=LEAGUE_AVG_RATING,
        def_rating=LEAGUE_AVG_RATING,
        games=82,
    )


# ---------------------------------------------------------------------------
# Math primitives
# ---------------------------------------------------------------------------

def test_normal_cdf_at_zero_is_half():
    assert _normal_cdf(0.0) == pytest.approx(0.5)


def test_normal_cdf_monotonic():
    for a, b in [(-2, -1), (-1, 0), (0, 1), (1, 2)]:
        assert _normal_cdf(a) < _normal_cdf(b)


def test_normal_cdf_tails():
    assert _normal_cdf(-3) < 0.005
    assert _normal_cdf(3) > 0.995


# ---------------------------------------------------------------------------
# predict()
# ---------------------------------------------------------------------------

def test_predict_returns_none_without_data(isolated):
    assert predict("Anything", "Anything", league="nba") is None


def test_predict_two_average_teams_gives_home_advantage(isolated):
    teams = {"Home": _avg_team("Home"), "Away": _avg_team("Away")}
    basketball_model.save_teams(teams, path=isolated)
    basketball_model.reset_cache()

    p = predict("Home", "Away", league="nba")
    assert p is not None
    # Home advantage = 2.7 pts → margin = 2.7 → home_win > 0.5
    assert p.expected_margin == pytest.approx(NBA_HOME_ADVANTAGE)
    assert 0.55 < p.home_win < 0.65
    assert p.home_win + p.away_win == pytest.approx(1.0, abs=1e-6)


def test_predict_total_reflects_pace(isolated):
    """Higher pace = more possessions = higher expected total."""
    fast = TeamSnapshot(name="Fast", pace=110.0, off_rating=120.0,
                        def_rating=110.0, games=82)
    slow = TeamSnapshot(name="Slow", pace=90.0, off_rating=110.0,
                        def_rating=110.0, games=82)
    teams = {"Fast": fast, "Slow": slow}
    basketball_model.save_teams(teams, path=isolated)
    basketball_model.reset_cache()
    p_fast = predict("Fast", "Fast", league="nba")  # both fast
    basketball_model.save_teams({"Slow": slow, "Slow2": slow}, path=isolated)
    basketball_model.reset_cache()
    p_slow = predict("Slow", "Slow2", league="nba")  # both slow
    assert p_fast.expected_total > p_slow.expected_total


def test_predict_better_team_dominates(isolated):
    elite = TeamSnapshot(name="Elite", pace=100.0, off_rating=125.0,
                        def_rating=105.0, games=82)  # net +20
    weak = TeamSnapshot(name="Weak", pace=100.0, off_rating=105.0,
                       def_rating=125.0, games=82)   # net -20
    teams = {"Elite": elite, "Weak": weak}
    basketball_model.save_teams(teams, path=isolated)
    basketball_model.reset_cache()
    p = predict("Elite", "Weak", league="nba")
    assert p.home_win > 0.95
    assert p.expected_margin > 15  # very favored


def test_predict_euroleague_uses_smaller_home_advantage(isolated):
    teams = {"H": _avg_team("H"), "A": _avg_team("A")}
    basketball_model.save_teams(teams, path=isolated)
    basketball_model.reset_cache()
    p_nba = predict("H", "A", league="nba")
    p_eu = predict("H", "A", league="euroleague")
    assert p_nba.expected_margin == pytest.approx(NBA_HOME_ADVANTAGE)
    assert p_eu.expected_margin == pytest.approx(EUROLEAGUE_HOME_ADVANTAGE)
    assert p_nba.home_win > p_eu.home_win


# ---------------------------------------------------------------------------
# Total over/under
# ---------------------------------------------------------------------------

def test_predict_total_over_below_expected_is_likely():
    """If expected total is 230 and the over/under line is 220, P(over) > 0.5."""
    p = predict_total_over(line=220, total_expected=230)
    assert p > 0.5


def test_predict_total_over_above_expected_is_unlikely():
    p = predict_total_over(line=240, total_expected=230)
    assert p < 0.5


# ---------------------------------------------------------------------------
# Name lookup
# ---------------------------------------------------------------------------

def test_name_lookup_exact_match():
    teams = {"Boston Celtics": _avg_team("Boston Celtics")}
    t, matched = _name_lookup("Boston Celtics", teams)
    assert t is not None
    assert matched == "Boston Celtics"


def test_name_lookup_token_set_substring():
    teams = {"Los Angeles Lakers": _avg_team("Los Angeles Lakers")}
    t, matched = _name_lookup("Lakers", teams)
    assert t is not None  # "lakers" is subset of {"los", "angeles", "lakers"}


def test_name_lookup_returns_none_when_no_overlap():
    teams = {"Boston Celtics": _avg_team("Boston Celtics")}
    t, matched = _name_lookup("Phoenix Suns", teams)
    assert t is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(isolated):
    teams = {
        "Boston Celtics": TeamSnapshot(name="Boston Celtics", pace=98.5,
                                        off_rating=120.8, def_rating=112.7, games=80),
    }
    basketball_model.save_teams(teams, path=isolated)
    basketball_model.reset_cache()
    loaded = basketball_model.load_teams(path=isolated)
    assert "Boston Celtics" in loaded
    assert loaded["Boston Celtics"].pace == pytest.approx(98.5)
    assert loaded["Boston Celtics"].off_rating == pytest.approx(120.8)


def test_status_reports_top5(isolated):
    teams = {
        f"T{i}": TeamSnapshot(name=f"T{i}", pace=100, off_rating=100 + i,
                              def_rating=100, games=80)
        for i in range(10)
    }
    basketball_model.save_teams(teams, path=isolated)
    basketball_model.reset_cache()
    s = basketball_model.status()
    assert s["available"] is True
    assert s["n_teams"] == 10
    assert len(s["top5_net_rating"]) == 5
    # Highest off_rating - def_rating wins
    assert s["top5_net_rating"][0]["name"] == "T9"
