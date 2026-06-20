"""Greedy target-odds parlay builder (×1000 mode) — Vague 4, item 4.2."""
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


def test_reaches_target_with_enough_legs():
    # 12 candidate legs at odds 2.0 → 2^10 = 1024 ≥ 1000 after 10 legs.
    bets = [_bet(f"e{i}", 2.0) for i in range(12)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=1)
    assert len(parlays) == 1
    assert parlays[0].combined_odds >= 1000.0
    assert len(parlays[0].bets) == 10


def test_returns_empty_when_target_unreachable():
    # odds 1.5, max 5 legs → 1.5^5 ≈ 7.6, far below ×1000 → nothing emitted.
    bets = [_bet(f"e{i}", 1.5) for i in range(20)]
    assert build_target_parlays(bets, target_odds=1000.0, max_legs=5, top_n=1) == []


def test_parlays_are_event_disjoint():
    bets = [_bet(f"e{i}", 2.0) for i in range(20)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=2)
    assert len(parlays) == 2
    ev0 = {b.event_id for b in parlays[0].bets}
    ev1 = {b.event_id for b in parlays[1].bets}
    assert ev0.isdisjoint(ev1)


def test_min_leg_odds_filters_the_pool():
    bets = [_bet(f"e{i}", 1.1) for i in range(20)]  # all below min_leg_odds
    assert build_target_parlays(bets, target_odds=1000.0, max_legs=15,
                                min_leg_odds=1.2, top_n=1) == []


def test_same_league_parlay_flagged_correlated():
    bets = [_bet(f"e{i}", 2.0, sport_key="soccer_epl") for i in range(12)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=15, top_n=1)
    assert parlays[0].correlated is True


def test_single_fat_leg_does_not_abort_build():
    # Regression (audit C1): a lone leg whose odds alone exceed the target used
    # to abort the WHOLE build (break). It must still produce ≥2-leg combos.
    bets = [_bet("fat", 1500.0, edge=0.40)] + [_bet(f"e{i}", 2.0) for i in range(12)]
    parlays = build_target_parlays(bets, target_odds=1000.0, max_legs=12, top_n=2)
    assert len(parlays) >= 1
    assert all(len(p.bets) >= 2 for p in parlays)          # never a 1-leg "combiné"
    assert all(p.combined_odds >= 1000.0 for p in parlays)  # all genuinely reach target


# ── Robustesse : atteindre la cible avec des FAVORIS, pas des longshots ──────

def test_max_leg_odds_excludes_longshots():
    # A pool of fat longshots (odds 12) + many favorites (odds 2.0). With a leg
    # cap at 2.5, the target is reached using ONLY favorites — no longshot legs.
    longshots = [_bet(f"L{i}", 12.0, prob=0.10) for i in range(5)]
    favorites = [_bet(f"f{i}", 2.0, prob=0.55, edge=0.10) for i in range(14)]
    parlays = build_target_parlays(longshots + favorites, target_odds=1000.0,
                                   max_legs=15, top_n=1, max_leg_odds=2.5)
    assert len(parlays) == 1
    assert all(b.best_odds <= 2.5 for b in parlays[0].bets)   # zero longshots used
    assert len(parlays[0].bets) >= 10                          # reached via MORE legs


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
    # → emitted even with the honesty gate on.
    edged = [_bet(f"e{i}", 2.0, sport_key=f"lg{i}", prob=0.55, edge=0.10) for i in range(14)]
    parlays = build_target_parlays(edged, target_odds=1000.0, max_legs=15, top_n=1,
                                   require_positive_ev=True)
    assert len(parlays) == 1
    assert parlays[0].combined_ev > 0
