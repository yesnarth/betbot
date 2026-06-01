"""Parlay correlation haircut — Wave 3, item 3.2.

Legs from the same league (same day) aren't fully independent, so build_parlays
applies a conservative EV haircut and flags the parlay as correlated.
"""
from betbot.analysis import CORRELATION_HAIRCUT, ValueBet, build_parlays


def _bet(event_id: str, sport_key: str, odds: float = 2.0, prob: float = 0.55) -> ValueBet:
    return ValueBet(
        event_id=event_id, sport_key=sport_key, home_team="A", away_team="B",
        league_label="L", market="h2h", selection_code="1", selection_label="x",
        model_prob=prob, best_odds=odds, best_book="b", value_edge=0.10,
        kelly_stake=1.0, lambda_home=1.0, lambda_away=1.0, model_type="poisson",
    )


def test_same_league_parlay_is_flagged_and_haircut():
    bets = [_bet("e1", "soccer_epl"), _bet("e2", "soccer_epl"), _bet("e3", "soccer_epl")]
    parlays = build_parlays(bets, n_legs=3, top_n=1, min_combined_odds=1.0)
    assert len(parlays) == 1
    p = parlays[0]
    assert p.correlated is True
    # 3 legs, 1 distinct league → 2 "extra" correlated legs → haircut^2
    expected = round(0.55 ** 3 * (CORRELATION_HAIRCUT ** 2), 4)
    assert p.combined_prob == expected


def test_distinct_leagues_parlay_not_correlated():
    bets = [
        _bet("e1", "soccer_epl"),
        _bet("e2", "soccer_spain_la_liga"),
        _bet("e3", "soccer_italy_serie_a"),
    ]
    parlays = build_parlays(bets, n_legs=3, top_n=1, min_combined_odds=1.0)
    assert len(parlays) == 1
    p = parlays[0]
    assert p.correlated is False
    assert p.combined_prob == round(0.55 ** 3, 4)   # no haircut


def test_haircut_lowers_ev_vs_independent():
    same = build_parlays(
        [_bet("e1", "soccer_epl"), _bet("e2", "soccer_epl"), _bet("e3", "soccer_epl")],
        n_legs=3, top_n=1, min_combined_odds=1.0,
    )[0]
    diff = build_parlays(
        [_bet("e1", "soccer_epl"), _bet("e2", "soccer_spain_la_liga"), _bet("e3", "soccer_italy_serie_a")],
        n_legs=3, top_n=1, min_combined_odds=1.0,
    )[0]
    # Same odds, but the correlated parlay's EV is discounted.
    assert same.combined_ev < diff.combined_ev
