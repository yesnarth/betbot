"""
Les deux signaux ajoutés le 2026-09-13 à sa demande : forme sur les résultats,
et importance du joueur absent.

Les deux sont des MODIFICATEURS bornés, pas des termes du blend. Le blend est
calibré et mesuré à 74,7 % de réussite au-dessus de 0,70 ; lui ajouter un terme
prendrait du poids aux quatre autres et casserait cette mesure sans qu'on sache
lequel a aidé. Ces tests tiennent surtout les BORNES : un signal sur-interprété
fait plus de dégâts qu'un signal absent.
"""
from __future__ import annotations

import pytest

from betbot.injuries import (
    KEY_PLAYER_EXTRA, MIN_FACTOR, PENALTY_PER_ABSENCE,
    injury_factor_from_counts, _norm_player,
)
from betbot.standings import (
    MAX_SWING, form_factor_from_score, form_score, get_form_factor,
)


# ---------------------------------------------------------------------------
# Forme sur les résultats
# ---------------------------------------------------------------------------

def test_recent_results_weigh_more_than_old_ones():
    """« WWWLL » et « LLWWW » contiennent les mêmes résultats ; seule leur
    chronologie diffère. Le plus récent est à droite chez api-football, donc la
    seconde équipe est en meilleure forme."""
    assert form_score("LLWWW") > form_score("WWWLL")


def test_an_average_form_moves_nothing():
    """Un signal neutre ne doit JAMAIS déplacer une prédiction : trois nuls,
    c'est une forme moyenne, donc facteur exactement 1,0."""
    assert form_factor_from_score(form_score("DDDDD")) == 1.0


def test_the_swing_is_bounded_in_both_directions():
    """La forme récente est le signal le plus sur-interprété du pronostic
    sportif : cinq matchs sont un échantillon minuscule et le modèle en capture
    déjà l'essentiel par la décroissance sur la récence. Une borne large ferait
    plus de dégâts que d'apport."""
    assert form_factor_from_score(form_score("WWWWW")) == pytest.approx(1 + MAX_SWING)
    assert form_factor_from_score(form_score("LLLLL")) == pytest.approx(1 - MAX_SWING)
    for f in ("WWWWW", "LLLLL", "WDLWD", "WWDLW", "LLDWL"):
        assert 1 - MAX_SWING <= form_factor_from_score(form_score(f)) <= 1 + MAX_SWING


@pytest.mark.parametrize("forme", ["", "W", "WD", "??", None])
def test_too_little_history_is_not_a_form(forme):
    """Deux matchs ne sont pas une forme. Sans assez d'historique le signal doit
    être NEUTRE, jamais deviné."""
    assert form_score(forme or "") is None
    assert form_factor_from_score(form_score(forme or "")) == 1.0


def test_the_form_signal_never_raises_and_defaults_to_neutral(monkeypatch):
    """Aucun signal d'enrichissement ne doit pouvoir faire échouer un scan."""
    import betbot.standings as m
    m._factor_cache.clear()
    monkeypatch.setattr(m, "_league_standings",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api morte")))
    assert get_form_factor("Arsenal", "soccer_epl") == 1.0


def test_the_form_signal_is_off_when_disabled(monkeypatch):
    import betbot.standings as m
    m._factor_cache.clear()
    monkeypatch.setenv("FETCH_FORM", "0")
    assert get_form_factor("Arsenal", "soccer_epl") == 1.0


# ---------------------------------------------------------------------------
# Importance du joueur absent
# ---------------------------------------------------------------------------

def test_a_key_absence_costs_about_double_a_routine_one():
    """Le modèle COMPTAIT les absents sans regarder qui manquait : la sortie du
    meilleur buteur et celle d'un troisième gardien pesaient identiquement."""
    banal = 1.0 - injury_factor_from_counts(1, 0)
    cadre = 1.0 - injury_factor_from_counts(1, 1)
    assert cadre > banal
    assert cadre == pytest.approx(PENALTY_PER_ABSENCE + KEY_PLAYER_EXTRA)


def test_key_absences_are_a_subset_not_a_separate_count():
    """Un cadre absent est DÉJÀ compté dans n_out : il paie le supplément, pas
    une seconde pénalité de base. Compter deux fois doublerait silencieusement
    l'effet du signal."""
    assert injury_factor_from_counts(2, 5) == injury_factor_from_counts(2, 2)
    assert injury_factor_from_counts(3, 0) > injury_factor_from_counts(3, 3)


def test_the_attack_floor_holds_whatever_the_absences():
    """Aucune équipe ne perd plus de 20 % d'attaque : au-delà, le modèle
    n'extrapole plus, il invente."""
    for n, k in ((5, 5), (12, 12), (99, 99)):
        assert injury_factor_from_counts(n, k) == pytest.approx(MIN_FACTOR)


def test_no_absence_changes_nothing():
    assert injury_factor_from_counts(0, 0) == 1.0


@pytest.mark.parametrize("a,b", [
    ("K. Mbappé", "Kylian Mbappe"),
    ("M. Salah", "Mohamed Salah"),
    ("Vinícius Júnior", "Vinicius Junior"),
])
def test_player_names_match_across_endpoint_spellings(a, b):
    """Les blessures et le classement des buteurs n'orthographient pas les noms
    pareil. Une comparaison exacte ne rapprocherait jamais rien — et un signal
    qui n'apparie jamais est un signal mort qui coûte quand même son appel."""
    assert _norm_player(a) == _norm_player(b) != ""


def test_the_form_factor_reaches_the_model():
    """Un signal calculé mais jamais branché est le pire des deux mondes : il
    coûte son quota et ne change aucune prédiction."""
    import ast
    from pathlib import Path
    src = Path("betbot/analysis.py").read_text(encoding="utf-8")
    used = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "_form_factor" in used
    assert src.count("_form_factor(") >= 2, "appliqué à domicile ET à l'extérieur"


def test_the_topscorers_call_does_not_starve_the_injury_budget(monkeypatch):
    """`_budget_used` protège les recherches PAR ÉQUIPE (deux appels par
    équipe, proportionnelles au nombre de matchs). Le classement des buteurs
    est un appel par LIGUE, caché 12 h. L'imputer au même budget de 40 l'aurait
    épuisé dès les premières ligues et éteint le signal blessures en silence —
    un signal en tuant un autre."""
    import betbot.injuries as m
    m._key_players_cache.clear()
    monkeypatch.setattr("betbot.data_sources.api_football.get_topscorers",
                        lambda *a, **k: {"Arsenal": ["Bukayo Saka"]})
    avant = m._budget_used
    for lid in range(50):                      # 50 ligues d'affilée
        m._key_players(lid, 2026)
    assert m._budget_used == avant
