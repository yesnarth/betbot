"""Injury → attack-strength penalty for the blended model.

Turns API-Football injuries (players ruled out) into a multiplicative ATTACK
factor (≤ 1.0) on the affected team's expected goals (λ). This captures info the
market prices but the statistical model ignores.

SAFE BY DESIGN:
  - OFF by default (FETCH_INJURIES=0) → returns 1.0 → model unchanged.
  - Fully graceful: no API key / quota hit / error / unmapped league /
    off-season → 1.0 (never raises).
  - Quota-aware (API-Football free tier = 100 req/day): in-process caches for
    team-id and computed factors (TTL 12h) + a per-run lookup budget.

Modelling choice (documented simplification): we only model the dominant,
reliably-signed effect — absences REDUCE the team's attack. We do NOT try to
infer "defender out → opponent scores more" (needs reliable position data).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("betbot.injuries")

# sport_key → API-Football league id (v3). Only mapped leagues are queried;
# unmapped (consensus-only) leagues simply get factor 1.0.
_LEAGUE_ID: dict[str, int] = {
    "soccer_epl": 39,
    "soccer_spain_la_liga": 140,
    "soccer_germany_bundesliga": 78,
    "soccer_italy_serie_a": 135,
    "soccer_france_ligue1": 61,
    "soccer_uefa_champs_league": 2,
    "soccer_efl_champ": 40,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
}

PENALTY_PER_ABSENCE = 0.035    # −3.5 % attack per confirmed absence
# UN ABSENT N'EN VAUT PAS UN AUTRE — sa demande du 2026-09-13 (« forme /
# statistique des joueurs »). Le modèle COMPTAIT les absents sans jamais
# regarder QUI manquait : la sortie du meilleur buteur et celle d'un troisième
# gardien pesaient exactement pareil. Un joueur figurant au classement des
# buteurs de sa ligue paie ce supplément en plus de la pénalité de base, soit
# environ le double. Les buteurs sont une approximation grossière de
# l'importance offensive — mais c'est la seule qui tienne en UN appel par
# ligue, et le surcoût d'un appel par joueur ne se justifierait pas.
KEY_PLAYER_EXTRA = 0.045       # supplément pour l'absence d'un buteur de la ligue
MAX_ABSENCES_COUNTED = 5       # cap the penalty → at most −17.5 %
MIN_FACTOR = 0.80              # never cut a team's attack by more than 20 %
CACHE_TTL_SEC = 12 * 3600      # injuries don't change minute-to-minute
_DEFAULT_BUDGET = 40           # max API team-lookups per scan run (quota guard)

_key_players_cache: dict[tuple, tuple[set, float]] = {}   # (league, season) → (noms, ts)
_team_id_cache: dict[tuple, int | None] = {}      # (name, league_id) → team_id
_factor_cache: dict[tuple, tuple[float, float]] = {}  # (name, sport_key) → (factor, ts)
_budget_used = 0


def _enabled() -> bool:
    """Read each call so .env toggles take effect without a restart."""
    return os.getenv("FETCH_INJURIES", "0") == "1"


def _current_season(now: datetime) -> int:
    """API-Football season = the starting year (Aug-Jul cycle)."""
    return now.year if now.month >= 7 else now.year - 1


def reset_run_budget() -> None:
    """Reset the per-scan API-lookup budget. Called at the start of a scan so a
    long-running worker keeps fetching on subsequent scans (cache covers repeats)."""
    global _budget_used
    _budget_used = 0


def get_injury_factor(team_name: str, sport_key: str | None) -> float:
    """Multiplicative attack factor in [MIN_FACTOR, 1.0]. Returns 1.0 (no-op) when
    disabled, unavailable, unmapped, or on any error. Never raises."""
    global _budget_used
    if not _enabled() or not team_name or not sport_key:
        return 1.0
    league_id = _LEAGUE_ID.get(sport_key)
    if not league_id:
        return 1.0

    now = time.time()
    cache_key = (team_name, sport_key)
    cached = _factor_cache.get(cache_key)
    if cached is not None and (now - cached[1]) < CACHE_TTL_SEC:
        return cached[0]

    budget = _DEFAULT_BUDGET
    try:
        budget = max(0, int(os.getenv("INJURY_LOOKUP_BUDGET", str(_DEFAULT_BUDGET))))
    except ValueError:
        pass
    if _budget_used >= budget:
        # Protect the daily quota — cache neutral so we don't retry this run.
        _factor_cache[cache_key] = (1.0, now)
        return 1.0

    try:
        from betbot.data_sources import api_football

        season = _current_season(datetime.now(timezone.utc))
        tid_key = (team_name, league_id)
        if tid_key in _team_id_cache:
            tid = _team_id_cache[tid_key]
        else:
            _budget_used += 1
            tid = api_football.search_team_id(team_name, league_id)
            _team_id_cache[tid_key] = tid
        if not tid:
            _factor_cache[cache_key] = (1.0, now)
            return 1.0

        _budget_used += 1
        injuries = api_football.get_team_injuries(tid, league_id, season)
        absents = [i for i in injuries
                   if (i.get("type") or "").lower().startswith("missing")]
        n_out = len(absents)
        cles = _key_players(league_id, season)
        n_key_out = sum(1 for i in absents
                        if _norm_player(i.get("player") or "") in cles)
        factor = injury_factor_from_counts(n_out, n_key_out)
        _factor_cache[cache_key] = (factor, now)
        if n_out:
            logger.info("injuries %s: %d absent(s) dont %d cadre(s) → attaque ×%.3f",
                        team_name, n_out, n_key_out, factor)
        return factor
    except api_football.APIFootballNotConfigured:
        return 1.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("injury factor for %s failed: %s", team_name, exc)
        return 1.0


def _norm_player(name: str) -> str:
    """Les noms de joueurs diffèrent d'un endpoint à l'autre (« K. Mbappé » vs
    « Kylian Mbappe »). On compare sur le NOM DE FAMILLE normalisé : c'est la
    partie stable, et une comparaison exacte ne rapprocherait jamais rien."""
    import unicodedata
    s = unicodedata.normalize("NFKD", (name or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() or c == " " else " " for c in s)
    parts = [p for p in s.split() if len(p) > 1]
    return parts[-1] if parts else ""


def _key_players(league_id: int, season: int) -> set[str]:
    """Noms de famille normalisés des buteurs de la ligue. Un appel par ligue,
    caché 12 h — et caché MÊME VIDE, sinon une ligue sans classement de buteurs
    serait réinterrogée à chaque équipe de chaque scan."""
    key = (league_id, season)
    now = time.time()
    cached = _key_players_cache.get(key)
    if cached is not None and (now - cached[1]) < CACHE_TTL_SEC:
        return cached[0]
    noms: set[str] = set()
    try:
        from betbot.data_sources import api_football
        # VOLONTAIREMENT hors du budget `_budget_used`, qui protège les
        # recherches PAR ÉQUIPE (deux appels par équipe, donc proportionnelles
        # au nombre de matchs du scan). Celui-ci est un appel par LIGUE, caché
        # 12 h : au pire 46 sur une journée. Le compter dans un budget de 40
        # aurait épuisé celui-ci dès les premières ligues et éteint le signal
        # blessures en silence — un signal en tuant un autre.
        for joueurs in api_football.get_topscorers(league_id, season).values():
            noms.update(_norm_player(j) for j in joueurs)
        noms.discard("")
    except Exception as exc:  # noqa: BLE001
        logger.debug("topscorers %s indisponible : %s", league_id, exc)
    _key_players_cache[key] = (noms, now)
    return noms


def injury_factor_from_counts(n_out: int, n_key_out: int = 0) -> float:
    """Heuristique pure (testable sans API) : absences → facteur d'attaque.

    `n_key_out` est un SOUS-ENSEMBLE de `n_out`, pas un compte séparé : un
    cadre absent est déjà compté dans n_out et paie seulement le supplément.
    """
    n_out = max(n_out, 0)
    n_key_out = max(0, min(n_key_out, n_out))
    base = PENALTY_PER_ABSENCE * min(n_out, MAX_ABSENCES_COUNTED)
    extra = KEY_PLAYER_EXTRA * min(n_key_out, MAX_ABSENCES_COUNTED)
    return max(MIN_FACTOR, 1.0 - base - extra)
