"""
Value bet detection, Kelly stake sizing, and parlay construction.

Takes raw events (from The Odds API) + team stats (from SQLite/Poisson model)
and returns ranked value bets and parlay combinations.
"""
from __future__ import annotations

import itertools
import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import get_close_matches

from betbot.models import (
    MatchProbs,
    poisson_match_probs,
    blended_match_probs,
    consensus_match_probs,
    extract_best_odds,
    build_team_stats,
    compute_league_averages,
)
from betbot.football_api import parse_match_results, LEAGUE_MAP

logger = logging.getLogger("betbot.analysis")


@dataclass
class ValueBet:
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    league_label: str
    market: str          # "h2h"
    selection_code: str  # "1", "X", "2"
    selection_label: str # "Victoire domicile" etc.
    model_prob: float
    best_odds: float
    best_book: str
    value_edge: float    # model_prob * best_odds - 1.0  (positive = value)
    kelly_stake: float
    lambda_home: float | None
    lambda_away: float | None
    model_type: str      # "poisson" or "consensus"


@dataclass
class Parlay:
    bets: list[ValueBet]
    combined_odds: float
    combined_prob: float
    combined_ev: float   # (combined_prob * combined_odds - 1) * 100


# ---------------------------------------------------------------------------
# Team name normalization (bridges Odds API ↔ football-data.org names)
# ---------------------------------------------------------------------------

_STRIP_WORDS = frozenset([
    'fc', 'cf', 'ac', 'rc', 'rcd', 'as', 'ss', 'us', 'ud', 'cd', 'afc',
    'sc', 'bv', 'sv', 'fk', 'nk', 'sk',
    'de', 'del', 'la', 'le', 'les',
    'city', 'united', 'town', 'county', 'rovers', 'wanderers',
    'calcio', 'balompie',
    'hotspur', 'albion',
])

# Odds API common name → distinctive fragment present in the normalized DB name.
# Needed for teams whose English common name differs fundamentally from their official name.
_KNOWN_ALIASES: dict[str, str] = {
    'inter milan':           'internazionale',
    'internazionale':        'internazionale',
    'atletico madrid':       'atletico',
    'real betis':            'betis',
    'borussia m.gladbach':   'gladbach',
    'monchengladbach':       'gladbach',
    'bayer leverkusen':      'leverkusen',
    'rb leipzig':            'leipzig',
    'paris saint-germain':   'paris',
    'psg':                   'paris',
}


def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove common football suffixes/words."""
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'\b\d{4}\b', ' ', s)          # strip year suffixes (1913, 1909…)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)         # keep only letters, digits, spaces
    words = [w for w in s.split() if w not in _STRIP_WORDS]
    return ' '.join(words)


def _fuzzy_lookup(name: str, cache: dict):
    """Look up a team in cache: exact → alias → normalized → substring (≥6 chars) → fuzzy."""
    if name in cache:
        return cache[name], name

    norm_query = _normalize_name(name)
    norm_index: dict[str, str] = {_normalize_name(k): k for k in cache}

    # 1. Exact normalized match
    if norm_query in norm_index:
        return cache[norm_index[norm_query]], norm_index[norm_query]

    # 2. Known alias (handles fundamentally different names between the two APIs)
    alias_fragment = _KNOWN_ALIASES.get(norm_query)
    if alias_fragment:
        for norm_key, orig_key in norm_index.items():
            if alias_fragment in norm_key:
                return cache[orig_key], orig_key

    # 3. Substring match — the shorter name must be ≥ 6 chars to avoid short city-name collisions
    #    (e.g. prevent "milan" from matching "inter milan" when looking for AC Milan)
    for norm_key, orig_key in norm_index.items():
        shorter = norm_key if len(norm_key) <= len(norm_query) else norm_query
        longer  = norm_query if shorter == norm_key else norm_key
        if len(shorter) >= 6 and shorter in longer:
            return cache[orig_key], orig_key

    # 4. Fuzzy match (last resort)
    close = get_close_matches(norm_query, list(norm_index.keys()), n=1, cutoff=0.65)
    if close:
        orig_key = norm_index[close[0]]
        return cache[orig_key], orig_key

    return None, None


# ---------------------------------------------------------------------------
# Kelly Criterion
# ---------------------------------------------------------------------------

def kelly_stake(
    model_prob: float,
    decimal_odds: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_fraction: float = 0.05,
) -> float:
    """
    Fractional Kelly stake.
    Returns 0.0 if edge is negative (do not bet).
    Never exceeds max_fraction * bankroll.
    """
    b = decimal_odds - 1.0
    p = model_prob
    q = 1.0 - p
    if b <= 0 or p <= 0:
        return 0.0
    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0.0
    fraction = min(full_kelly * kelly_fraction, max_fraction)
    return round(fraction * bankroll, 2)


# ---------------------------------------------------------------------------
# Value detection
# ---------------------------------------------------------------------------

def detect_value_bets(
    events_by_sport: dict[str, list[dict]],
    match_history_by_sport: dict[str, list[dict]],
    bankroll: float,
    kelly_fraction: float = 0.25,
    min_value_edge: float = 0.04,
    min_model_prob: float = 0.40,
    min_book_odds: float = 1.50,
    prebuilt_stats_by_sport: dict[str, dict] | None = None,
    probs_cache: dict[str, "MatchProbs"] | None = None,
) -> list[ValueBet]:
    """
    Main analysis pipeline.
    prebuilt_stats_by_sport: {sport_key: {"teams": {name: TeamStats}, "home_avg": float, "away_avg": float}}
    probs_cache: optional {event_id: MatchProbs} cache shared across calls (avoids
                 recomputing Poisson at each relaxation level in _ensure_min_combos).
    """
    from betbot.models import DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG
    all_bets: list[ValueBet] = []
    if probs_cache is None:
        probs_cache = {}

    for sport_key, events in events_by_sport.items():
        # Resolve team-stats cache + league averages for this sport
        if prebuilt_stats_by_sport and sport_key in prebuilt_stats_by_sport:
            entry = prebuilt_stats_by_sport[sport_key]
            team_stats_cache = entry.get("teams", {})
            home_avg = entry.get("home_avg", DEFAULT_HOME_AVG)
            away_avg = entry.get("away_avg", DEFAULT_AWAY_AVG)
            logger.info(
                "  %s : Poisson (%d équipes, ligue %.2f/%.2f buts)",
                sport_key, len(team_stats_cache), home_avg, away_avg,
            )
        else:
            raw_matches = match_history_by_sport.get(sport_key, [])
            parsed = parse_match_results(raw_matches) if raw_matches else []
            home_avg, away_avg = compute_league_averages(parsed)
            team_stats_cache: dict[str, object] = {}
            if parsed:
                all_teams = {m["home_team"] for m in parsed} | {m["away_team"] for m in parsed}
                for team in all_teams:
                    stats = build_team_stats(team, parsed, home_avg, away_avg)
                    if stats:
                        team_stats_cache[team] = stats
            logger.info("  %s : modèle consensus (pas de stats Poisson)", sport_key)

        league_label = _sport_key_to_label(sport_key)

        for event in events:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            event_id = event.get("id", f"{home}_{away}")

            # Cache probabilities by event_id (avoids 5x recomputation in relaxation loop)
            probs = probs_cache.get(event_id)
            if probs is None:
                probs = _compute_probs(home, away, event, team_stats_cache, home_avg, away_avg)
                if probs is not None:
                    probs_cache[event_id] = probs
            if probs is None:
                continue

            # Evaluate each H2H outcome
            outcome_map = [
                ("1", "Victoire domicile", home, probs.home_win),
                ("X", "Match nul", "Draw", probs.draw),
                ("2", "Victoire extérieur", away, probs.away_win),
            ]

            for code, label, outcome_name, raw_model_prob in outcome_map:
                if raw_model_prob < min_model_prob:
                    continue

                best = extract_best_odds(event, outcome_name)
                if best is None or best.price < min_book_odds:
                    continue

                # Market shrinkage: pull the raw model prob toward the market
                # implied prob when the disagreement is suspiciously large.
                # This prevents fictitious mega-edges driven by qualitative
                # info (injuries, suspensions) the model doesn't see.
                from betbot.calibration import shrink_toward_market
                model_prob = shrink_toward_market(raw_model_prob, best.price)

                edge = round(model_prob * best.price - 1.0, 4)
                if edge < min_value_edge:
                    continue

                stake = kelly_stake(model_prob, best.price, bankroll, kelly_fraction)
                if stake == 0.0:
                    continue

                all_bets.append(ValueBet(
                    event_id=event_id,
                    sport_key=sport_key,
                    home_team=home,
                    away_team=away,
                    league_label=league_label,
                    market="h2h",
                    selection_code=code,
                    selection_label=label,
                    model_prob=round(model_prob, 4),
                    best_odds=best.price,
                    best_book=best.bookmaker,
                    value_edge=edge,
                    kelly_stake=stake,
                    lambda_home=probs.lambda_home,
                    lambda_away=probs.lambda_away,
                    model_type=probs.model,
                ))

    logger.info("Détection terminée : %d paris de valeur trouvés", len(all_bets))
    return all_bets


def _compute_probs(
    home: str,
    away: str,
    event: dict,
    team_stats_cache: dict,
    league_home_avg: float,
    league_away_avg: float,
) -> MatchProbs | None:
    """
    Compute match probabilities using Dixon-Coles-style independent Poisson.

    Formula (corrected):
        λ_home = α_home(home) × β_away(away) × μ_home_avg
        λ_away = α_away(away) × β_home(home) × μ_away_avg

    where α (attack) and β (defense) are dimensionless ratios relative to the
    league average (≈ 1.0 for an average team), and μ is the league-wide
    average goals scored at home / away (which already encodes the home advantage).

    Falls back to the multi-bookmaker consensus model if either team has no stats.
    """
    home_stats, home_matched = _fuzzy_lookup(home, team_stats_cache)
    away_stats, away_matched = _fuzzy_lookup(away, team_stats_cache)

    if home_stats and away_stats:
        try:
            # Use blended model (Dixon-Coles + xG + ELO) — auto-degrades to plain
            # Dixon-Coles when xG/ELO are missing (legacy team_stats rows).
            result = blended_match_probs(
                home_stats=home_stats,
                away_stats=away_stats,
                league_home_avg=league_home_avg,
                league_away_avg=league_away_avg,
                weather_modifier=1.0,  # weather applied separately at scan-time when known
            )
            match_info = f"({home_matched} / {away_matched})" if (home_matched != home or away_matched != away) else ""
            logger.debug("%s %s vs %s %s: λH=%.2f λA=%.2f → H=%.1f%% D=%.1f%% A=%.1f%%",
                         result.model, home, away, match_info,
                         result.lambda_home, result.lambda_away,
                         result.home_win*100, result.draw*100, result.away_win*100)
            return result
        except Exception as exc:
            logger.warning("Blended/Poisson échoué pour %s vs %s : %s", home, away, exc)
    else:
        missing = []
        if not home_stats:
            missing.append(home)
        if not away_stats:
            missing.append(away)
        logger.debug("Consensus %s vs %s (stats manquantes: %s)", home, away, ", ".join(missing))

    # Fallback: consensus multi-bookmaker model
    return consensus_match_probs(event)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_value_bets(bets: list[ValueBet]) -> list[ValueBet]:
    """Sort by value_edge descending. Ties broken by model_prob."""
    return sorted(bets, key=lambda b: (b.value_edge, b.model_prob), reverse=True)


# ---------------------------------------------------------------------------
# Parlay builder
# ---------------------------------------------------------------------------

def build_parlays(
    bets: list[ValueBet],
    n_legs: int = 3,
    top_n: int = 3,
    min_combined_odds: float = 2.0,
) -> list[Parlay]:
    """
    Generate n-leg parlays from ranked value bets.
    Constraints:
    - No two legs from the same match
    - Combined odds >= min_combined_odds
    - Ranked by combined expected value
    """
    parlays: list[Parlay] = []

    for combo in itertools.combinations(bets, n_legs):
        # Check: no two bets on the same event
        event_ids = [b.event_id for b in combo]
        if len(event_ids) != len(set(event_ids)):
            continue

        combined_odds = 1.0
        combined_prob = 1.0
        for bet in combo:
            combined_odds *= bet.best_odds
            combined_prob *= bet.model_prob

        combined_odds = round(combined_odds, 2)
        if combined_odds < min_combined_odds:
            continue

        combined_ev = round((combined_prob * combined_odds - 1.0) * 100, 2)

        parlays.append(Parlay(
            bets=list(combo),
            combined_odds=combined_odds,
            combined_prob=round(combined_prob, 4),
            combined_ev=combined_ev,
        ))

    parlays.sort(key=lambda p: p.combined_ev, reverse=True)
    return parlays[:top_n]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sport_key_to_label(sport_key: str) -> str:
    labels = {
        "soccer_france_ligue1": "Ligue 1",
        "soccer_epl": "Premier League",
        "soccer_spain_la_liga": "La Liga",
        "soccer_italy_serie_a": "Serie A",
        "soccer_germany_bundesliga": "Bundesliga",
        "soccer_uefa_champs_league": "Champions League",
        "soccer_africa_cup_of_nations": "CAN",
    }
    return labels.get(sport_key, sport_key.replace("soccer_", "").replace("_", " ").title())
