"""Greedy combined-odds builder (×1000 mode).

`target_odds` is a CEILING (max not to exceed), not a floor: the builder returns
the requested number of combos, each stacking as many quality favorites as fit
WITHOUT exceeding the cap — it does not require reaching it.
"""
from betbot.analysis import ValueBet, build_target_parlays


def _bet(event_id: str, odds: float, sport_key: str = "soccer_epl",
         prob: float = 0.5, edge: float = 0.05) -> ValueBet:
    return ValueBet(
        event_id=event_id, sport_key=sport_key, home_team="A", away_team="B",
        league_label="L", market="h2h", selection_code="1", selection_label="x",
        model_prob=prob, best_odds=odds, best_book="b", value_edge=edge,
        kelly_stake=1.0, lambda_home=1.0, lambda_away=1.0, model_type="poisson",
        reliability=1.0,
    )


def test_combo_never_exceeds_the_ceiling():
    # 12 legs at 2.0, ceiling ×1000 : 2^9=512 ≤1000 but 2^10=1024 would overshoot
    # → stops at 9 legs / ×512, never over the cap.
    bets = [_bet(f"e{i}", 2.0) for i in range(12)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=1)
    assert len(parlays) == 1
    assert parlays[0].combined_odds <= 1000.0
    assert len(parlays[0].bets) >= 2


def test_ceiling_caps_growth_below_max_legs():
    # Legs at 3.0, ceiling ×100 : 3^4=81 ≤100, 3^5=243 overshoots → 4 legs/×81,
    # even though max_legs (10) and the pool (10) would allow far more.
    bets = [_bet(f"e{i}", 3.0) for i in range(10)]
    parlays = build_target_parlays(bets, target_odds=100.0, max_legs=10, top_n=1)
    assert len(parlays) == 1
    assert parlays[0].combined_odds <= 100.0
    assert len(parlays[0].bets) == 4


def test_returns_best_effort_below_target():
    # odds 1.5, max 5 legs → 1.5^5 ≈ 7.59, far below ×1000. The OLD builder
    # returned []; now it returns the best achievable combo (cap not a floor).
    bets = [_bet(f"e{i}", 1.5) for i in range(20)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=5, top_n=1)
    assert len(parlays) == 1
    assert parlays[0].combined_odds <= 1000.0
    assert len(parlays[0].bets) == 5


def test_returns_requested_number_of_combos():
    bets = [_bet(f"e{i}", 2.0) for i in range(30)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=3)
    assert len(parlays) == 3
    assert all(p.combined_odds <= 1000.0 for p in parlays)


def test_parlays_are_event_disjoint():
    bets = [_bet(f"e{i}", 2.0) for i in range(30)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=2)
    assert len(parlays) == 2
    ev0 = {b.event_id for b in parlays[0].bets}
    ev1 = {b.event_id for b in parlays[1].bets}
    assert ev0.isdisjoint(ev1)


def test_min_leg_odds_filters_the_pool():
    bets = [_bet(f"e{i}", 1.1) for i in range(20)]  # all below min_leg_odds
    assert build_target_parlays(bets, target_odds=1000.0, max_legs=15,
                                min_leg_odds=1.2, top_n=1) == []


def test_too_few_legs_returns_empty():
    # A single eligible leg can't form a ≥2-leg combiné.
    assert build_target_parlays([_bet("solo", 2.0)], target_odds=1000.0, top_n=1) == []


def test_same_league_parlay_flagged_correlated():
    bets = [_bet(f"e{i}", 2.0, sport_key="soccer_epl") for i in range(12)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=1)
    assert parlays[0].correlated is True


def test_leg_that_would_overshoot_is_skipped():
    # A fat leg whose odds alone exceed the ceiling must never be used — the
    # combo is built from the favorites and stays under the cap.
    bets = [_bet("fat", 1500.0, edge=0.40)] + [_bet(f"e{i}", 2.0) for i in range(12)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=1)
    assert len(parlays) == 1
    used = {b.event_id for b in parlays[0].bets}
    assert "fat" not in used
    assert parlays[0].combined_odds <= 1000.0
    assert len(parlays[0].bets) >= 2


# ── Robustesse : favoris empilés, garde EV ──────────────────────────────────

def test_max_leg_odds_excludes_longshots():
    longshots = [_bet(f"L{i}", 12.0, prob=0.10) for i in range(5)]
    favorites = [_bet(f"f{i}", 2.0, prob=0.55, edge=0.10) for i in range(14)]
    parlays = build_target_parlays(longshots + favorites, target_odds=1000.0,
                                   max_legs=15, top_n=1, max_leg_odds=2.5)
    assert len(parlays) == 1
    assert all(b.best_odds <= 2.5 for b in parlays[0].bets)   # zero longshots used


def test_require_positive_ev_skips_negative_ev_combo():
    # Fair legs (prob×odds = 1) in the SAME league → correlation haircut drags
    # combined EV below 0 → with the gate ON, nothing is emitted...
    fair = [_bet(f"e{i}", 2.0, prob=0.5, edge=0.0) for i in range(14)]
    assert build_target_parlays(fair, target_odds=1000.0, max_legs=15, top_n=1,
                                require_positive_ev=True) == []
    # ...but the gate defaults OFF, so the legacy builder still emits it.
    assert len(build_target_parlays(fair, target_odds=1000.0, max_legs=15, top_n=1)) == 1


def test_positive_ev_combo_is_emitted_with_gate():
    # Real per-leg edges across DISTINCT leagues (no haircut) → combined EV > 0
    # → emitted even with the honesty gate on, and still under the ceiling.
    edged = [_bet(f"e{i}", 2.0, sport_key=f"lg{i}", prob=0.55, edge=0.10) for i in range(14)]
    parlays = build_target_parlays(edged, target_odds=1000.0, max_legs=15, top_n=1,
                                   require_positive_ev=True)
    assert len(parlays) == 1
    assert parlays[0].combined_ev > 0
    assert parlays[0].combined_odds <= 1000.0
