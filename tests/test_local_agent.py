"""Unit tests for the local deterministic agent's business rules.

Each rule is tested in isolation so a regression in one rule doesn't
hide a regression in another.
"""
from unittest.mock import patch

from betbot.local_agent import (
    PickEvaluation,
    _news_mentions_coach_drama,
    _news_mentions_injury,
    _opposing_team,
    _rule_bad_weather,
    _rule_coach_drama,
    _rule_elo_contradiction,
    _rule_huge_edge_needs_confirmation,
    _rule_injury_news,
    _rule_overconfidence,
    _selection_team,
    evaluate_picks,
)


def _pick(home="Arsenal", away="Chelsea", code="1", prob=0.50,
          odds=2.0, edge=0.0, label="Victoire domicile") -> dict:
    return {
        "event_id": "test_id",
        "sport_key": "soccer_epl",
        "league": "Premier League",
        "home_team": home,
        "away_team": away,
        "market": "h2h",
        "selection_code": code,
        "selection_label": label,
        "model_prob": prob,
        "best_odds": odds,
        "best_book": "Pinnacle",
        "value_edge": edge,
        "kelly_stake": 1.0,
        "model_type": "blended",
    }


def _make_eval(prob: float, odds: float = 2.0) -> PickEvaluation:
    p = _pick(prob=prob, odds=odds)
    return PickEvaluation(pick=p, final_prob=prob, final_edge=prob * odds - 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_selection_team_returns_home_for_code_1():
    assert _selection_team(_pick(code="1")) == "Arsenal"


def test_selection_team_returns_away_for_code_2():
    assert _selection_team(_pick(code="2")) == "Chelsea"


def test_selection_team_returns_none_for_draw():
    assert _selection_team(_pick(code="X")) is None


def test_opposing_team_inverse_of_selection():
    assert _opposing_team(_pick(code="1")) == "Chelsea"
    assert _opposing_team(_pick(code="2")) == "Arsenal"


def test_news_mentions_injury_detects_keywords():
    hits = [{"title": "Star striker injured", "snippet": "out for 3 weeks"}]
    matched, _ = _news_mentions_injury(hits)
    assert matched


def test_news_mentions_injury_no_false_positive_on_unrelated():
    hits = [{"title": "Team announces sponsor deal", "snippet": "new shirt"}]
    matched, _ = _news_mentions_injury(hits)
    assert not matched


def test_news_mentions_coach_drama():
    hits = [{"title": "Manager sacked after defeat", "snippet": "club crisis"}]
    matched, _ = _news_mentions_coach_drama(hits)
    assert matched


# ---------------------------------------------------------------------------
# Rule: huge edge needs supporting news
# ---------------------------------------------------------------------------

def test_huge_edge_with_no_news_gets_penalty():
    ev = _make_eval(prob=0.70, odds=2.0)  # raw edge = +40%
    _rule_huge_edge_needs_confirmation(ev, raw_edge=0.40, news_picked_team=[],
                                       news_opposing_team=[])
    assert ev.final_prob < 0.70
    assert any("sans news favorable" in r for r in ev.rationale)


def test_huge_edge_with_supporting_injury_news_no_penalty():
    """If the opposing team is reported injured, the high edge IS supported."""
    ev = _make_eval(prob=0.70, odds=2.0)
    opposing_news = [{"title": "Chelsea hit by injury crisis", "snippet": "key players out"}]
    _rule_huge_edge_needs_confirmation(ev, raw_edge=0.40, news_picked_team=[],
                                       news_opposing_team=opposing_news)
    assert ev.final_prob == 0.70  # untouched


def test_modest_edge_does_not_trigger_huge_edge_rule():
    ev = _make_eval(prob=0.55, odds=2.0)  # edge = +10%, below threshold
    _rule_huge_edge_needs_confirmation(ev, raw_edge=0.10, news_picked_team=[],
                                       news_opposing_team=[])
    assert ev.final_prob == 0.55


# ---------------------------------------------------------------------------
# Rule: injury news
# ---------------------------------------------------------------------------

def test_injury_on_picked_team_reduces_prob():
    ev = _make_eval(prob=0.60)
    picked_news = [{"title": "Arsenal striker out injured", "snippet": "season over"}]
    _rule_injury_news(ev, picked_news, [])
    assert ev.final_prob < 0.60


def test_injury_on_opposing_team_boosts_prob():
    ev = _make_eval(prob=0.60)
    opposing_news = [{"title": "Chelsea captain suspended", "snippet": "red card"}]
    _rule_injury_news(ev, [], opposing_news)
    assert ev.final_prob > 0.60


def test_no_injury_news_leaves_prob_untouched():
    ev = _make_eval(prob=0.60)
    benign_news = [{"title": "Stadium hosts charity event", "snippet": "fundraiser"}]
    _rule_injury_news(ev, benign_news, benign_news)
    assert ev.final_prob == 0.60


# ---------------------------------------------------------------------------
# Rule: coach drama
# ---------------------------------------------------------------------------

def test_coach_sacked_flags_pick():
    ev = _make_eval(prob=0.55)
    drama = [{"title": "Manager fired after losing streak", "snippet": "interim takes over"}]
    _rule_coach_drama(ev, drama)
    assert ev.final_prob < 0.55


# ---------------------------------------------------------------------------
# Rule: bad weather on Over picks
# ---------------------------------------------------------------------------

def test_bad_weather_reduces_over_pick():
    ev = _make_eval(prob=0.60)
    ev.pick["selection_label"] = "Plus de 2.5 buts"
    weather = {"will_rain_heavy": True, "is_windy": False,
               "precipitation_mm": 8.0, "wind_kmh": 10.0}
    _rule_bad_weather(ev, weather)
    assert ev.final_prob < 0.60


def test_bad_weather_does_not_affect_h2h_pick():
    ev = _make_eval(prob=0.60)
    # default selection_label is "Victoire domicile" — not an Over pick
    weather = {"will_rain_heavy": True, "is_windy": True,
               "precipitation_mm": 8.0, "wind_kmh": 50.0}
    _rule_bad_weather(ev, weather)
    assert ev.final_prob == 0.60


def test_no_weather_data_no_change():
    ev = _make_eval(prob=0.60)
    ev.pick["selection_label"] = "Plus de 2.5 buts"
    _rule_bad_weather(ev, weather=None)
    assert ev.final_prob == 0.60


# ---------------------------------------------------------------------------
# Rule: ELO contradiction
# ---------------------------------------------------------------------------

def test_elo_contradiction_against_home_pick_reduces_prob():
    """We picked home but away is much stronger on ELO → reduce confidence."""
    ev = _make_eval(prob=0.55)
    _rule_elo_contradiction(ev, elo_home=1700, elo_away=2000)  # gap 300
    assert ev.final_prob < 0.55


def test_elo_contradiction_supportive_no_change():
    """We picked home and home IS stronger on ELO → no penalty."""
    ev = _make_eval(prob=0.55)
    _rule_elo_contradiction(ev, elo_home=2000, elo_away=1700)
    assert ev.final_prob == 0.55


def test_elo_missing_no_change():
    ev = _make_eval(prob=0.55)
    _rule_elo_contradiction(ev, elo_home=None, elo_away=None)
    assert ev.final_prob == 0.55


# ---------------------------------------------------------------------------
# Rule: overconfidence cap
# ---------------------------------------------------------------------------

def test_overconfidence_cap_triggers_above_85_percent():
    ev = _make_eval(prob=0.90)
    _rule_overconfidence(ev, raw_prob=0.90)
    assert ev.final_prob < 0.90


def test_overconfidence_cap_does_not_trigger_below_85():
    ev = _make_eval(prob=0.80)
    _rule_overconfidence(ev, raw_prob=0.80)
    assert ev.final_prob == 0.80


# ---------------------------------------------------------------------------
# Integration: evaluate_picks with mocked external services
# ---------------------------------------------------------------------------

def test_evaluate_picks_rejects_below_min_edge():
    """A pick whose final edge falls below min_final_edge must be rejected."""
    picks = [_pick(prob=0.40, odds=2.0, edge=-0.20)]   # raw edge = -20%
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings", return_value={}), \
         patch("betbot.local_agent.club_elo.get_team_elo", return_value=None):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False,
                                min_final_edge=0.05)
    assert result["n_accepted"] == 0
    assert result["n_rejected"] == 1


def test_evaluate_picks_accepts_modest_edge():
    """A clean pick at +5% edge with no contradicting signals must pass."""
    picks = [_pick(prob=0.55, odds=2.0, edge=0.10)]   # raw edge = +10%
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings", return_value={}), \
         patch("betbot.local_agent.club_elo.get_team_elo", return_value=None):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False,
                                min_final_edge=0.02)
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 0


def test_evaluate_picks_runs_without_tavily_or_weather():
    """The agent must work even when no external services are configured."""
    picks = [_pick(prob=0.55, odds=2.0, edge=0.10)]
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings",
               side_effect=Exception("offline")):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False)
    assert result["n_evaluated"] == 1
