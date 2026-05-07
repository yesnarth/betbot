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

from betbot.calibration import shrink_toward_market
from betbot.ml import calibrate as ml_calibrate
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

# Only strip TRUE corporate suffixes — never discriminating tokens like
# "united", "city", "hotspur", which are the only thing that tells "Manchester
# United" apart from "Manchester City". Stripping them caused a name collision
# bug where both teams normalized to "manchester", so lookups for one returned
# the other's stats.
_STRIP_WORDS = frozenset([
    'fc', 'cf', 'ac', 'rc', 'rcd', 'as', 'ss', 'us', 'ud', 'cd', 'afc',
    'sc', 'bv', 'sv', 'fk', 'nk', 'sk',
    'de', 'del', 'la', 'le', 'les',
    'calcio', 'balompie',
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
    """Look up a team in cache: exact → normalized → alias → token-set → fuzzy.

    Token-set matching beats substring because it requires the discriminating
    tokens (city/united/hotspur) to match — preventing the historical bug
    where 'Manchester United' would silently get 'Manchester City' stats.
    """
    if name in cache:
        return cache[name], name

    norm_query = _normalize_name(name)
    norm_index: dict[str, str] = {_normalize_name(k): k for k in cache}

    # 1. Exact normalized match
    if norm_query in norm_index:
        return cache[norm_index[norm_query]], norm_index[norm_query]

    # 2. Known alias (different names between Odds API and football-data)
    alias_fragment = _KNOWN_ALIASES.get(norm_query)
    if alias_fragment:
        for norm_key, orig_key in norm_index.items():
            if alias_fragment in norm_key.split():
                return cache[orig_key], orig_key

    # 3. Token-set match — every token of the shorter name must appear in the
    #    longer name. Example: "manchester united" tokens {manchester, united}
    #    must ALL be present in candidate's tokens. This rejects the buggy case
    #    where "manchester united" silently matched "manchester city".
    query_tokens = set(norm_query.split())
    if query_tokens:
        best_match: tuple[str, str] | None = None
        best_overlap = 0
        for norm_key, orig_key in norm_index.items():
            key_tokens = set(norm_key.split())
            shorter, longer = (
                (query_tokens, key_tokens) if len(query_tokens) <= len(key_tokens)
                else (key_tokens, query_tokens)
            )
            # ALL tokens of the shorter side must be in the longer side
            if shorter and shorter.issubset(longer) and len(shorter) > best_overlap:
                best_overlap = len(shorter)
                best_match = (orig_key, orig_key)
        if best_match:
            return cache[best_match[0]], best_match[1]

    # 4. Fuzzy match (last resort, conservative threshold)
    close = get_close_matches(norm_query, list(norm_index.keys()), n=1, cutoff=0.75)
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
                probs = _compute_probs(home, away, event, team_stats_cache,
                                       home_avg, away_avg, sport_key=sport_key)
                if probs is not None:
                    probs_cache[event_id] = probs
            if probs is None:
                continue

            # Evaluate every market we expose. Each tuple is:
            #   (selection_code, label, outcome_name, market_key, point, raw_prob)
            # `market_key` matches The Odds API ("h2h" | "totals" | "btts").
            # `point` is the line for totals (None elsewhere).
            # Markets actually requested from The Odds API (h2h + totals only).
            # BTTS is calculated by the model but its odds aren't fetched, so
            # we don't iterate it here — would always produce best=None.
            is_tennis = bool(sport_key and sport_key.startswith("tennis_"))
            is_basketball = bool(sport_key and sport_key.startswith("basketball_"))
            if is_tennis:
                outcome_map = [
                    ("1", "Victoire joueur 1", home, "h2h", None, probs.home_win),
                    ("2", "Victoire joueur 2", away, "h2h", None, probs.away_win),
                ]
            elif is_basketball:
                # Basketball: only moneyline. The totals market uses team-specific
                # lines (e.g. 224.5) that we don't fetch from the Odds API yet —
                # adding it would require a separate pipeline path with the actual
                # over/under line per game.
                outcome_map = [
                    ("1", "Victoire équipe à domicile", home, "h2h", None, probs.home_win),
                    ("2", "Victoire équipe extérieure", away, "h2h", None, probs.away_win),
                ]
            else:
                outcome_map = [
                    ("1",   "Victoire domicile",   home,    "h2h",    None, probs.home_win),
                    ("X",   "Match nul",           "Draw",  "h2h",    None, probs.draw),
                    ("2",   "Victoire extérieur",  away,    "h2h",    None, probs.away_win),
                    ("O25", "Plus de 2.5 buts",    "Over",  "totals", 2.5,  probs.over_25),
                    ("U25", "Moins de 2.5 buts",   "Under", "totals", 2.5,  probs.under_25),
                ]

            for code, label, outcome_name, market_key, point, raw_model_prob in outcome_map:
                if raw_model_prob < min_model_prob:
                    continue

                best = extract_best_odds(event, outcome_name, market_key=market_key, point=point)
                if best is None or best.price < min_book_odds:
                    continue

                # Two-stage probability calibration:
                # 1. Market shrinkage — pull toward the bookmaker-implied
                #    probability when the disagreement is suspiciously large.
                #    This caps fictitious mega-edges from qualitative info
                #    (injuries, suspensions) the statistical model doesn't see.
                # 2. ML calibration — apply the IsotonicRegression learned from
                #    historical resolved bets. No-op until 50+ resolved bets,
                #    then auto-corrects systematic over/under-confidence.
                shrunk = shrink_toward_market(raw_model_prob, best.price)
                model_prob = ml_calibrate(shrunk)

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
                    market=market_key,
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


def _tennis_event_to_probs(home: str, away: str, sport_key: str) -> MatchProbs | None:
    """Tennis-specific path : surface-aware ELO from Sackmann history."""
    from betbot.tennis_model import predict as tennis_predict
    surface_map = {
        "tennis_atp_aus_open":     "Hard",
        "tennis_atp_us_open":      "Hard",
        "tennis_atp_french_open":  "Clay",
        "tennis_atp_wimbledon":    "Grass",
    }
    surface = surface_map.get(sport_key or "", "Hard")
    tp = tennis_predict(home, away, surface=surface)
    if tp is None:
        return None
    # Tennis has no draw and no totals 2.5 market — set them to dummy values.
    return MatchProbs(
        home_win=tp.home_win,
        draw=0.0,
        away_win=tp.away_win,
        over_25=0.0,
        under_25=1.0,
        btts_yes=0.0,
        btts_no=1.0,
        lambda_home=0.0,
        lambda_away=0.0,
        model=f"tennis_elo_{surface.lower()}",
    )


def _basketball_event_to_probs(home: str, away: str, sport_key: str) -> MatchProbs | None:
    """Basketball-specific path : pace + offensive/defensive rating model."""
    from betbot.basketball_model import predict as bb_predict
    league = "euroleague" if "euroleague" in (sport_key or "") else "nba"
    bp = bb_predict(home, away, league=league)
    if bp is None:
        return None
    # We don't currently fetch the basketball totals odds line, so we surface
    # the predicted total in the model name for diagnostics. Downstream code
    # only looks at home_win / away_win for h2h evaluation.
    return MatchProbs(
        home_win=bp.home_win,
        draw=0.0,           # basketball doesn't draw (OT until winner)
        away_win=bp.away_win,
        over_25=0.0,        # basketball totals line is e.g. 220.5, not 2.5
        under_25=1.0,
        btts_yes=0.0,
        btts_no=1.0,
        lambda_home=bp.expected_home_points,
        lambda_away=bp.expected_away_points,
        model=f"basketball_pace_{league}",
    )


def _compute_probs(
    home: str,
    away: str,
    event: dict,
    team_stats_cache: dict,
    league_home_avg: float,
    league_away_avg: float,
    sport_key: str | None = None,
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
    # Tennis has its own ELO-based path — short-circuit before hitting the
    # football-shaped team_stats lookup.
    if sport_key and sport_key.startswith("tennis_"):
        tp = _tennis_event_to_probs(home, away, sport_key)
        if tp is not None:
            logger.debug("tennis ELO %s vs %s on %s: H=%.1f%% A=%.1f%%",
                         home, away, tp.model, tp.home_win * 100, tp.away_win * 100)
            return tp
        logger.debug("tennis ELO miss for %s / %s — falling back to consensus", home, away)
        return consensus_match_probs(event)

    # Basketball : pace + offensive/defensive rating model
    if sport_key and sport_key.startswith("basketball_"):
        bp = _basketball_event_to_probs(home, away, sport_key)
        if bp is not None:
            logger.debug("basket %s %s vs %s : H=%.1f%% A=%.1f%% (total=%.1f)",
                         bp.model, home, away,
                         bp.home_win * 100, bp.away_win * 100,
                         bp.lambda_home + bp.lambda_away)
            return bp
        logger.debug("basket model miss for %s / %s — falling back to consensus", home, away)
        return consensus_match_probs(event)

    home_stats, home_matched = _fuzzy_lookup(home, team_stats_cache)
    away_stats, away_matched = _fuzzy_lookup(away, team_stats_cache)

    # Visibility on imperfect matches — these are the rows where stats could
    # be wrong. Surfacing them here lets us catch new collisions early.
    if home_matched and home_matched != home and _normalize_name(home_matched) != _normalize_name(home):
        logger.info("team-match home: '%s' -> '%s'", home, home_matched)
    if away_matched and away_matched != away and _normalize_name(away_matched) != _normalize_name(away):
        logger.info("team-match away: '%s' -> '%s'", away, away_matched)
    if home_stats is None:
        logger.warning("team-match MISS home: '%s' has no stats in cache", home)
    if away_stats is None:
        logger.warning("team-match MISS away: '%s' has no stats in cache", away)

    if home_stats and away_stats:
        try:
            # Use blended model (Dixon-Coles + xG + ELO) — auto-degrades to plain
            # Dixon-Coles when xG/ELO are missing (legacy team_stats rows).
            result = blended_match_probs(
                home_stats=home_stats,
                away_stats=away_stats,
                league_home_avg=league_home_avg,
                league_away_avg=league_away_avg,
                weather_modifier=1.0,
                sport_key=sport_key,   # propagates to per-league Dixon-Coles τ
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
