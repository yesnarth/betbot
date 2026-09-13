"""
Le canal aveugle ne doit jamais consulter une cote.

Sa consigne (2026-09-13) : les cotes peuvent être « une fausse piste » ou nous
« manipuler ». Un canal qui promet de les ignorer et en consulte une par un
chemin détourné est pire que pas de canal du tout — il donne la confiance sans
la propriété. Ces tests tiennent la frontière, à la source et au comportement.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from betbot.blind import CHANNEL, detect_blind_picks
from betbot.models import MatchProbs

_SRC = Path("betbot/blind.py").read_text(encoding="utf-8")

# Les identifiants RÉELLEMENT référencés par le code, docstrings et commentaires
# exclus. Le module DOCUMENTE les chemins de cote qu'il coupe — les chercher
# dans le texte brut ferait échouer le test sur sa propre explication.
_TREE = ast.parse(_SRC)
_USED = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {
    n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)} | {
    a.name.split(".")[0] for n in ast.walk(_TREE)
    if isinstance(n, ast.Import) for a in n.names} | {
    a.name for n in ast.walk(_TREE)
    if isinstance(n, ast.ImportFrom) for a in n.names}


def _probs(model="blended", home=0.72, draw=0.18, away=0.10):
    return MatchProbs(
        home_win=home, draw=draw, away_win=away,
        over_25=0.55, under_25=0.45,
        over_05=0.93, under_05=0.07,
        over_15=0.78, under_15=0.22,
        over_35=0.28, under_35=0.72,
        btts_yes=0.52, btts_no=0.48,
        lambda_home=1.7, lambda_away=0.9, model=model,
    )


def _event(eid="e1"):
    return {"id": eid, "home_team": "Alpha FC", "away_team": "Beta FC",
            "commence_time": "2026-09-14T18:00:00Z",
            # Des cotes SONT présentes dans l'événement : si un chemin les lit,
            # les tests de valeur ci-dessous le révèlent.
            "bookmakers": [{"key": "betclic_fr", "title": "Betclic (FR)", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Alpha FC", "price": 1.30},
                    {"name": "Draw", "price": 5.0},
                    {"name": "Beta FC", "price": 9.0}]}]}]}


@pytest.fixture
def patched(monkeypatch):
    def _apply(probs):
        monkeypatch.setattr("betbot.blind._compute_probs",
                            lambda *a, **k: probs)
    return _apply


# ---------------------------------------------------------------------------
# La frontière, à la source
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("forbidden", [
    "extract_best_odds",      # le prix obtenable
    "shrink_toward_market",   # tire la proba du modèle vers le prix
    "_derive_dc_dnb_odds",    # dérive DC/DNB depuis les prix des books
    "_novig_fair_prob",       # la proba dévigée du marché
    "_market_total_lambda",   # le niveau de buts lu depuis le marché
    "consensus_match_probs",  # le modèle qui EST la cote
])
def test_the_blind_channel_never_touches_an_odds_path(forbidden):
    assert forbidden not in _USED, (
        f"{forbidden} est APPELÉ dans blind.py : le canal aveugle regarde une cote"
    )


# ---------------------------------------------------------------------------
# La frontière, en comportement
# ---------------------------------------------------------------------------

def test_a_consensus_fallback_is_dropped_not_repriced():
    """Le modèle consensus EST la cote dévigée. Un match sans stats d'équipe
    n'a pas de pronostic statistique — on se tait plutôt que de faire passer le
    prix du bookmaker pour une prédiction."""
    import betbot.blind as blind
    orig = blind._compute_probs
    blind._compute_probs = lambda *a, **k: _probs(model="consensus")
    try:
        assert detect_blind_picks({"soccer_epl": [_event()]}) == []
    finally:
        blind._compute_probs = orig


def test_double_chance_comes_from_probabilities_not_prices(patched):
    """1X doit valoir EXACTEMENT P(1)+P(X). L'événement porte une cote domicile
    à 1,30 (≈77 %) très différente du modèle (72 %) : si un prix s'était glissé
    dans le calcul, l'égalité ci-dessous casserait."""
    p = _probs(home=0.72, draw=0.18, away=0.10)
    patched(p)
    picks = detect_blind_picks({"soccer_epl": [_event()]},
                               min_prob=0.0, max_per_match=50)
    dc = {b.selection_code: b.model_prob for b in picks}
    assert dc["1X"] == pytest.approx(0.90)
    assert dc["X2"] == pytest.approx(0.28)
    assert dc["12"] == pytest.approx(0.82)
    # Draw No Bet : reconditionné sur les cas tranchés, sans aucun prix.
    # model_prob est arrondi à 4 décimales à l'émission, d'où la tolérance
    # absolue : c'est la précision réellement stockée en base.
    assert dc["DNB1"] == pytest.approx(0.72 / 0.82, abs=1e-4)


def test_markets_no_bookmaker_quotes_are_still_emitted(patched):
    """Le cœur de sa consigne : « peu importe si ces options ne sont pas
    disponibles pour le pari ». BTTS n'a jamais eu ses cotes demandées à l'API,
    et la ligne 1,5 est absente du flux — les deux doivent sortir."""
    patched(_probs())
    codes = {b.selection_code for b in
             detect_blind_picks({"soccer_epl": [_event()]},
                                min_prob=0.0, max_per_match=50)}
    assert "BTTS_O" in codes and "O15" in codes and "O05" in codes


def test_one_match_does_not_flood_the_list(patched):
    """Huit variantes de totals sur le même match ne sont pas huit pronostics,
    c'est le même énoncé huit fois — et elles sont corrélées entre elles."""
    patched(_probs())
    picks = detect_blind_picks({"soccer_epl": [_event()]}, min_prob=0.0)
    assert len(picks) == 1
    assert picks[0].model_prob == max(
        b.model_prob for b in detect_blind_picks(
            {"soccer_epl": [_event()]}, min_prob=0.0, max_per_match=50))


def test_the_floor_is_the_only_selection_criterion(patched):
    patched(_probs())
    picks = detect_blind_picks({"soccer_epl": [_event()]},
                               min_prob=0.90, max_per_match=50)
    assert picks and all(b.model_prob >= 0.90 for b in picks)


def test_emitted_picks_declare_they_carry_no_price(patched):
    """best_odds=0 n'est pas un prix manquant à compléter : c'est la déclaration
    que ce canal n'en dépend pas. Ses statistiques se lisent en taux de
    réussite, jamais en ROI — et un ROI calculé sur 0 serait un mensonge."""
    patched(_probs())
    b = detect_blind_picks({"soccer_epl": [_event()]})[0]
    assert b.channel == CHANNEL
    assert b.best_odds == 0.0 and b.best_book == ""
    assert b.value_edge == 0.0 and b.kelly_stake == 0.0
    assert b.market_prob is None
