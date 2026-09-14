"""
Combinés du canal aveugle : assemblés sur les cotes justes du modèle, jamais sur
un prix de bookmaker.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from betbot.analysis import ValueBet
from betbot.blind_parlays import MIN_LEG_PROB, build_blind_parlays

_SRC = Path("betbot/blind_parlays.py").read_text(encoding="utf-8")
_USED = {n.id for n in ast.walk(ast.parse(_SRC)) if isinstance(n, ast.Name)} | {
    n.attr for n in ast.walk(ast.parse(_SRC)) if isinstance(n, ast.Attribute)}


def _leg(eid: str, prob: float) -> ValueBet:
    return ValueBet(
        event_id=eid, sport_key="soccer_epl", home_team=f"H{eid}", away_team=f"A{eid}",
        league_label="EPL", market="h2h", selection_code="1",
        selection_label="Victoire domicile", model_prob=prob,
        best_odds=0.0, best_book="", value_edge=0.0, kelly_stake=0.0,
        lambda_home=1.5, lambda_away=1.0, model_type="blended",
        commence_time="2026-09-15T18:00:00Z", channel="modele",
    )


def _pool(n: int, prob: float = 0.75):
    return [_leg(f"e{i}", prob) for i in range(n)]


@pytest.mark.parametrize("forbidden", ["extract_best_odds", "shrink_toward_market",
                                       "_derive_dc_dnb_odds", "_novig_fair_prob"])
def test_no_bookmaker_price_enters_the_combo(forbidden):
    assert forbidden not in _USED


def test_the_multiplier_and_the_chances_are_two_faces_of_one_number():
    """C'EST LE TEST QUI COMPTE. Si le produit des cotes justes vaut 100, le
    produit des probabilités vaut exactement 1/100 : un ×100 honnête gagne une
    fois sur cent, par construction. Aucune qualité de modèle ne change cette
    arithmétique, et un combiné qui prétendrait le contraire mentirait."""
    for c in build_blind_parlays(_pool(40), target_odds=100.0, n_combos=3):
        assert c.win_prob == pytest.approx(1.0 / c.fair_odds, rel=1e-3)


def test_a_match_never_appears_twice():
    """Deux options d'un même match se recouvrent, et réutiliser un match d'un
    ticket à l'autre ferait tomber ou gagner les trois ensemble — ce qui annule
    la diversification qu'on croyait acheter en en prenant trois."""
    combos = build_blind_parlays(_pool(60), n_combos=3)
    assert len(combos) == 3
    vus = [b.event_id for c in combos for b in c.legs]
    assert len(vus) == len(set(vus))


def test_two_options_of_the_same_match_cannot_both_be_legs():
    doublon = [_leg("e1", 0.80), _leg("e1", 0.72), _leg("e2", 0.70), _leg("e3", 0.70)]
    combos = build_blind_parlays(doublon, target_odds=2.0, n_combos=1)
    ids = [b.event_id for b in combos[0].legs]
    assert len(ids) == len(set(ids))


def test_legs_below_the_floor_are_refused():
    """Sous 0,60 le modèle annonçait 63,7 % et réalisait 43,7 %. Une jambe de
    cette zone ne rend pas le ticket plus gros, elle le rend faux."""
    combos = build_blind_parlays(_pool(30, prob=0.45), n_combos=3)
    assert combos == []
    melange = _pool(20, 0.75) + [_leg("bas", 0.40)]
    for c in build_blind_parlays(melange, n_combos=1):
        assert all(b.model_prob >= MIN_LEG_PROB for b in c.legs)


def test_a_thin_pool_returns_fewer_combos_never_looser_ones():
    """L'ancienne échelle de relâchement complétait les combinés en abaissant
    les seuils jusqu'à 0,30 — et a coûté de l'argent réel pendant la trêve de
    septembre. Moins de tickets, jamais des tickets plus lâches."""
    combos = build_blind_parlays(_pool(6), target_odds=100.0, n_combos=3)
    assert len(combos) < 3
    for c in combos:
        assert all(b.model_prob >= MIN_LEG_PROB for b in c.legs)


def test_the_most_trustworthy_legs_are_used_first():
    """À produit de probabilités égal, vingt jambes sûres valent mieux que huit
    douteuses : le modèle est calibré au-dessus de 0,70 et s'effondre sous 0,60."""
    pool = [_leg("sur", 0.85), _leg("moyen", 0.70), _leg("limite", 0.62)]
    c = build_blind_parlays(pool, target_odds=1.5, n_combos=1)[0]
    assert c.legs[0].event_id == "sur"


def test_an_unreachable_target_is_reported_not_hidden():
    combos = build_blind_parlays(_pool(3, 0.90), target_odds=1000.0, n_combos=1)
    assert combos and combos[0].reached_target is False


def test_the_target_is_actually_reachable_on_a_realistic_pool():
    """Le constructeur prenait l'option la PLUS PROBABLE de chaque match — donc
    la cote juste la plus BASSE, donc la plus dure à empiler. Mesuré sur le
    vivier réel : des jambes à 1,15 auraient exigé 33 jambes pour ×100, un
    ticket impossible à placer. Viser une probabilité PAR JAMBE est ce qui rend
    la cible atteignable.

    Le vivier imite le vrai : chaque match offre un double chance très probable,
    un Over, un vainqueur sec et un BTTS."""
    pool = []
    for i in range(60):
        for p, code in ((0.90, "1X"), (0.78, "O15"), (0.68, "1"), (0.62, "BTTS_O")):
            b = _leg(f"e{i}", p)
            b.selection_code = code
            pool.append(b)
    combos = build_blind_parlays(pool, target_odds=100.0, n_combos=3)
    assert len(combos) == 3
    for c in combos:
        assert c.reached_target, f"×{c.fair_odds} sur {c.n_legs if hasattr(c,'n_legs') else len(c.legs)} jambes"
        assert len(c.legs) <= 20, "un ticket de plus de 20 lignes ne se place pas"
        assert c.win_prob == pytest.approx(1.0 / c.fair_odds, rel=1e-3)


def test_the_floor_still_wins_over_the_target():
    """Viser une probabilité par jambe ne doit jamais servir de prétexte à
    descendre sous le plancher : une cible très haute demanderait des jambes
    très incertaines, et c'est le plancher qui tranche."""
    pool = []
    for i in range(40):
        for p in (0.95, 0.75, 0.61):
            pool.append(_leg(f"e{i}", p))
    for c in build_blind_parlays(pool, target_odds=100000.0, n_combos=1):
        assert all(b.model_prob >= MIN_LEG_PROB for b in c.legs)
