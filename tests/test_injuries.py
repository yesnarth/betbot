"""Injury → attack factor and its effect on the blended model — Vague 7."""
import pytest

from betbot import injuries
from betbot.models import TeamStats, blended_match_probs


def test_factor_is_1_when_disabled(monkeypatch):
    monkeypatch.delenv("FETCH_INJURIES", raising=False)
    injuries._factor_cache.clear()
    injuries.reset_run_budget()
    assert injuries.get_injury_factor("Arsenal", "soccer_epl") == 1.0


def test_factor_heuristic_counts():
    assert injuries.injury_factor_from_counts(0) == 1.0
    assert injuries.injury_factor_from_counts(1) == pytest.approx(0.965)
    assert injuries.injury_factor_from_counts(5) == pytest.approx(0.825)
    # Count is capped at MAX_ABSENCES_COUNTED (5) → max penalty 5×3.5% = 0.825.
    assert injuries.injury_factor_from_counts(20) == pytest.approx(0.825)
    assert injuries.injury_factor_from_counts(20) == injuries.injury_factor_from_counts(5)
    assert injuries.injury_factor_from_counts(-3) == 1.0                   # guard


def test_factor_1_for_unmapped_league(monkeypatch):
    monkeypatch.setenv("FETCH_INJURIES", "1")
    injuries._factor_cache.clear()
    injuries.reset_run_budget()
    # League not in _LEAGUE_ID → no API call, neutral factor.
    assert injuries.get_injury_factor("X", "soccer_brazil_serie_a") == 1.0


def test_factor_uses_api_when_enabled(monkeypatch):
    monkeypatch.setenv("FETCH_INJURIES", "1")
    injuries._factor_cache.clear()
    injuries._team_id_cache.clear()
    injuries.reset_run_budget()
    from betbot.data_sources import api_football
    monkeypatch.setattr(api_football, "search_team_id", lambda name, league_id=None: 42)
    monkeypatch.setattr(
        api_football, "get_team_injuries",
        lambda tid, lid, season: [
            {"type": "Missing Fixture"}, {"type": "Missing Fixture"}, {"type": "Questionable"},
        ],
    )
    f = injuries.get_injury_factor("Arsenal", "soccer_epl")
    # 2 confirmed absences ("Missing"); "Questionable" is not counted.
    assert f == pytest.approx(injuries.injury_factor_from_counts(2))
    injuries._factor_cache.clear()


def test_blended_applies_attack_mod():
    home = TeamStats(name="H", attack_home=1.5, defense_home=1.0,
                     attack_away=1.2, defense_away=1.0, matches_analyzed=20)
    away = TeamStats(name="A", attack_home=1.0, defense_home=1.0,
                     attack_away=0.9, defense_away=1.0, matches_analyzed=20)
    base = blended_match_probs(home, away, 1.4, 1.1)
    injured = blended_match_probs(home, away, 1.4, 1.1, home_attack_mod=0.80)
    assert injured.lambda_home < base.lambda_home   # home attack cut
    assert injured.home_win < base.home_win         # → less likely to win
