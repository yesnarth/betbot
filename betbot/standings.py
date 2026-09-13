"""
Signal de FORME — l'élan sur les derniers résultats, absent du blend.

Sa demande du 2026-09-13 : tenir compte de « la forme / statistique de l'équipe,
[...] classement et bien d'autres signaux ».

Ce qui existait déjà, pour ne pas le refaire : attaque/défense Dixon-Coles avec
décroissance exponentielle sur la récence, xG des 6 derniers matchs, ELO Club
Elo, confrontations directes, blessures, fatigue, météo. Le CLASSEMENT lui-même
double largement l'ELO et les forces attaque/défense — un club haut au
classement est déjà un club fort dans le modèle, l'ajouter compterait la même
information deux fois.

Ce qui manquait vraiment, c'est l'ÉLAN SUR LES RÉSULTATS : une équipe qui vient
d'enchaîner quatre victoires n'est pas décrite par sa moyenne de buts de la
saison. `form` du classement api-football (« WWDLW », le plus récent à droite)
le capture en un seul appel par ligue.

UN MODIFICATEUR, PAS UN TERME DU BLEND. Le blend est calibré et mesuré à 74,7 %
de réussite au-dessus de 0,70 ; y ajouter un cinquième terme prendrait du poids
aux quatre autres et casserait cette mesure sans qu'on sache lequel a aidé. La
forme s'applique donc comme les blessures : un facteur multiplicatif borné sur
l'attaque, via `home_attack_mod` / `away_attack_mod`, plumbing qui existe déjà.

BORNES ÉTROITES, ET ASSUMÉES. ±6 % au maximum. La forme récente est le signal
le plus sur-interprété du pronostic sportif : cinq matchs, c'est un échantillon
minuscule, et l'essentiel de ce qu'on y lit est du bruit que le modèle capture
déjà par ailleurs. Une borne large ferait plus de dégâts que d'apport.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("betbot.standings")

CACHE_TTL_SEC = 12 * 3600
MAX_SWING = 0.06               # ±6 % sur l'attaque, jamais plus
_LOOKBACK = 5                  # derniers résultats lus dans `form`

_standings_cache: dict[tuple, tuple[dict, float]] = {}
_factor_cache: dict[tuple, tuple[float, float]] = {}


def _enabled() -> bool:
    return os.getenv("FETCH_FORM", "1") == "1"


def _current_season(now: datetime) -> int:
    return now.year if now.month >= 7 else now.year - 1


def form_score(form: str) -> float | None:
    """Score de forme dans [0, 1] depuis une chaîne « WWDLW ».

    Les résultats récents pèsent plus : le dernier match compte double le
    cinquième. Retourne None si la chaîne est vide ou trop courte pour dire
    quoi que ce soit — deux matchs ne sont pas une forme.
    """
    if not form:
        return None
    recents = [c for c in form.upper() if c in ("W", "D", "L")][-_LOOKBACK:]
    if len(recents) < 3:
        return None
    points = {"W": 1.0, "D": 0.5, "L": 0.0}
    # Le plus récent est à droite → poids croissant vers la fin.
    poids = [1.0 + i * (1.0 / max(len(recents) - 1, 1)) for i in range(len(recents))]
    total = sum(p * points[c] for p, c in zip(poids, recents))
    return total / sum(poids)


def form_factor_from_score(score: float | None) -> float:
    """Facteur multiplicatif borné. 0,5 (forme moyenne) donne exactement 1,0 —
    un signal neutre ne doit jamais déplacer une prédiction."""
    if score is None:
        return 1.0
    return 1.0 + MAX_SWING * (2.0 * score - 1.0)


def _league_standings(sport_key: str) -> dict:
    from betbot.data_sources import api_football

    league_id = api_football.league_id_for(sport_key)
    if not league_id:
        return {}
    now = time.time()
    cached = _standings_cache.get((sport_key,))
    if cached is not None and (now - cached[1]) < CACHE_TTL_SEC:
        return cached[0]
    season = _current_season(datetime.now(timezone.utc))
    table = api_football.get_standings(league_id, season)
    # Le cache est écrit MÊME VIDE : une ligue sans classement (coupe,
    # compétition continentale) serait sinon re-interrogée à chaque match de
    # chaque scan, pour rien.
    _standings_cache[(sport_key,)] = (table, now)
    return table


def get_form_factor(team_name: str, sport_key: str | None) -> float:
    """Facteur d'attaque dans [1-MAX_SWING, 1+MAX_SWING]. Vaut 1.0 — donc
    n'a aucun effet — dès qu'il manque quoi que ce soit. Ne lève jamais."""
    if not _enabled() or not team_name or not sport_key:
        return 1.0
    key = (team_name, sport_key)
    now = time.time()
    cached = _factor_cache.get(key)
    if cached is not None and (now - cached[1]) < CACHE_TTL_SEC:
        return cached[0]
    factor = 1.0
    try:
        table = _league_standings(sport_key)
        row = table.get(team_name)
        if row is None:
            # Le classement porte l'orthographe api-football, la requête celle
            # d'Odds API : on réutilise le matcher de production plutôt que
            # d'en réinventer un — un validateur qui apparie autrement ne
            # valide rien (cf. l'incident Dundee United / Dundee).
            from betbot.analysis import _fuzzy_lookup
            # _fuzzy_lookup renvoie (valeur, nom_apparié) et (None, None) sur
            # échec — pas la valeur seule.
            row, _matched = _fuzzy_lookup(team_name, table)
        if row:
            factor = form_factor_from_score(form_score(row.get("form") or ""))
    except Exception as exc:
        logger.debug("forme indisponible pour %s : %s", team_name, exc)
        factor = 1.0
    _factor_cache[key] = (factor, now)
    return factor
