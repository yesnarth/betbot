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
from betbot.ml import calibrate as ml_calibrate, segment_for as _ml_segment
from betbot.blend_params import get_weights as _get_blend_weights
from betbot.injuries import (
    get_injury_factor as _injury_factor,
    reset_run_budget as _reset_injury_budget,
)
from betbot.fatigue import (
    get_fatigue_factor as _fatigue_factor,
    reset_run_budget as _reset_fatigue_budget,
)
from betbot.data_sources.weather import (
    get_weather_factor as _weather_factor,
    reset_weather_budget as _reset_weather_budget,
)
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
    # Reliability score in [0, 1] — qualifies the edge. Higher = more
    # trustworthy. Default 1.0 so legacy callers that build ValueBet
    # manually aren't broken; detect_value_bets populates it from
    # betbot.reliability.compute_reliability.
    reliability: float = 1.0


@dataclass
class Parlay:
    bets: list[ValueBet]
    combined_odds: float
    combined_prob: float
    combined_ev: float   # (combined_prob * combined_odds - 1) * 100
    # True when ≥2 legs share a league (same-day correlation). Surfaced in the
    # UI and reflected in a small EV haircut applied at build time.
    correlated: bool = False


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
    'athletic bilbao':       'athletic',   # football-data: "Athletic Club"
    'athletic club':         'athletic',
}


def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove common football suffixes/words."""
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'\b\d{4}\b', ' ', s)          # strip year suffixes (1913, 1909…)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)         # keep only letters, digits, spaces
    words = [w for w in s.split() if w not in _STRIP_WORDS]
    return ' '.join(words)


# Module-level memoization of (norm_index, token_index) keyed by id(cache).
# A single `detect_value_bets` call invokes _fuzzy_lookup hundreds of times
# (2 lookups per event × ~100 events × 5 sports), and rebuilding the
# normalized index every call is pure waste — the team_stats_cache is
# immutable during a scan. We use id(cache) since dicts aren't weakref-able;
# entries are evicted by `_invalidate_norm_cache(cache)` when callers know
# the cache has changed (typically not, since a scan reuses the same dict).
_NORM_INDEX_CACHE: dict[int, tuple[dict[str, str], dict[str, frozenset]]] = {}


def _norm_indexes_for(cache: dict) -> tuple[dict[str, str], dict[str, frozenset]]:
    """Return (norm_index, token_index) for a team-stats cache, memoized.

    norm_index : {normalized_name: original_name}
    token_index: {normalized_name: frozenset(tokens)}
    """
    cache_id = id(cache)
    cached = _NORM_INDEX_CACHE.get(cache_id)
    # Cheap freshness check: if cache size changed, invalidate.
    if cached is not None and len(cached[0]) == len(cache):
        return cached
    norm_index = {_normalize_name(k): k for k in cache}
    token_index = {n: frozenset(n.split()) for n in norm_index}
    _NORM_INDEX_CACHE[cache_id] = (norm_index, token_index)
    return norm_index, token_index


def _invalidate_norm_cache(cache: dict) -> None:
    """Evict the memoized (norm_index, token_index) for a cache.

    The memo is keyed by ``id(cache)`` with only a size check for freshness —
    fine for the model's single long-lived cache, but unsafe for callers that
    build many SHORT-LIVED caches (e.g. the stale resolver, one per league): a
    freed dict's id() can be reused by a new dict of the same size, which would
    otherwise return a stale index. Such callers must evict around their use.
    """
    _NORM_INDEX_CACHE.pop(id(cache), None)


def _fuzzy_lookup(name: str, cache: dict):
    """Look up a team in cache: exact → normalized → alias → token-set → fuzzy.

    Token-set matching beats substring because it requires the discriminating
    tokens (city/united/hotspur) to match — preventing the historical bug
    where 'Manchester United' would silently get 'Manchester City' stats.

    Performance: norm_index + token_index are memoized per-cache (see above),
    so this is O(n) on tokens not O(n × normalize_cost) per call.
    """
    if name in cache:
        return cache[name], name

    norm_query = _normalize_name(name)
    norm_index, token_index = _norm_indexes_for(cache)

    # 1. Exact normalized match
    if norm_query in norm_index:
        return cache[norm_index[norm_query]], norm_index[norm_query]

    # 2. Known alias (different names between Odds API and football-data)
    alias_fragment = _KNOWN_ALIASES.get(norm_query)
    if alias_fragment:
        for norm_key, orig_key in norm_index.items():
            if alias_fragment in token_index[norm_key]:
                return cache[orig_key], orig_key

    # 3. Token-set match — every token of the shorter name must appear in the
    #    longer name. Example: "manchester united" tokens {manchester, united}
    #    must ALL be present in candidate's tokens. This rejects the buggy case
    #    where "manchester united" silently matched "manchester city".
    query_tokens = frozenset(norm_query.split())
    if query_tokens:
        best_match: tuple[str, str] | None = None
        best_overlap = 0
        for norm_key, orig_key in norm_index.items():
            key_tokens = token_index[norm_key]
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
    reliability: float = 1.0,
) -> float:
    """
    Fractional Kelly stake, optionally down-weighted by reliability.

    Returns 0.0 if edge is negative (do not bet). Never exceeds
    max_fraction * bankroll.

    `reliability` ∈ [0, 1] qualifies how trustworthy the edge estimate is
    (see betbot.reliability.compute_reliability). It linearly scales the
    fractional Kelly so a reliability=0.3 pick gets ~30% of the stake a
    reliability=1.0 pick would. Protects the bankroll from acting on
    low-sample / huge-edge / extreme-prob signals at full conviction.
    """
    b = decimal_odds - 1.0
    p = model_prob
    q = 1.0 - p
    if b <= 0 or p <= 0:
        return 0.0
    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0.0
    # Clamp reliability defensively; callers should already pass a [0, 1] value.
    rel = max(0.0, min(reliability, 1.0))
    fraction = min(full_kelly * kelly_fraction * rel, max_fraction)
    return round(fraction * bankroll, 2)


# ---------------------------------------------------------------------------
# Value detection
# ---------------------------------------------------------------------------

def _derive_dc_dnb_odds(event: dict, home: str, away: str) -> dict:
    """
    Best available Double Chance / Draw No Bet decimal odds, DERIVED from each
    bookmaker's own 1/X/2 prices. Zero extra API quota — bookmakers construct
    these markets exactly this way, so the derivation invents no free money:

        q1,qX,q2 = 1/o1, 1/oX, 1/o2   (that book's vig-inclusive implieds)
        Double Chance   1X = 1/(q1+qX)   X2 = 1/(qX+q2)   12 = 1/(q1+q2)
        Draw No Bet   home = (q1+q2)/q1  away = (q1+q2)/q2   (draw → refund)

    Because q1/qX/q2 already carry the book's margin, the derived prices are
    realistic (slightly short of true-fair), never optimistic. Only books that
    quote the FULL 1/X/2 are used (coherent derivation). Returns
    {code: BestOdds} taking the best price per selection across books.
    """
    from betbot.models import BestOdds
    best: dict = {}
    for bm in event.get("bookmakers", []):
        o: dict[str, float] = {}
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for out in mkt.get("outcomes", []):
                try:
                    price = float(out["price"])
                except (KeyError, ValueError, TypeError):
                    continue
                if price <= 1.0:
                    continue
                name = out.get("name")
                if name == home:
                    o["1"] = price
                elif name == away:
                    o["2"] = price
                elif name == "Draw":
                    o["X"] = price
        if not {"1", "X", "2"} <= o.keys():
            continue  # need the whole 1/X/2 to derive coherently
        q1, qx, q2 = 1.0 / o["1"], 1.0 / o["X"], 1.0 / o["2"]
        title = bm.get("title", bm.get("key", "?"))
        derived = {
            "1X":   1.0 / (q1 + qx),
            "X2":   1.0 / (qx + q2),
            "12":   1.0 / (q1 + q2),
            "DNB1": (q1 + q2) / q1,
            "DNB2": (q1 + q2) / q2,
        }
        for code, price in derived.items():
            if price <= 1.0:
                continue
            price = round(price, 3)
            if code not in best or price > best[code].price:
                best[code] = BestOdds(outcome_name=code, price=price, bookmaker=title)
    return best


def detect_value_bets(
    events_by_sport: dict[str, list[dict]],
    match_history_by_sport: dict[str, list[dict]],
    bankroll: float,
    kelly_fraction: float = 0.25,
    min_value_edge: float = 0.04,
    min_model_prob: float = 0.40,
    min_book_odds: float = 1.50,
    min_edge_vs_novig: float = 0.0,
    require_positive_stake: bool = True,
    max_book_odds: float = 0.0,
    underdog_odds: float = 0.0,
    underdog_min_prob: float = 0.0,
    novig_required: bool = False,
    derive_dc_dnb: bool = True,
    derived_min_edge: float = 0.02,
    derived_min_odds: float = 1.10,
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
    _reset_injury_budget()  # fresh per-scan API-Football lookup budget (injuries)
    _reset_fatigue_budget()  # fresh per-scan API-Football lookup budget (rest/congestion)
    _reset_weather_budget()  # fresh per-scan Open-Meteo lookup budget (weather)
    if probs_cache is None:
        probs_cache = {}

    for sport_key, events in events_by_sport.items():
        # Resolve team-stats cache + league averages + H2H lookup for this sport
        h2h_lookup: dict = {}
        if prebuilt_stats_by_sport and sport_key in prebuilt_stats_by_sport:
            entry = prebuilt_stats_by_sport[sport_key]
            team_stats_cache = entry.get("teams", {})
            home_avg = entry.get("home_avg", DEFAULT_HOME_AVG)
            away_avg = entry.get("away_avg", DEFAULT_AWAY_AVG)
            h2h_lookup = entry.get("h2h", {})
            logger.info(
                "  %s : Poisson (%d équipes, %d paires H2H, ligue %.2f/%.2f buts)",
                sport_key, len(team_stats_cache), len(h2h_lookup), home_avg, away_avg,
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
                                       home_avg, away_avg, sport_key=sport_key,
                                       h2h_lookup=h2h_lookup)
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
                    # Totals — the `totals` market in Odds API returns multiple
                    # points (typically 1.5, 2.5, 3.5). extract_best_odds
                    # silently returns None when a bookmaker doesn't quote the
                    # specific point, so the loop just skips those legs.
                    ("O15", "Plus de 1.5 buts",    "Over",  "totals", 1.5,  probs.over_15),
                    ("U15", "Moins de 1.5 buts",   "Under", "totals", 1.5,  probs.under_15),
                    ("O25", "Plus de 2.5 buts",    "Over",  "totals", 2.5,  probs.over_25),
                    ("U25", "Moins de 2.5 buts",   "Under", "totals", 2.5,  probs.under_25),
                    ("O35", "Plus de 3.5 buts",    "Over",  "totals", 3.5,  probs.over_35),
                    ("U35", "Moins de 3.5 buts",   "Under", "totals", 3.5,  probs.under_35),
                ]

            # ---- Stage 1 : calibrate every outcome, grouped by coherent market.
            # Calibrating each outcome independently (market shrink + ML isotonic)
            # destroys the 1+X+2 == 1 (and Over+Under == 1) coherence. So we
            # calibrate first, then RE-NORMALIZE within each group before any
            # value test. Groups are keyed by (market_key, point): all of 1/X/2
            # share ("h2h", None); each totals line is its own pair.
            groups: dict[tuple, list[dict]] = {}
            for code, label, outcome_name, market_key, point, raw_model_prob in outcome_map:
                best = extract_best_odds(event, outcome_name, market_key=market_key, point=point)
                # Market shrinkage needs the outcome's own odds. Without a price
                # we still ML-calibrate the raw prob so the group normalizes on
                # the full distribution (the leg just can't be bet anyway).
                shrunk = shrink_toward_market(raw_model_prob, best.price) if best else raw_model_prob
                cal = ml_calibrate(shrunk, _ml_segment(sport_key, market_key))
                groups.setdefault((market_key, point), []).append({
                    "code": code, "label": label, "outcome_name": outcome_name,
                    "market_key": market_key, "point": point,
                    "best": best, "cal": max(0.0, min(cal, 1.0)),
                })

            # ---- Stage 2 : renormalize within each group, then test value.
            for (market_key, point), members in groups.items():
                total_cal = sum(m["cal"] for m in members)
                for m in members:
                    m["model_prob"] = (m["cal"] / total_cal) if total_cal > 0 else m["cal"]

                group_names = {m["outcome_name"] for m in members}
                for m in members:
                    model_prob = m["model_prob"]
                    if model_prob < min_model_prob:
                        continue

                    best = m["best"]
                    if best is None or best.price < min_book_odds:
                        continue
                    # Discipline (anti "value-trap") : cap extreme longshots —
                    # model error grows with odds — and require real conviction on
                    # underdogs. The edge formula (prob×odds−1) is easiest to
                    # satisfy on high-odds outcomes the market priced as unlikely
                    # (and is usually right about), so we gate those out.
                    if max_book_odds > 0.0 and best.price > max_book_odds:
                        continue
                    if underdog_odds > 0.0 and best.price >= underdog_odds and model_prob < underdog_min_prob:
                        continue

                    # No-vig gate (adverse-selection guard) : require the model to
                    # beat the market's *fair* (vig-removed) CONSENSUS line — not
                    # merely the single best price, which is often the one book
                    # whose line is most stale. With novig_required, a pick with
                    # NO consensus to validate against is DROPPED rather than
                    # silently allowed (thin markets are where the model is worst).
                    if min_edge_vs_novig > 0.0:
                        novig = _novig_fair_prob(event, m["outcome_name"], market_key, point, group_names)
                        if novig is None or novig <= 0.0:
                            if novig_required:
                                continue
                        elif (model_prob / novig - 1.0) < min_edge_vs_novig:
                            continue

                    # value_edge stays computed against the BEST available price —
                    # that's the real EV of the bet you'd actually place.
                    edge = round(model_prob * best.price - 1.0, 4)
                    if edge < min_value_edge:
                        continue

                    # Reliability is computed BEFORE Kelly so we can down-weight
                    # the stake for low-confidence picks (huge-edge / small-sample
                    # artifacts get ~reliability× of a full-conviction stake).
                    from betbot.reliability import compute_reliability
                    reliability = compute_reliability(
                        model_prob=model_prob,
                        value_edge=edge,
                        model_type=probs.model,
                        n_matches=probs.n_matches if probs.n_matches > 0 else None,
                    )

                    stake = kelly_stake(model_prob, best.price, bankroll,
                                        kelly_fraction, reliability=reliability)
                    if require_positive_stake and stake == 0.0:
                        # A genuine edge zeroed by low reliability is dropped here —
                        # surface it so filtered picks aren't silently invisible.
                        # (The ×1000 parlay pool passes require_positive_stake=False:
                        # leg eligibility there is about EV, not stake sizing.)
                        logger.debug(
                            "drop %s %s/%s edge=%+.1f%% rel=%.2f → stake 0",
                            event_id, m["code"], m["outcome_name"], edge * 100, reliability,
                        )
                        continue

                    all_bets.append(ValueBet(
                        event_id=event_id,
                        sport_key=sport_key,
                        home_team=home,
                        away_team=away,
                        league_label=league_label,
                        market=market_key,
                        selection_code=m["code"],
                        selection_label=m["label"],
                        model_prob=round(model_prob, 4),
                        best_odds=best.price,
                        best_book=best.bookmaker,
                        value_edge=edge,
                        kelly_stake=stake,
                        lambda_home=probs.lambda_home,
                        lambda_away=probs.lambda_away,
                        model_type=probs.model,
                        reliability=reliability,
                    ))

            # ---- Stage 3 : derived markets (Double Chance + Draw No Bet).
            # Deterministic functions of the CALIBRATED 1/X/2 above, priced from
            # each book's own 1X2 (vig-inclusive). They add lower-variance options
            # and ideal favorite legs for combos at ZERO extra quota. Skipped for
            # tennis/basketball (no draw) and when 1/X/2 wasn't fully produced.
            if derive_dc_dnb and not is_tennis and not is_basketball:
                h2h_members = {m["code"]: m for m in groups.get(("h2h", None), [])}
                if {"1", "X", "2"} <= h2h_members.keys():
                    p1 = h2h_members["1"]["model_prob"]
                    px = h2h_members["X"]["model_prob"]
                    p2 = h2h_members["2"]["model_prob"]
                    win_no_draw = p1 + p2
                    dc_dnb_odds = _derive_dc_dnb_odds(event, home, away)
                    derived_specs = [
                        ("1X",   "Double chance 1X (domicile ou nul)",       "double_chance", p1 + px),
                        ("X2",   "Double chance X2 (nul ou extérieur)",      "double_chance", px + p2),
                        ("12",   "Double chance 12 (domicile ou extérieur)", "double_chance", p1 + p2),
                        ("DNB1", "Domicile — remb. si nul (Draw No Bet)",    "draw_no_bet",
                         (p1 / win_no_draw) if win_no_draw > 0 else 0.0),
                        ("DNB2", "Extérieur — remb. si nul (Draw No Bet)",   "draw_no_bet",
                         (p2 / win_no_draw) if win_no_draw > 0 else 0.0),
                    ]
                    for code, label, mkt_key, model_prob in derived_specs:
                        if model_prob < min_model_prob:
                            continue
                        best = dc_dnb_odds.get(code)
                        # DC/DNB are low-odds by nature → their own (lower) odds
                        # floor, not min_book_odds which is tuned for 1X2/totals.
                        if best is None or best.price < derived_min_odds:
                            continue
                        if max_book_odds > 0.0 and best.price > max_book_odds:
                            continue
                        if underdog_odds > 0.0 and best.price >= underdog_odds and model_prob < underdog_min_prob:
                            continue
                        # No no-vig gate here: the price is already the book's own
                        # vig-inclusive 1X2 recombined, so there is no separate
                        # consensus to beat — the edge below is the honest EV.
                        edge = round(model_prob * best.price - 1.0, 4)
                        if edge < derived_min_edge:
                            continue
                        from betbot.reliability import compute_reliability
                        reliability = compute_reliability(
                            model_prob=model_prob,
                            value_edge=edge,
                            model_type=probs.model,
                            n_matches=probs.n_matches if probs.n_matches > 0 else None,
                            skip_extreme_prob_penalty=True,  # high DC prob is by design
                        )
                        stake = kelly_stake(model_prob, best.price, bankroll,
                                            kelly_fraction, reliability=reliability)
                        if require_positive_stake and stake == 0.0:
                            continue
                        all_bets.append(ValueBet(
                            event_id=event_id,
                            sport_key=sport_key,
                            home_team=home,
                            away_team=away,
                            league_label=league_label,
                            market=mkt_key,
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
                            reliability=reliability,
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


def _orient_h2h(h2h_lookup: dict, home: str, away: str) -> dict | None:
    """
    Return H2H stats oriented from the home_team's perspective, given an
    alphabetically-keyed pair dict (keys = (team_a, team_b) with team_a < team_b).
    Returns None when the pair has no recorded history.
    """
    if not h2h_lookup:
        return None
    a, b = (home, away) if home < away else (away, home)
    row = h2h_lookup.get((a, b))
    if not row:
        return None
    if home == a:
        return {
            "n_matches": row["team_a_wins"] + row["draws"] + row["team_b_wins"],
            "home_wins": row["team_a_wins"],
            "draws":     row["draws"],
            "away_wins": row["team_b_wins"],
            "home_goals_avg": row["team_a_goals_avg"],
            "away_goals_avg": row["team_b_goals_avg"],
        }
    return {
        "n_matches": row["team_a_wins"] + row["draws"] + row["team_b_wins"],
        "home_wins": row["team_b_wins"],
        "draws":     row["draws"],
        "away_wins": row["team_a_wins"],
        "home_goals_avg": row["team_b_goals_avg"],
        "away_goals_avg": row["team_a_goals_avg"],
    }


def _novig_fair_prob(
    event: dict,
    outcome_name: str,
    market_key: str,
    point: float | None,
    group_names: set[str],
) -> float | None:
    """
    Consensus *no-vig* (vig-removed) fair probability for `outcome_name`,
    averaged across every bookmaker that prices the FULL market group.

    Removing the margin requires the complete set of mutually-exclusive
    outcomes (1/X/2, or Over/Under for one line). For each book that quotes the
    whole group we divide its raw implied probabilities by their overround, then
    weight-average across books (sharper books weigh more — models.BOOK_WEIGHTS).

    Returns None when no book prices the whole group, so the caller's no-vig gate
    ABSTAINS on thin markets rather than blocking a pick on missing data.
    """
    from betbot.models import BOOK_WEIGHTS, DEFAULT_BOOK_WEIGHT

    acc = 0.0
    total_w = 0.0
    for bm in event.get("bookmakers", []):
        weight = BOOK_WEIGHTS.get(bm.get("key", ""), DEFAULT_BOOK_WEIGHT)
        prices: dict[str, float] = {}
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for o in mkt.get("outcomes", []):
                nm = o.get("name")
                if nm not in group_names:
                    continue
                if point is not None and o.get("point") not in (point, str(point)):
                    continue
                try:
                    price = float(o["price"])
                except (KeyError, ValueError, TypeError):
                    continue
                if price > 1.0:
                    prices[nm] = price
        # Need the whole group priced by this book to strip the vig coherently.
        if len(prices) < len(group_names):
            continue
        implied = {nm: 1.0 / p for nm, p in prices.items()}
        overround = sum(implied.values())
        if overround <= 0:
            continue
        acc += (implied.get(outcome_name, 0.0) / overround) * weight
        total_w += weight

    if total_w <= 0:
        return None
    return acc / total_w


def _compute_probs(
    home: str,
    away: str,
    event: dict,
    team_stats_cache: dict,
    league_home_avg: float,
    league_away_avg: float,
    sport_key: str | None = None,
    h2h_lookup: dict | None = None,
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
            # Orient H2H by home_team for this fixture (lookup is alphabetical).
            h2h_oriented = _orient_h2h(h2h_lookup or {}, home, away)
            # Use blended model (Dixon-Coles + xG + ELO + H2H) — auto-degrades
            # when any signal is missing (legacy rows / fresh install / no past
            # matchup between these two teams).
            # Per-league tuned blend weights (betbot.tuning) when available —
            # otherwise blended_match_probs uses its hardcoded defaults.
            _tuned = _get_blend_weights(sport_key)
            result = blended_match_probs(
                home_stats=home_stats,
                away_stats=away_stats,
                league_home_avg=league_home_avg,
                league_away_avg=league_away_avg,
                weather_modifier=_weather_factor(home, event.get("commence_time"), sport_key),
                # Attack modifier = injuries × rest/congestion fatigue (both ≤1.0).
                home_attack_mod=_injury_factor(home, sport_key)
                * _fatigue_factor(home, sport_key, event.get("commence_time")),
                away_attack_mod=_injury_factor(away, sport_key)
                * _fatigue_factor(away, sport_key, event.get("commence_time")),
                sport_key=sport_key,   # propagates to per-league Dixon-Coles τ
                h2h=h2h_oriented,
                **({"elo_weight": _tuned[0], "xg_weight": _tuned[1]} if _tuned else {}),
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

# Conservative multiplier applied to a parlay's combined probability per EXTRA
# leg sharing the same league (same day). Legs in the same league carry mild
# positive correlation (shared conditions), so the independence product
# over-credits diversification. 0.97 nudges the ranking toward genuinely
# diversified combos and keeps the displayed EV honest; 1.0 disables it.
CORRELATION_HAIRCUT = 0.97


def build_parlays(
    bets: list[ValueBet],
    n_legs: int = 3,
    top_n: int = 3,
    min_combined_odds: float = 2.0,
    diversify_across_parlays: bool = True,
) -> list[Parlay]:
    """
    Generate n-leg parlays from ranked value bets.

    Constraints applied in order :
      - Within a parlay : no two legs from the same match (always).
      - Combined odds ≥ min_combined_odds (filter out low-payout combos).
      - When `diversify_across_parlays=True` (default) : ACROSS all returned
        parlays, each event appears in at most ONE parlay. Prevents a single
        upset from killing multiple parlays — the most common complaint when
        the same high-edge pick gets stamped into every top-EV combo.

    Ranking : combined expected value, descending. With diversification on,
    we walk the sorted list and greedy-pick the next parlay whose events
    are all disjoint from any already-selected parlay.

    Returns up to `top_n` parlays (may return fewer if the diversification
    constraint exhausts the disjoint pool — preferred to silent overlap).
    """
    parlays: list[Parlay] = []

    for combo in itertools.combinations(bets, n_legs):
        # Within-parlay constraint : no two legs on the same event.
        event_ids = [b.event_id for b in combo]
        if len(event_ids) != len(set(event_ids)):
            continue

        combined_odds = 1.0
        raw_prob = 1.0
        sport_counts: dict[str, int] = {}
        for bet in combo:
            combined_odds *= bet.best_odds
            raw_prob *= bet.model_prob
            sport_counts[bet.sport_key] = sport_counts.get(bet.sport_key, 0) + 1

        combined_odds = round(combined_odds, 2)
        if combined_odds < min_combined_odds:
            continue

        # Correlation haircut : legs from the SAME league (same day) aren't fully
        # independent, so the naive product over-states diversification. Penalize
        # per EXTRA same-league leg so genuinely diversified parlays rank above
        # concentrated ones and the displayed EV stays honest. (Same-match legs
        # are already excluded by the event_id check above.)
        extra_corr = sum(c - 1 for c in sport_counts.values() if c > 1)
        correlated = extra_corr > 0
        combined_prob = raw_prob * (CORRELATION_HAIRCUT ** extra_corr)
        combined_ev = round((combined_prob * combined_odds - 1.0) * 100, 2)

        parlays.append(Parlay(
            bets=list(combo),
            combined_odds=combined_odds,
            combined_prob=round(combined_prob, 4),
            combined_ev=combined_ev,
            correlated=correlated,
        ))

    parlays.sort(key=lambda p: p.combined_ev, reverse=True)

    if not diversify_across_parlays:
        return parlays[:top_n]

    # Greedy event-disjoint selection. Walking the sorted list and skipping
    # any parlay whose events overlap with already-chosen parlays guarantees
    # the top_n returned share no match — a single failing match can take
    # down at most ONE of the parlays. We trade EV for diversification : a
    # slightly lower-EV parlay can supplant a higher-EV one that overlaps.
    selected: list[Parlay] = []
    used_events: set[str] = set()
    for parlay in parlays:
        parlay_events = {bet.event_id for bet in parlay.bets}
        if parlay_events & used_events:
            continue
        selected.append(parlay)
        used_events.update(parlay_events)
        if len(selected) >= top_n:
            break
    return selected


# ---------------------------------------------------------------------------
# Target-odds parlay builder (×1000 "lottery" mode)
# ---------------------------------------------------------------------------

def build_target_parlays(
    bets: list[ValueBet],
    target_odds: float = 1000.0,
    max_legs: int = 12,
    top_n: int = 3,
    min_leg_odds: float = 1.2,
    max_leg_odds: float | None = None,
    require_positive_ev: bool = False,
) -> list[Parlay]:
    """
    Assemble parlays up to a combined-odds CEILING (e.g. ×1000) by greedily
    stacking the best value legs. Unlike build_parlays (all n-combinations at a
    FIXED n_legs, via itertools — explodes past ~5 legs), this scales to the
    ~8-15 legs a big combo needs.

    `target_odds` is a CAP, not a floor. The builder returns the requested
    `top_n` combos, each stacking as many quality favorites as fit WITHOUT the
    product exceeding `target_odds` — it does NOT require reaching it. So on a
    thin day you still get combos (e.g. ×300), just smaller; on a rich day they
    approach the ceiling. This matches the user's model: "×1000 = a max not to
    exceed; otherwise give me the requested number of combos."

    Strategy:
      - Candidate legs sorted by quality (value_edge × reliability, then prob).
      - Walk the pool, adding event-disjoint legs while combined_odds stays
        ≤ target_odds (skip any leg that would overshoot; stop at max_legs).
      - Across the `top_n` returned parlays, each event is used at most ONCE
        (same diversification guarantee as build_parlays).
      - A parlay is emitted once it has ≥2 legs.

    Stacking MORE disciplined favorites rather than a few longshots keeps the
    combo honest:
      - `max_leg_odds` caps the odds of any single leg, so we approach the
        ceiling by adding more *favorites* (well-calibrated, lower-margin)
        instead of padding with high-odds longshots likely to fail.
      - `require_positive_ev` drops any assembled combo whose combined EV is ≤ 0
        (e.g. eroded by the same-league correlation haircut) so we never surface
        a negative-EV ticket.

    A big combo is still a low-probability lottery on variance — the win here is
    that every leg carries a real edge and the ticket is +EV. Returns fewer (or
    zero) parlays only when the pool can't field ≥2 eligible favorites.
    """
    pool = [
        b for b in bets
        if b.best_odds >= min_leg_odds
        and (max_leg_odds is None or b.best_odds <= max_leg_odds)
    ]
    pool.sort(
        key=lambda b: (b.value_edge * (b.reliability or 1.0), b.model_prob),
        reverse=True,
    )

    parlays: list[Parlay] = []
    used_events: set[str] = set()

    for _ in range(max(1, top_n)):
        legs: list[ValueBet] = []
        leg_events: set[str] = set()
        combined_odds = 1.0
        # target_odds is a CEILING, not a floor : stack the best favorites whose
        # running product stays ≤ target_odds. A leg that would push the combo
        # OVER the ceiling is skipped (a smaller one may still fit) — we never
        # exceed it. We do NOT require reaching it : the result is simply the
        # biggest quality combo achievable up to the cap, even if that's ×300.
        for b in pool:
            if b.event_id in used_events or b.event_id in leg_events:
                continue
            if combined_odds * b.best_odds > target_odds:
                continue
            legs.append(b)
            leg_events.add(b.event_id)
            combined_odds *= b.best_odds
            if len(legs) >= max_legs:
                break

        # A combiné needs ≥2 legs — below that, try the next slot.
        if len(legs) < 2:
            continue

        sport_counts: dict[str, int] = {}
        raw_prob = 1.0
        for b in legs:
            raw_prob *= b.model_prob
            sport_counts[b.sport_key] = sport_counts.get(b.sport_key, 0) + 1
        extra_corr = sum(c - 1 for c in sport_counts.values() if c > 1)
        combined_prob = raw_prob * (CORRELATION_HAIRCUT ** extra_corr)
        combined_odds = round(combined_odds, 2)
        combined_ev = round((combined_prob * combined_odds - 1.0) * 100, 2)

        # Honesty gate : never surface a negative-EV ticket. The legs are each
        # +edge, but the same-league correlation haircut can erode the product —
        # skip without consuming the events so a different slot can still try.
        if require_positive_ev and combined_ev <= 0:
            continue

        parlays.append(Parlay(
            bets=list(legs),
            combined_odds=combined_odds,
            combined_prob=round(combined_prob, 6),
            combined_ev=combined_ev,
            correlated=extra_corr > 0,
        ))
        used_events |= leg_events

    return parlays


def enforce_disjoint_parlays(parlays: list[dict]) -> list[dict]:
    """Hard guarantee applied to EVERY parlay list returned to the user: across
    the returned combos, each match (event_id) appears in AT MOST ONE combo — so a
    single losing pick can sink at most one combo, never several.

    Greedy by input order (assumed best-first): keep a parlay only if none of its
    legs share an event with an already-kept parlay. Operates on the serialized
    dict form ({"legs": [{"event_id": ...}, ...]}). The deterministic builders
    already satisfy this (no-op there); this also hardens the AI-agent path, whose
    parlays come from an LLM and are not otherwise guaranteed disjoint.
    """
    kept: list[dict] = []
    used: set = set()
    for p in parlays or []:
        evs = {leg.get("event_id") for leg in (p.get("legs") or []) if leg.get("event_id")}
        if evs and (evs & used):
            continue
        kept.append(p)
        used |= evs
    return kept


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
        "soccer_efl_champ": "Championship",
        "soccer_netherlands_eredivisie": "Eredivisie",
        "soccer_portugal_primeira_liga": "Primeira Liga",
    }
    return labels.get(sport_key, sport_key.replace("soccer_", "").replace("_", " ").title())
