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
    # Testé sur la fonction pure : depuis qu'une seule option par famille est
    # émise, un seul double chance sort du détecteur — mais c'est l'arithmétique
    # qu'on vérifie ici, et elle doit tenir sur les trois.
    from betbot.blind import _h2h_options
    p = _probs(home=0.72, draw=0.18, away=0.10)
    dc = {o[0]: o[4] for o in _h2h_options(p, "Alpha FC", "Beta FC")}
    assert dc["1X"] == pytest.approx(0.90)
    assert dc["X2"] == pytest.approx(0.28)
    assert dc["12"] == pytest.approx(0.82)
    # Draw No Bet : reconditionné sur les cas tranchés, sans aucun prix.
    assert dc["DNB1"] == pytest.approx(0.72 / 0.82)

    # Et le pick réellement émis porte bien cette valeur, pas une valeur
    # reconstruite depuis la cote de 1,30 que porte l'événement.
    patched(p)
    emis = {b.selection_code: b.model_prob
            for b in detect_blind_picks({"soccer_epl": [_event()]},
                                        min_prob=0.0, max_per_match=50)}
    assert emis["1X"] == pytest.approx(0.90, abs=1e-4)


def test_markets_no_bookmaker_quotes_are_still_emitted(patched):
    """Le cœur de sa consigne : « peu importe si ces options ne sont pas
    disponibles pour le pari ». BTTS n'a jamais eu ses cotes demandées à l'API,
    et la ligne 1,5 est absente du flux — les deux doivent sortir."""
    patched(_probs())
    picks = detect_blind_picks({"soccer_epl": [_event()]},
                               min_prob=0.0, max_per_match=50)
    familles = {b.market.split("_")[0] for b in picks}
    # BTTS : ses cotes n'ont jamais été demandées à l'API. totals : la ligne 1,5
    # est absente du flux et Betclic n'y propose aucun total. Les deux familles
    # doivent tout de même produire un pronostic.
    assert "btts" in familles
    assert "totals" in familles


def test_one_match_does_not_flood_the_list(patched):
    """Huit variantes de totals sur le même match ne sont pas huit pronostics,
    c'est le même énoncé huit fois — et elles sont corrélées entre elles."""
    patched(_probs())
    toutes = detect_blind_picks({"soccer_epl": [_event()]},
                                min_prob=0.0, max_per_match=50)
    # Sans plafond : au plus une option par famille, jamais huit variantes de
    # totals corrélées entre elles.
    assert len(toutes) == len({b.market.split("_")[0] for b in toutes})
    # Avec le plafond par défaut, le bulletin reste court. Le nombre est lu
    # dans la signature plutôt que recopié : ce test figeait 3, le défaut est
    # passé à 4, et il a cassé pour une raison qui n'était pas un défaut.
    import inspect
    defaut = inspect.signature(detect_blind_picks).parameters["max_per_match"].default
    assert len(detect_blind_picks({"soccer_epl": [_event()]}, min_prob=0.0)) == defaut


def test_the_floor_is_the_only_selection_criterion(patched):
    patched(_probs())
    picks = detect_blind_picks({"soccer_epl": [_event()]},
                               min_prob=0.85, max_per_match=50)
    assert picks and all(b.model_prob >= 0.85 for b in picks)


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


# ---------------------------------------------------------------------------
# Les deux défauts trouvés au premier scan réel (2026-09-13)
# ---------------------------------------------------------------------------

def test_probabilities_leave_as_plain_python_floats(patched):
    """numpy.float64 n'est PAS adapté par psycopg2 : il le sérialise en
    « np.float64(0.91) » dans le SQL, Postgres y lit un schéma nommé « np » et
    refuse l'insertion. Mesuré en production : le canal a tourné et n'a rien
    enregistré, chaque écriture perdue dans un log d'erreur.

    Les autres canaux y échappent parce que leurs probabilités traversent le
    calibrateur, qui les repasse en float Python. Celui-ci ne le traverse pas —
    donc la conversion doit être explicite et le rester."""
    np = pytest.importorskip("numpy")
    patched(MatchProbs(
        home_win=np.float64(0.76), draw=np.float64(0.15), away_win=np.float64(0.09),
        over_25=np.float64(0.55), under_25=np.float64(0.45),
        over_15=np.float64(0.80), under_15=np.float64(0.20),
        btts_yes=np.float64(0.52), btts_no=np.float64(0.48),
        lambda_home=np.float64(1.7), lambda_away=np.float64(0.9), model="blended",
    ))
    for b in detect_blind_picks({"soccer_epl": [_event()]},
                                min_prob=0.0, max_per_match=50):
        assert type(b.model_prob) is float, type(b.model_prob)
        assert type(b.lambda_home) is float
        assert type(b.lambda_away) is float


def test_each_market_family_competes_against_itself(patched):
    """« Plus de 0,5 but » vaut ~0,91 dans TOUS les matchs : ce n'est pas une
    prédiction sur ce match-ci, c'est un fait sur le football. Classées ensemble,
    les options laissaient O05 gagner partout — au premier scan réel la quasi-
    totalité des picks émis étaient O05, et le 1X2 n'apparaissait jamais."""
    patched(_probs(home=0.76, draw=0.15, away=0.09))   # O05 = 0.93 > tout le reste
    picks = detect_blind_picks({"soccer_epl": [_event()]},
                               min_prob=0.70, max_per_match=50)
    familles = {b.market.split("_")[0] for b in picks}
    assert "h2h" in familles, "le 1X2 a été écrasé par un marché trivial"
    # Une seule option par famille : pas huit variantes de totals corrélées.
    par_famille = [b.market for b in picks]
    assert len(par_famille) == len(set(par_famille))


def test_the_half_goal_line_is_out_by_default_and_can_come_back(patched):
    """« Plus de 0,5 but » vaut ~0,91 dans presque tous les matchs : elle ne
    distingue rien et raflait la quasi-totalité des pronostics au premier scan
    réel. Écartée par défaut — mais c'est un jugement, pas une loi, donc le
    drapeau doit vraiment la ramener."""
    patched(_probs())
    defaut = {b.selection_code for b in
              detect_blind_picks({"soccer_epl": [_event()]},
                                 min_prob=0.0, max_per_match=50)}
    assert "O05" not in defaut and "U05" not in defaut

    avec = {b.selection_code for b in
            detect_blind_picks({"soccer_epl": [_event()]}, min_prob=0.0,
                               max_per_match=50, include_half_line=True)}
    assert "O05" in avec


def test_the_winner_call_survives_the_default_cap(patched):
    """Le défaut doit produire un bulletin utilisable : un avis sur le vainqueur
    en fait partie. Avec un seul pronostic par match, un marché de buts le
    supprimait systématiquement."""
    patched(_probs(home=0.76, draw=0.15, away=0.09))
    picks = detect_blind_picks({"soccer_epl": [_event()]}, min_prob=0.70)
    assert any(b.market == "h2h" for b in picks), \
        [(b.market, b.model_prob) for b in picks]


# ---------------------------------------------------------------------------
# Couverture : un pronostic par match, toujours
# ---------------------------------------------------------------------------

def test_every_modelled_match_gets_its_pronostic(patched):
    """Sa précision du 2026-09-13 : « s'il y a 100 matchs à jouer, tu devrais
    pouvoir me donner 100 pronostics ». Aucun plancher de probabilité ne doit
    pouvoir faire disparaître une affiche — un plancher supprime des MATCHS
    quand il devrait au pire supprimer des options."""
    patched(_probs(home=0.34, draw=0.33, away=0.33))   # match parfaitement ouvert
    evts = [dict(_event(f"e{i}")) for i in range(100)]
    picks = detect_blind_picks({"soccer_epl": evts})
    assert len({b.event_id for b in picks}) == 100
    # « au moins 3 ou 4 options de paris par match »
    for eid in {b.event_id for b in picks}:
        assert len([b for b in picks if b.event_id == eid]) >= 3


def test_capping_keeps_whole_matches_never_half_of_one(patched):
    """« À la rigueur, tu pourrais sélectionner les 100 matchs les plus
    privilégiés. » Trancher dans la liste à plat laisserait des affiches avec
    deux options sur quatre — à moitié analysées, donc pires qu'absentes."""
    patched(_probs())
    evts = [dict(_event(f"e{i}")) for i in range(10)]
    picks = detect_blind_picks({"soccer_epl": evts}, top_matches=3)
    par_match = {}
    for b in picks:
        par_match.setdefault(b.event_id, []).append(b)
    assert len(par_match) == 3
    tailles = {len(v) for v in par_match.values()}
    assert len(tailles) == 1, f"matchs tronqués : {tailles}"


def test_the_kept_matches_are_the_most_confident_ones(monkeypatch):
    """« les plus privilégiés » = ceux sur lesquels le modèle est le plus sûr,
    pas les premiers rencontrés."""
    import betbot.blind as blind
    sûrs = {"e_sur"}
    monkeypatch.setattr(
        blind, "_compute_probs",
        lambda h, a, e, *args, **k: _probs(home=0.92, draw=0.05, away=0.03)
        if e.get("id") in sûrs else _probs(home=0.34, draw=0.33, away=0.33))
    evts = [dict(_event("e_ouvert")), dict(_event("e_sur")), dict(_event("e_ouvert2"))]
    picks = blind.detect_blind_picks({"soccer_epl": evts}, top_matches=1)
    assert {b.event_id for b in picks} == {"e_sur"}
