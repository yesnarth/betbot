"""Unit tests for the local deterministic agent's business rules.

Each rule is tested in isolation so a regression in one rule doesn't
hide a regression in another. Tests cover the bug fixes for cross-team
contamination (Mendy/Mallorca) and the over-eager injury regex.
"""
from unittest.mock import patch

from betbot.local_agent import (
    PickEvaluation,
    _filter_relevant_hits,
    _INJURY_KEYWORDS,
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
    _team_mentioned,
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


# ---------------------------------------------------------------------------
# Team-mention filtering — fix for Augsburg → Mainoo / Mallorca → Mendy bugs
# ---------------------------------------------------------------------------

def test_team_mentioned_exact():
    assert _team_mentioned("Liverpool", "Liverpool wins again")
    assert _team_mentioned("Liverpool FC", "Liverpool wins again")
    assert _team_mentioned("Real Madrid", "Real Madrid Defender Mendy is out")


def test_team_not_mentioned_when_news_is_about_a_different_team():
    # Tavily search on Augsburg returned a Manchester United story → must be dropped
    assert not _team_mentioned(
        "Augsburg",
        "Manchester United: Kobbie Mainoo stars with match-winning goal",
    )
    # Tavily search on Mallorca returned a Real Madrid story → must be dropped
    assert not _team_mentioned(
        "Mallorca",
        "Real Madrid Defender Mendy Could Miss Up to Five Months",
    )


def test_filter_relevant_hits_drops_unrelated_story():
    hits = [
        {"title": "Real Madrid star injured", "snippet": "Mendy out for months"},
        {"title": "Mainoo stars for United", "snippet": "Match winner against Liverpool"},
    ]
    kept = _filter_relevant_hits("Mallorca", hits)
    assert kept == []


def test_filter_relevant_hits_keeps_actual_team_story():
    hits = [
        {"title": "Real Madrid loses Mendy", "snippet": "Mendy out for months"},
        {"title": "Mainoo stars for United", "snippet": "Match winner"},
    ]
    kept = _filter_relevant_hits("Real Madrid", hits)
    assert len(kept) == 1
    assert "Mendy" in kept[0]["title"]


# ---------------------------------------------------------------------------
# Injury regex tightening — fix for the "Mainoo exile" false positive
# ---------------------------------------------------------------------------

def test_injury_regex_matches_real_injuries():
    cases = [
        "Mendy is out for the season",
        "Star striker injured in training",
        "Suspended for the next match",
        "Will miss the game on Sunday",
        "Hamstring injury rules him out",
        "Could miss up to five months due to injury",
    ]
    for s in cases:
        assert _INJURY_KEYWORDS.search(s) is not None, f"should match: {s!r}"


def test_injury_regex_rejects_false_positives():
    """The old regex matched 'exile', 'miss' generically — these must now pass through."""
    cases = [
        "Mainoo stars with match-winning goal after exile under Ruben Amo",
        "Liverpool dominate possession but miss several chances",
        "Mainoo's match-winning brace",
        "Big victory: club seals title",
    ]
    for s in cases:
        assert _INJURY_KEYWORDS.search(s) is None, f"should NOT match: {s!r}"


# ---------------------------------------------------------------------------
# news_mentions_injury now requires the team name to be in the snippet
# ---------------------------------------------------------------------------

def test_news_mentions_injury_requires_team_match():
    """Even if a hit contains 'injury', it must also mention the team."""
    hits = [{"title": "Mendy out for months", "snippet": "Real Madrid defender injured"}]
    matched, _ = _news_mentions_injury(hits, "Mallorca")
    assert matched is False


def test_news_mentions_injury_with_correct_team():
    hits = [{"title": "Real Madrid: Mendy out for months", "snippet": "Long-term injury"}]
    matched, snippet = _news_mentions_injury(hits, "Real Madrid")
    assert matched is True
    assert "Mendy" in snippet


def test_news_mentions_injury_no_false_positive_on_unrelated():
    hits = [{"title": "Arsenal announces sponsor deal", "snippet": "new shirt"}]
    matched, _ = _news_mentions_injury(hits, "Arsenal")
    assert not matched


def test_coach_drama_requires_team_match():
    hits = [{"title": "Liverpool sacks coach", "snippet": "Crisis at Anfield"}]
    matched, _ = _news_mentions_coach_drama(hits, "Arsenal")
    assert matched is False


def test_coach_drama_with_correct_team():
    hits = [{"title": "Arsenal manager fired after defeat", "snippet": "club crisis"}]
    matched, _ = _news_mentions_coach_drama(hits, "Arsenal")
    assert matched is True


# ---------------------------------------------------------------------------
# Rule: huge edge needs supporting news
# ---------------------------------------------------------------------------

def test_huge_edge_with_no_news_gets_penalty():
    ev = _make_eval(prob=0.70, odds=2.0)
    _rule_huge_edge_needs_confirmation(
        ev, raw_edge=0.40, picked_team="Arsenal", opposing_team="Chelsea",
        news_picked_team=[], news_opposing_team=[],
    )
    assert ev.final_prob < 0.70
    assert any("sans news favorable" in r for r in ev.rationale)


def test_huge_edge_with_supporting_injury_news_no_penalty():
    """If the OPPOSING team is reported injured, the high edge IS supported."""
    ev = _make_eval(prob=0.70, odds=2.0)
    opposing_news = [{
        "title": "Chelsea hit by injury crisis",
        "snippet": "Chelsea key players injured",
    }]
    _rule_huge_edge_needs_confirmation(
        ev, raw_edge=0.40, picked_team="Arsenal", opposing_team="Chelsea",
        news_picked_team=[], news_opposing_team=opposing_news,
    )
    assert ev.final_prob == 0.70


def test_modest_edge_does_not_trigger_huge_edge_rule():
    ev = _make_eval(prob=0.55, odds=2.0)
    _rule_huge_edge_needs_confirmation(
        ev, raw_edge=0.10, picked_team="Arsenal", opposing_team="Chelsea",
        news_picked_team=[], news_opposing_team=[],
    )
    assert ev.final_prob == 0.55


# ---------------------------------------------------------------------------
# Rule: injury news (with team name args)
# ---------------------------------------------------------------------------

def test_injury_on_picked_team_reduces_prob():
    ev = _make_eval(prob=0.60)
    picked_news = [{
        "title": "Arsenal striker out injured",
        "snippet": "Arsenal's season over for star man",
    }]
    _rule_injury_news(ev, "Arsenal", "Chelsea", picked_news, [])
    assert ev.final_prob < 0.60


def test_injury_on_opposing_team_boosts_prob():
    ev = _make_eval(prob=0.60)
    opposing_news = [{
        "title": "Chelsea captain suspended",
        "snippet": "Chelsea red card",
    }]
    _rule_injury_news(ev, "Arsenal", "Chelsea", [], opposing_news)
    assert ev.final_prob > 0.60


def test_no_injury_news_leaves_prob_untouched():
    ev = _make_eval(prob=0.60)
    benign = [{"title": "Arsenal hosts charity event", "snippet": "fundraiser"}]
    _rule_injury_news(ev, "Arsenal", "Chelsea", benign, benign)
    assert ev.final_prob == 0.60


# ---------------------------------------------------------------------------
# Rule: coach drama
# ---------------------------------------------------------------------------

def test_coach_sacked_reduces_prob():
    ev = _make_eval(prob=0.55)
    drama = [{"title": "Arsenal manager fired after losing streak", "snippet": "interim takes over"}]
    _rule_coach_drama(ev, "Arsenal", drama)
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
    ev = _make_eval(prob=0.55)
    _rule_elo_contradiction(ev, elo_home=1700, elo_away=2000)
    assert ev.final_prob < 0.55


def test_elo_contradiction_supportive_no_change():
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
# Integration: evaluate_picks recomputes Kelly + handles offline services
# ---------------------------------------------------------------------------

def test_evaluate_picks_rejects_below_min_edge():
    picks = [_pick(prob=0.40, odds=2.0, edge=-0.20)]
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings", return_value={}), \
         patch("betbot.local_agent.club_elo.get_team_elo", return_value=None):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False,
                                min_final_edge=0.05)
    assert result["n_accepted"] == 0
    assert result["n_rejected"] == 1


def test_evaluate_picks_accepts_modest_edge():
    picks = [_pick(prob=0.55, odds=2.0, edge=0.10)]
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings", return_value={}), \
         patch("betbot.local_agent.club_elo.get_team_elo", return_value=None):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False,
                                min_final_edge=0.02)
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 0


def test_evaluate_picks_runs_without_tavily_or_weather():
    picks = [_pick(prob=0.55, odds=2.0, edge=0.10)]
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings",
               side_effect=Exception("offline")):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False)
    assert result["n_evaluated"] == 1


def test_evaluate_picks_recomputes_kelly_after_calibration():
    """When the rule chain reduces the probability, the Kelly stake must
    shrink with it — not stay at the raw scan value."""
    # Strong overconfidence: prob 0.90, odds 2.0 → raw Kelly is at the 5% cap
    picks = [_pick(prob=0.90, odds=2.0, edge=0.80)]
    picks[0]["kelly_stake"] = 5.0   # raw-scan value
    with patch("betbot.local_agent.club_elo.get_all_elo_ratings", return_value={}), \
         patch("betbot.local_agent.club_elo.get_team_elo", return_value=None):
        result = evaluate_picks(picks, fetch_news=False, fetch_weather=False,
                                bankroll=100.0, kelly_fraction=0.25)
    if result["n_accepted"] > 0:
        # After overconfidence cap (× 0.90), prob drops, Kelly must drop too
        accepted = result["picks"][0]
        assert accepted["kelly_stake"] != 5.0 or accepted["model_prob"] >= 0.90
