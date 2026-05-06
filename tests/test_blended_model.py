"""Unit tests for the blended Dixon-Coles + xG + ELO model."""
import pytest

from betbot.models import TeamStats, blended_match_probs


def _ts(name, ah, dh, aa, da, elo=None, xgf=None, xga=None):
    return TeamStats(
        name=name,
        attack_home=ah, defense_home=dh,
        attack_away=aa, defense_away=da,
        matches_analyzed=20,
        elo_rating=elo, xg_for=xgf, xg_against=xga,
    )


def test_blended_falls_back_to_dixon_coles_without_enrichment():
    """No ELO, no xG → must produce probs labeled "poisson"."""
    home = _ts("Home", 1.2, 0.9, 0.8, 1.1)
    away = _ts("Away", 1.0, 1.0, 1.1, 0.9)
    probs = blended_match_probs(home, away, 1.4, 1.1)
    assert probs.model == "poisson"
    assert 0.0 <= probs.home_win <= 1.0
    assert 0.0 <= probs.draw <= 1.0
    assert 0.0 <= probs.away_win <= 1.0
    # Probs are rounded to 6 decimals → tolerance must be looser than 1e-6
    assert abs(probs.home_win + probs.draw + probs.away_win - 1.0) < 1e-3


def test_blended_uses_xg_when_present_on_both_sides():
    home = _ts("H", 1.2, 0.9, 0.8, 1.1, xgf=2.1, xga=0.9)
    away = _ts("A", 1.0, 1.0, 1.1, 0.9, xgf=1.0, xga=1.5)
    probs = blended_match_probs(home, away, 1.4, 1.1)
    assert probs.model == "blended"
    # Home's high xG should keep home_win >= away_win
    assert probs.home_win > probs.away_win


def test_blended_elo_pulls_strong_team_higher():
    # Equal stats but Home has +200 ELO → must shift probs toward home
    home_baseline = _ts("H", 1.0, 1.0, 1.0, 1.0)
    away_baseline = _ts("A", 1.0, 1.0, 1.0, 1.0)
    p_baseline = blended_match_probs(home_baseline, away_baseline, 1.4, 1.1)

    home_strong = _ts("H", 1.0, 1.0, 1.0, 1.0, elo=1900)
    away_weak = _ts("A", 1.0, 1.0, 1.0, 1.0, elo=1700)
    p_strong = blended_match_probs(home_strong, away_weak, 1.4, 1.1)

    assert p_strong.home_win > p_baseline.home_win


def test_weather_modifier_dampens_total_goals_equally():
    home = _ts("H", 1.2, 0.9, 0.8, 1.1)
    away = _ts("A", 1.0, 1.0, 1.1, 0.9)
    p_clear = blended_match_probs(home, away, 1.4, 1.1, weather_modifier=1.0)
    p_rainy = blended_match_probs(home, away, 1.4, 1.1, weather_modifier=0.85)
    # Lower λ → more probability on 0-0 / low scores → over_25 must drop
    assert p_rainy.over_25 < p_clear.over_25


def test_weights_validation():
    home = _ts("H", 1.0, 1.0, 1.0, 1.0)
    away = _ts("A", 1.0, 1.0, 1.0, 1.0)
    # elo + xg > 0.95 → no room for Dixon-Coles → must raise
    with pytest.raises(ValueError):
        blended_match_probs(home, away, 1.4, 1.1, elo_weight=0.5, xg_weight=0.5)
