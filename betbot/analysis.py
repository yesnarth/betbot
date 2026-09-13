"""
Value bet detection, Kelly stake sizing, and parlay construction.

Takes raw events (from The Odds API) + team stats (from SQLite/Poisson model)
and returns ranked value bets and parlay combinations.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from betbot.calibration import is_edge_suspicious, shrink_toward_market
from betbot.ml import (
    calibrate as ml_calibrate,
    in_domain as _ml_in_domain,
    segment_for as _ml_segment,
)
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
    # Kickoff, ISO-8601, straight from the event. Carried ON THE OBJECT rather
    # than rebuilt by each caller: of the four `save_prediction` call sites only
    # one supplied it, so three paths silently stored NULL — and without a
    # kickoff the CLV snapshot cannot narrow to matches near kickoff, which is
    # what made CLV unaffordable in the first place.
    commence_time: str = ""
    # De-vigged CONSENSUS probability of this exact selection at pick time.
    #
    # It was already computed on every pick (as the adverse-selection gate) and
    # then thrown away. Keeping it is what makes the one question that matters
    # answerable: Brier(model_prob) vs Brier(market_prob) on the same graded
    # picks decides whether the model adds anything over the price. That
    # verdict cannot be reconstructed later — `best_odds` is one side of the
    # market, and de-vigging needs the whole outcome group, which is gone once
    # the scan ends.
    #
    # None when no bookmaker priced the full outcome group (thin market).
    market_prob: float | None = None
    # Which promise this pick belongs to. "valeur": the model claims an edge
    # over the price (rare by construction since the model was repaired — it
    # hugs the market, and disagreeing by the 7+ points the value gates demand
    # is exactly what an honest model stops doing). "favoris": model AND
    # de-vigged market AGREE the outcome is >= the confidence floor; no edge is
    # claimed, and the long-run expectation of the channel is minus the
    # bookmaker's margin. That trade-off is the owner's explicit, informed
    # choice: his goal is hit rate, not beating the market. The two channels
    # are stored, displayed and measured separately — mixing them would let
    # a flood of favourites mask the value channel's record.
    channel: str = "valeur"
    # SHADOW pick: recorded and graded, never recommended for placement.
    #
    # Totals were cut on evidence gathered while the goals model was broken
    # (`_prob_to_lambda` invented 4.61 expected goals on a favourite against
    # ~2.9 real, so the model over-bet Overs by construction). That bug is
    # fixed, which makes the -52% Over ROI stale — and no totals pick has been
    # produced since 2026-08-01, so the cut had become UNFALSIFIABLE, the exact
    # failure the residual contingent was built to prevent.
    #
    # Rebuilding the evidence must not cost real money on an untested
    # hypothesis. Shadow picks are born 'proposed' whatever AUTO_CONFIRM_PICKS
    # says, and are kept out of the email, so they accumulate a graded record
    # at zero stake.
    shadow: bool = False


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
    # German/Bulgarian corporate prefixes (2026-09-10 audit): 'VfL Wolfsburg'
    # was one shared 'vfl' away from VfL Bochum, 'PFC CSKA Sofia' one 'pfc'
    # from nothing useful. The city/club word is the identity, not these.
    'vfl', 'vfb', 'fsv', 'tsv', 'bsc', 'pfc',
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

# Direct full-name aliases: normalized Odds name → EXACT stored team_name.
# Used for clubs that api-football stores under a renamed / abbreviated /
# differently-transliterated name that token & fuzzy matching can't bridge
# (verified same club each). Resolved against the current league's cache only,
# so these never cross-match between leagues. Extend as new misses surface.
_TEAM_NAME_ALIASES: dict[str, str] = {
    # Finland
    'tps turku':             'Turku PS',
    # Brazil
    'atletico mineiro':      'Atletico-MG',
    'clube regatas brasil':  'CRB',          # "Clube de Regatas Brasil"
    # South Korea (club renamed Sangju Sangmu → Gimcheon Sangmu in 2021)
    'sangju sangmu':         'Gimcheon Sangmu FC',
    # France - the Odds API says "Lyon", api-football stores the official
    # "Olympique Lyonnais". No shared token and a low string ratio, so nothing
    # short of an explicit alias bridges it. Seen live on a Champions League
    # qualifier, where Lyon fell through to the consensus model.
    'lyon':                  'Olympique Lyonnais',
    # China (renames / alternate English names — verified same club)
    'chengdu rongcheng':     'Chengdu Better City',
    'zhejiang':              'Hangzhou Greentown',
    'dalian yingbo':         'Dalian Zhixing',
    # Champions-League night traps, all caught live 2026-09-08. The Odds API
    # speaks English club names; api-football stores the official ones. In a
    # continental pool full of near-namesakes, fuzzy matching resolved these
    # to the WRONG COUNTRY'S club — which then vetoed favourites the market
    # itself validated (a Poisson priced on a foreign namesake rarely reaches
    # the 0.70 agreement floor).
    'sporting lisbon':       'Sporting Clube de Portugal',   # was: Sporting Gijon
    'barcelona':             'FC Barcelona',                 # was: Barcelona SC (Ecuador)
    'red star belgrade':     'FK Crvena Zvezda',             # was: RED Star FC 93 (Paris)
    # Brazil — an all-generic-token name ('atletico' + 'mineiro') whose long
    # form the shared-generic guard would otherwise refuse to contain.
    'clube atletico mineiro': 'Atletico-MG',
    # Russia — api-football stores Dynamo Moscow as bare 'Dynamo'. Once
    # 'dynamo' is a generic token (it had been lending Moscow's form to
    # Kyiv, Batumi and Minsk), only an alias may bridge the bare row.
    'dynamo moscow': 'Dynamo',
    # Greece — the Odds API says 'Aris'/'Aris Thessaloniki', api-football
    # stores 'Aris Thessalonikis'; the trailing s breaks the token bridge.
    'aris': 'Aris Thessalonikis',
    'aris thessaloniki': 'Aris Thessalonikis',
    # France — same shape as 'lyon' above: no shared token bridges these.
    'red star':              'RED Star FC 93',               # Ligue 2, the actual Paris club
    'brest':                 'Stade Brestois 29',            # was: consensus all season
}


# Per-token spellings of the SAME word across providers. Applied during
# normalization so both sides converge before any matching is attempted.
#
# Measured 2026-08-09 in production, this exact gap produced two INVERTED
# predictions: the Odds API says "Dundee United" while api-football stores
# "Dundee Utd", so no exact match was found and the fallback quietly picked
# "Dundee" — a different club in the same league. Same in Russia:
# "FC Dynamo Makhachkala" was handed Dynamo Moscow's stats, while Dynamo
# Moscow itself matched nothing at all.
_TOKEN_SYNONYMS: dict[str, str] = {
    'utd': 'united',
    'dinamo': 'dynamo',
    # The Odds API anglicizes, api-football stores 'SK Slavia Praha' /
    # 'AC Sparta Praha'. Converging the city token is what lets 'praha' also
    # serve as a discriminating token below.
    'prague': 'praha',
    # From the 2026-09-10 /participants audit: 'Austria Wien' must keep
    # reaching 'Austria Vienna' once 'austria' becomes a generic token, and
    # 'Atletico-MG' is the Mineiro club ('America MG' likewise). Brazilian
    # clubs rebranded 'Athletico' (Paranaense) — same word, same role.
    'wien': 'vienna',
    'mg': 'mineiro',
    'athletico': 'atletico',
    'munich': 'munchen',
}

# Words whose whole purpose is telling two clubs of the same town apart.
# If the queried name carries one and a candidate does not, they are not the
# same club — however well the remaining tokens line up. This is what makes
# "Dundee United" refuse "Dundee" even when "Dundee Utd" is missing from the
# cache: a miss falls back to the consensus model, a false match silently
# prices the wrong team and is auto-confirmed as a real bet.
_DISCRIMINATING_TOKENS: frozenset = frozenset([
    'united', 'city', 'town', 'county', 'rovers', 'wanderers', 'albion',
    'forest', 'hotspur', 'orient', 'palace', 'argyle', 'athletic',
    'academical', 'alexandra', 'thistle', 'ii', 'b',
    # City names that tell same-named clubs of DIFFERENT countries apart —
    # the continental pool's version of Dundee vs Dundee United. 'praha'
    # refuses Sparta Rotterdam for a 'Sparta Prague' query (the synonym above
    # folds prague→praha first, so 'Slavia Prague' still reaches SK Slavia
    # Praha); 'gijon' refuses Sporting Gijon for any other Sporting; nothing
    # stored carries 'belgrade', so a Belgrade query declines to consensus
    # rather than borrow a Parisian or Dutch namesake.
    'praha', 'gijon', 'belgrade',
    # The Prague rivals share 'praha', so the CLUB tokens must discriminate
    # too: first verified in the container against the real pool, 'Sparta
    # Prague' had slid to SK Slavia Praha on the city token alone.
    'sparta', 'slavia',
    # 'RFC Liège' is not Standard: a query about the OTHER Liège club never
    # carries 'standard' (2026-09-10 /participants audit).
    'standard',
])

# Tokens that are GENERIC rather than discriminating: being the ONLY thing
# two names share proves nothing, but their presence on one side only proves
# nothing either — 'Atlético Huracán' IS 'Huracan' (api-football drops the
# prefix), while 'Atlético Huracán' is NOT 'Atletico Tucuman'. So they join
# _DISCRIMINATING_TOKENS in the shared-generic guard below and stay OUT of
# the one-side guard above it.
#
# Measured 2026-09-10 on the /participants audit (2 516 canonical names):
# these Latin-football generics play exactly the role 'united'/'city' play
# in Britain — identity lives in the OTHER word. False matches caught, each
# two different real clubs: Atlético Huracán→Atletico Tucuman, Deportes
# Iquique→Deportes Limache, Universidad Católica→Universidad de Chile (the
# Chilean rivals), Unión Española→Union La Calera, Nacional Potosí→Club
# Nacional, Defensor Sporting→Sporting Cristal, Sport Huancayo→Sport Recife,
# São Bernardo→Sao Paulo, Botafogo-SP→Botafogo (RJ), Austria Klagenfurt→
# Austria Lustenau, GV San José→San Lorenzo, Olimpia Asunción→Libertad
# Asuncion, Cerro Largo→Cerro Porteno, Alianza Lima→Alianza Atletico,
# Grêmio Novorizontino→Gremio, Atletico Mineiro→America Mineiro.
_GENERIC_TOKENS: frozenset = _DISCRIMINATING_TOKENS | frozenset([
    'atletico', 'deportes', 'deportivo', 'union', 'unido', 'nacional',
    'universidad', 'sporting', 'sport', 'real', 'austria', 'sao', 'san',
    'santa', 'fe', 'sp', 'cerro', 'alianza', 'asuncion', 'gremio',
    'mineiro', 'america', 'botafogo',
    # Second audit pass, Champions-League pool: club-family prefixes shared
    # across BORDERS. AEK Larnaca→AEK Athens, Aris Limassol→Aris
    # Thessalonikis, Dynamo Kyiv/Batumi/Minsk→Dynamo (Moscow's bare row —
    # Moscow itself now goes through an alias), PFC CSKA Sofia→Levski Sofia,
    # Viktoria Plzeň→Viktoria Köln, Víkingur Gøta→Vikingur Reykjavik,
    # FK Žalgiris (Vilnius)→Kauno Žalgiris, AC Virtus→Virtus Entella,
    # Inter Club d'Escaldes→Racing CLUB de Lens, and 'munchen' so a future
    # '1860 Munich' can never borrow Bayern.
    'club', 'sofia', 'viktoria', 'virtus', 'vikingur', 'zalgiris',
    'dynamo', 'aek', 'aris', 'munchen',
])


def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove common football suffixes/words."""
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    # Strip standalone numbers: year suffixes (1913, 1909…) but also the
    # short ones — '1. FSV Mainz 05', 'Sarpsborg 08', 'SC Dnipro-1'. Audit
    # 2026-09-10: the token '1' was the ONLY thing 'SC Dnipro-1' shared with
    # '1. FC Union Berlin', and it matched.
    s = re.sub(r'\b\d{1,4}\b', ' ', s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)         # keep only letters, digits, spaces
    words = [_TOKEN_SYNONYMS.get(w, w)
             for w in s.split() if w not in _STRIP_WORDS]
    return ' '.join(words)


def _normalize_name_full(name: str) -> str:
    """Like _normalize_name but KEEPS the suffix words.

    Collision fallback only: 'FC Barcelona' and 'Barcelona SC' are different
    clubs whose stripped forms are both 'barcelona'. Re-keying the colliding
    pair under 'fc barcelona' / 'barcelona sc' keeps both visible to the
    matcher, where the plain dict-comprehension let the LAST writer silently
    erase the other club from the index.
    """
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'\b\d{1,4}\b', ' ', s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return ' '.join(_TOKEN_SYNONYMS.get(w, w) for w in s.split())


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
    # Collision-aware build. Suffix-stripping maps 'FC Barcelona' and
    # 'Barcelona SC' (Ecuador) to the same 'barcelona': in a continental pool
    # the borrowed row was appended last and OVERWROTE the real one, so an
    # exact-normalized query returned the wrong country's club — measured live
    # on Champions-League day 2026-09-08. Colliding entries are re-keyed with
    # their suffixes kept, so both stay visible and a bare query must be
    # settled by alias or declined as ambiguous, never by dict insertion order.
    norm_index: dict[str, str] = {}
    collided: set[str] = set()
    for k in cache:
        n = _normalize_name(k)
        if n in collided:
            norm_index[_normalize_name_full(k)] = k
            continue
        prev = norm_index.get(n)
        if prev is not None and prev != k:
            del norm_index[n]
            collided.add(n)
            norm_index[_normalize_name_full(prev)] = prev
            norm_index[_normalize_name_full(k)] = k
        else:
            norm_index[n] = k
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

    # 1b. Direct full-name alias → exact stored name (renamed/abbreviated clubs).
    #     Resolved against THIS cache only, so it can't cross-match leagues.
    #     The raw-key lookup comes first: an alias target names the EXACT
    #     stored row, and when that row's stripped form collided with a
    #     namesake's (FC Barcelona / Barcelona SC) the normalized index no
    #     longer carries it under the stripped key.
    alias_target = _TEAM_NAME_ALIASES.get(norm_query)
    if alias_target:
        if alias_target in cache:
            return cache[alias_target], alias_target
        target_norm = _normalize_name(alias_target)
        if target_norm in norm_index:
            return cache[norm_index[target_norm]], norm_index[target_norm]

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
    #    Three guards, each earned: containment, no dropped discriminating
    #    word, and a strictly-better winner. The previous version tested only
    #    "shorter tokens are a subset of longer", so a stored short name
    #    swallowed any longer query built on it — see _TOKEN_SYNONYMS above
    #    for the two production cases this produced.
    #    Token containment and string similarity are scored TOGETHER, in one
    #    pass. They used to be two ordered steps, and the order was itself a
    #    bug: querying "Independiente Rivadavia" (Mendoza), token containment
    #    fired first and returned "Independiente" (Avellaneda, a different
    #    club) on the strength of one shared token, while the stored
    #    "Independ. Rivadavia" — the right club, merely abbreviated — was
    #    sitting one fuzzy comparison away at 0.93 similarity.
    query_tokens = frozenset(norm_query.split())
    if query_tokens:
        scored: list[tuple[int, float, str]] = []
        for norm_key, orig_key in norm_index.items():
            key_tokens = token_index[norm_key]
            if not key_tokens:
                continue
            # A discriminating word on one side only means two different clubs,
            # whatever the rest of the string does.
            if (query_tokens ^ key_tokens) & _DISCRIMINATING_TOKENS:
                continue
            # Shared GENERIC tokens are not identity. 'Manchester United' and
            # 'West Ham United FC' share 'united', so the guard above is
            # blind — and the 2026-09-10 /participants audit showed that with
            # the right club absent from the cache, one shared generic token
            # was enough to WIN: Colchester United→Manchester United FC (a
            # real EFL-Cup tie away), Bradford City→Salford City, Huddersfield
            # Town→Mansfield Town, Atlético Huracán→Atletico Tucuman. When
            # the ONLY words two names share are generic ones and either side
            # still carries leftover words, those leftovers are the identity —
            # and nothing matches them. 'Gremio' ⊂ 'Grêmio Novorizontino' is
            # exactly the trap: containment through a generic token is not
            # containment of a club. (A same-club long form of an all-generic
            # name — 'Clube Atlético Mineiro' → 'Atletico-MG' — goes through
            # _TEAM_NAME_ALIASES instead, which resolves before this loop.)
            common_tokens = query_tokens & key_tokens
            if (common_tokens and common_tokens <= _GENERIC_TOKENS
                    and ((query_tokens - common_tokens)
                         or (key_tokens - common_tokens))):
                continue
            common = len(common_tokens)
            ratio = SequenceMatcher(None, norm_query, norm_key).ratio()
            contained = (key_tokens.issubset(query_tokens)
                         or query_tokens.issubset(key_tokens))
            # 0.82, not 0.75: pure string similarity with ZERO shared tokens
            # is the weakest evidence there is, and the audit measured three
            # different clubs inside the old band — Cobreloa→Cobresal at
            # exactly 0.75, Barnsley→Burnley and Dartford→Watford at ~0.80.
            if not contained and ratio < 0.82 and common == 0:
                continue
            # True when the QUERY under-specifies: "Racing" against both
            # "Racing Club" and "Racing Santander". Several clubs answer to
            # it and nothing in the string says which.
            under_specified = query_tokens < key_tokens
            scored.append((common, ratio, orig_key, under_specified))

        if scored:
            # Shared tokens first, string similarity as the tie-break: that
            # ordering is what lets the abbreviated spelling of the RIGHT club
            # beat a shorter, wronger one.
            scored.sort(key=lambda t: (-t[0], -t[1]))
            best = scored[0]
            runner_up = scored[1] if len(scored) > 1 else None

            # Ambiguous evidence is not evidence — decline and fall back to
            # the consensus model. Two shapes of ambiguity, both real:
            #  * an exact tie on tokens AND similarity;
            #  * the query under-specifying several candidates equally, where
            #    the similarity tie-break would only be measuring which club
            #    has the shorter name.
            ambiguous = runner_up is not None and (
                (best[0], round(best[1], 3)) <= (runner_up[0], round(runner_up[1], 3))
                or (best[3] and runner_up[3] and best[0] == runner_up[0])
            )
            if not ambiguous and (best[0] > 0 or best[1] >= 0.82):
                return cache[best[2]], best[2]

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
    kelly_edge_cap: float = 0.0,
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
    # Cap the edge Kelly is allowed to act on BEFORE applying reliability.
    #
    # `reliability` is meant to shrink the stake of dubious picks, but it was
    # structurally neutralised: corr(reliability, value_edge) = -0.8311, so a
    # low-reliability pick always carried a large edge, and the two effects
    # cancelled almost exactly — measured corr(reliability, kelly_stake) =
    # +0.0034 across 310 production picks. The guard existed and did nothing.
    #
    # Kelly is also known to be badly behaved on an overstated edge: full_kelly
    # grows linearly with it, so a 50% phantom edge sizes 10x a 5% real one.
    # Capping the acting edge makes reliability the dominant term again.
    if kelly_edge_cap > 0.0:
        capped = kelly_edge_cap / b if b > 0 else full_kelly
        full_kelly = min(full_kelly, capped)
    # Clamp reliability defensively; callers should already pass a [0, 1] value.
    rel = max(0.0, min(reliability, 1.0))
    fraction = min(full_kelly * kelly_fraction * rel, max_fraction)
    return round(fraction * bankroll, 2)


# ---------------------------------------------------------------------------
# Value detection
# ---------------------------------------------------------------------------

# A derived DC/DNB "best" price above (median × this) is treated as a stale /
# placeholder-line outlier and rejected. Normal book-to-book line-shopping on a
# coherent derived market stays well under +20% over the cross-book median; the
# fake edges we're killing (e.g. a symmetric 1X2 → DNB=2.00 on an illiquid
# league) sit 40-60%+ above it, so 1.20 catches them with margin.
DERIVED_ODDS_OUTLIER_MAX = 1.20


# Two-sided 1X2 whose home/away prices differ by less than this are treated as
# a placeholder rather than a real market. 2% is wide enough to catch the exact
# symmetric case with margin, narrow enough to keep genuinely balanced fixtures.
# A calibrated probability of exactly 0 is only credible if the model itself
# considered the outcome impossible. Above this raw threshold, a zero means the
# calibrator extrapolated outside its trained domain, not that the outcome
# cannot happen.
_CALIB_ANNIHILATION_FLOOR = 0.02

DEGENERATE_1X2_TOL = 0.02



def _totals_sampling_rate(direction: str) -> float:
    """Share of totals selections still allowed through, per direction.

    A zero rate makes the gate unfalsifiable — it suppresses the very data that
    could overturn it, while n=327 would be needed to settle the question. A
    residual contingent keeps the decision testable at a bounded cost.

    The two directions get different rates because the evidence differs, not
    because the market does. On 87 graded totals picks:

        Over  n=42  predicted 0.592 -> realised 0.286  ROI -52.0%  t=-4.33
        Under n=45  predicted 0.566 -> realised 0.400  ROI -22.5%  t=-1.55

    Over is established; Under is clearly negative but not significant, so it
    keeps a larger contingent. Both are overconfident — the market as a whole
    carries no edge, which is why the gate is no longer Over-only. Removing
    totals entirely takes the global ROI from -17.9% (n=227) to -6.2% (n=140).
    """
    var = "OVER_SAMPLING_RATE" if direction == "Over" else "UNDER_SAMPLING_RATE"
    default = "0.25" if direction == "Over" else "0.50"
    try:
        return min(1.0, max(0.0, float(os.getenv(var, default))))
    except ValueError:
        return float(default)


def _keep_totals_sample(event_id: str, direction: str) -> bool:
    """Deterministic sampling on (event id, direction).

    Deliberately NOT random: a rescan of the same match must reach the same
    verdict, otherwise repeated scans would quietly accumulate duplicate picks
    on the lucky draws and turn the contingent into cherry-picking. blake2b
    keeps it stable across processes and Python runs (unlike hash()).

    The direction is part of the key so Over and Under sample independently —
    hashing the event alone would correlate the two decisions.
    """
    key = f"{event_id}|{direction}".encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % 10_000
    return bucket < int(_totals_sampling_rate(direction) * 10_000)


def _derive_dc_dnb_odds(event: dict, home: str, away: str) -> tuple[dict, dict]:
    """
    Best available Double Chance / Draw No Bet decimal odds, DERIVED from each
    bookmaker's own 1/X/2 prices. Zero extra API quota — bookmakers construct
    these markets exactly this way, so the derivation invents no free money:

        q1,qX,q2 = 1/o1, 1/oX, 1/o2   (that book's vig-inclusive implieds)
        S        = q1+qX+q2           (that book's overround, ≈1.05-1.07)
        Double Chance   1X = 1/(q1+qX)   X2 = 1/(qX+q2)   12 = 1/(q1+q2)
        Draw No Bet   home = (q1+q2)/(q1·S)   away = (q1+q2)/(q2·S)

    THE `·S` ON DRAW NO BET IS NOT COSMETIC. Double Chance sums raw implieds,
    so the book's margin survives and the derived price is conservative: the
    offered implied probability is exactly S times the fair one. Draw No Bet is
    a RATIO of implieds — `(q1+q2)/q1` — in which the margin cancels out
    exactly, yielding the true-fair price no bookmaker would ever offer.

    That asymmetry was the bug. The two derived markets were being compared to
    the model on different footings, and production proved it: on 310 picks,
    double_chance showed a modest +4.5..+7.7% edge and was well calibrated
    (68% predicted, 68% realised, ROI +2.7%), while draw_no_bet showed a
    +23.9..+27.3% edge and was 21 points overconfident (66% predicted, 45%
    realised, ROI -18.7%). Dividing by S puts DNB on exactly the same
    conservative footing as DC — offered implied = S × fair — with no magic
    constant: a tight book gets a small haircut, a loose one a large haircut.

    Only books that quote the FULL 1/X/2 are used (coherent derivation).
    Returns {code: BestOdds} taking the best price per selection across books.
    """
    from statistics import median

    from betbot.models import BestOdds
    # Collect EVERY book's derived price per code, so we can both line-shop (best)
    # AND sanity-check the best against the cross-book consensus (median). On
    # illiquid leagues a single stale/placeholder book (symmetric 1X2 → DNB=2.00)
    # would otherwise set an outlier "best" and invent a fake edge.
    from betbot.bookmaker_filter import is_allowed

    prices: dict[str, list[tuple[float, str]]] = {}
    for bm in event.get("bookmakers", []):
        # Derived DC/DNB prices inherit the whitelist: a derived price is only
        # as real as the 1/X/2 triplet it is built from.
        if not is_allowed(bm):
            continue
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
        # Reject a degenerate source line BEFORE deriving anything from it.
        # A perfectly (or near-perfectly) symmetric 1X2 is a placeholder for a
        # market the book has not really made — it derives to DNB = 2.000 exactly,
        # which then reads as a huge edge against any model. Production found 25
        # such rows, all Betfair, all DNB at exactly 2.000, average claimed edge
        # 57.8%, and 0/25 ever resolved (the events themselves were not real
        # fixtures). The existing median×1.20 outlier guard cannot catch them:
        # it fails precisely when SEVERAL books publish the same placeholder.
        if abs(o["1"] - o["2"]) / max(o["1"], o["2"]) < DEGENERATE_1X2_TOL:
            continue
        q1, qx, q2 = 1.0 / o["1"], 1.0 / o["X"], 1.0 / o["2"]
        # Overround of this book's own 1X2. Guarded: a book quoting a sub-100%
        # book (arbitrage or stale data) must not INFLATE the DNB price.
        overround = max(1.0, q1 + qx + q2)
        title = bm.get("title", bm.get("key", "?"))
        derived = {
            "1X":   1.0 / (q1 + qx),
            "X2":   1.0 / (qx + q2),
            "12":   1.0 / (q1 + q2),
            # `/ overround` restores the margin that the (q1+q2)/q1 ratio
            # cancels — see the docstring. Without it DNB is priced true-fair
            # and every model disagreement reads as a huge phantom edge.
            "DNB1": (q1 + q2) / (q1 * overround),
            "DNB2": (q1 + q2) / (q2 * overround),
        }
        for code, price in derived.items():
            if price <= 1.0:
                continue
            prices.setdefault(code, []).append((round(price, 3), title))

    best: dict = {}
    consensus: dict = {}
    for code, lst in prices.items():
        bp, bb = max(lst, key=lambda x: x[0])
        best[code] = BestOdds(outcome_name=code, price=bp, bookmaker=bb)
        consensus[code] = {"median": round(median(p for p, _ in lst), 3),
                           "n": len(lst)}
    return best, consensus


def detect_value_bets(
    events_by_sport: dict[str, list[dict]],
    match_history_by_sport: dict[str, list[dict]],
    bankroll: float,
    kelly_fraction: float = 0.25,
    min_value_edge: float = 0.04,
    max_value_edge: float = 0.0,
    min_model_prob: float = 0.40,
    # Totals get their own, lower floor. Not a relaxation of discipline: the
    # 1X2 floor and this market are on different scales, and applying the
    # former to the latter silences it entirely instead of protecting it.
    # Shadow-only, so a looser floor never reaches the user's stake.
    min_model_prob_totals: float = 0.55,
    favorites_channel: bool = False,
    favorites_min_prob: float = 0.70,
    favorites_min_odds: float = 1.20,
    min_book_odds: float = 1.50,
    min_edge_vs_novig: float = 0.0,
    require_positive_stake: bool = True,
    max_book_odds: float = 0.0,
    underdog_odds: float = 0.0,
    underdog_min_prob: float = 0.0,
    novig_required: bool = False,
    derive_dc_dnb: bool = True,
    derive_dnb: bool = True,
    allow_totals_over: bool = True,
    kelly_edge_cap: float = 0.0,
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
    from collections import defaultdict
    _funnel: dict = defaultdict(int)
    _favorites: list[ValueBet] = []
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
                    # points (typically 0.5, 1.5, 2.5, 3.5). extract_best_odds
                    # silently returns None when a bookmaker doesn't quote the
                    # specific point, so the loop just skips those legs.
                    # O05 (≥1 goal) is the highest-prob, earliest-resolving total.
                    ("O05", "Plus de 0.5 but",     "Over",  "totals", 0.5,  probs.over_05),
                    ("U05", "Moins de 0.5 but",    "Under", "totals", 0.5,  probs.under_05),
                    ("O15", "Plus de 1.5 buts",    "Over",  "totals", 1.5,  probs.over_15),
                    ("U15", "Moins de 1.5 buts",   "Under", "totals", 1.5,  probs.under_15),
                    ("O25", "Plus de 2.5 buts",    "Over",  "totals", 2.5,  probs.over_25),
                    ("U25", "Moins de 2.5 buts",   "Under", "totals", 2.5,  probs.under_25),
                    ("O35", "Plus de 3.5 buts",    "Over",  "totals", 3.5,  probs.over_35),
                    ("U35", "Moins de 3.5 buts",   "Under", "totals", 3.5,  probs.under_35),
                ]
                # Over selections are gated — but NOT for the reason first
                # given, and not to zero.
                #
                # The original justification was "the goals model over-predicts
                # in ONE direction". That claim does not survive: the Over-Under
                # difference is significant under no tested split (p=0.076 raw,
                # 0.243 after the odds cap, 0.936 pooled within league). Unders
                # lose too (-7.5%, n=21). The real finding is that the whole
                # totals market carries no edge — the model is beaten by the raw
                # bookmaker price in both directions.
                #
                # What DOES hold, on the population that the current filters
                # actually admit: 26 Over picks survive MAX_BOOK_ODDS=2.22 and
                # return -40.5%, CI95 [-71.5, -7.6] — entirely below zero. And
                # that -40.5% is an UPPER bound: those prices came from
                # exchanges ~3% above what betclic/bet365 would have offered.
                # The correct headline is -51.6% on n=32 (-40.5% on n=26), not
                # the -54.5% first quoted, which was the O2.5 cell alone.
                #
                # Scope note: O05 is unreachable regardless — P(O0.5)=0.924 puts
                # its fair price at 1.08, far under MIN_BOOK_ODDS=1.50.
                #
                # A hard zero would make this decision UNFALSIFIABLE: it removes
                # the very data that could overturn it, while n=327 would be
                # needed to settle the question to +/-15 points. So a fixed
                # fraction still goes through, sampled deterministically on the
                # event id so a rescan of the same match always decides the same
                # way (no double counting, no cherry-picking).
                if not allow_totals_over:
                    outcome_map = [
                        o for o in outcome_map
                        if o[2] not in ("Over", "Under")
                        or _keep_totals_sample(event_id, o[2])
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
                _seg = _ml_segment(sport_key, market_key)
                cal = ml_calibrate(shrunk, _seg)
                groups.setdefault((market_key, point), []).append({
                    "code": code, "label": label, "outcome_name": outcome_name,
                    "market_key": market_key, "point": point,
                    "best": best, "cal": max(0.0, min(cal, 1.0)),
                    # The pre-calibration probability, kept as the fallback the
                    # degeneracy guard below falls back TO.
                    "raw": max(0.0, min(raw_model_prob, 1.0)),
                    "in_domain": _ml_in_domain(shrunk, _seg),
                })

            # ---- Stage 2 : renormalize within each group, then test value.
            for (market_key, point), members in groups.items():
                total_cal = sum(m["cal"] for m in members)
                total_raw = sum(m["raw"] for m in members)

                # DEGENERACY GUARD — calibration may reshape a distribution,
                # it may never delete an outcome the model considered possible.
                #
                # Renormalising within the group is what keeps 1+X+2 == 1, but
                # it also means that if calibration drives members to zero, the
                # survivor is handed the entire probability mass. That is how
                # every pick since 05/08 was born at model_prob = 1.000 on
                # matches the market priced near 1.75: the calibrator, fitted
                # only on picks above MIN_MODEL_PROB, mapped the draw and the
                # underdog to 0.0 and the renormalisation did the rest. The
                # Poisson lambdas underneath were perfectly sane the whole time,
                # which is precisely why nothing looked broken.
                # ALL OR NOTHING. Calibrating the favourite while leaving the
                # draw untouched, then renormalising, reshapes the distribution
                # by an amount nobody chose. And the calibrator, fitted on
                # selected picks, has a domain that never reaches a draw's
                # probability — so for a full 1X2 the honest answer is "none".
                _partial = any(not m["in_domain"] for m in members)
                _annihilated = [m for m in members
                                if m["cal"] <= 0.0 and m["raw"] >= _CALIB_ANNIHILATION_FLOOR]
                if total_cal <= 0 or _annihilated or _partial:
                    if _annihilated or total_cal <= 0:
                        logger.warning(
                            "calibration dégénérée sur %s/%s (%s) — repli sur "
                            "les probabilités brutes du modèle",
                            event_id, market_key,
                            ", ".join(f"{m['code']}:{m['raw']:.2f}→0"
                                      for m in _annihilated) or "somme nulle",
                        )
                    else:
                        logger.debug(
                            "%s/%s hors domaine du calibrateur — groupe laissé brut",
                            event_id, market_key,
                        )
                    for m in members:
                        m["model_prob"] = (m["raw"] / total_raw) if total_raw > 0 else 0.0
                else:
                    for m in members:
                        m["model_prob"] = m["cal"] / total_cal

                group_names = {m["outcome_name"] for m in members}
                _is_totals = market_key == "totals"
                # The 0.70 confidence floor is what produces the 77% hit rate,
                # and it is structurally incompatible with this market: measured
                # on production, Over 2.5 averages 0.574 (5 of 29 would clear
                # 0.70) and Over 3.5 averages 0.514. Judging totals by the 1X2
                # floor does not make them safe, it makes them absent — which is
                # how the contingent meant to keep the question testable ended
                # up producing nothing at all.
                _floor = min_model_prob_totals if _is_totals else min_model_prob
                # The consensus model reads the goals level FROM the totals
                # market since `_prob_to_lambda` was fixed, so it reproduces the
                # price and its edge is just the margin, negative. It has
                # nothing to say about goals — 71 of 97 historical totals picks
                # came from it, and that is precisely where the losses came
                # from. Blocked explicitly rather than left to the edge filter.
                _consensus = str(probs.model or "").startswith("consensus")
                if _is_totals and _consensus:
                    continue

                for m in members:
                    _funnel["candidats"] += 1
                    model_prob = m["model_prob"]
                    if model_prob < _floor:
                        _funnel["sous_plancher_proba"] += 1
                        continue

                    best = m["best"]
                    # Two distinct failures, two counters. They shared one key
                    # ("sans_prix_ou_cote_basse") and that hid which of the two
                    # actually starves the scan. Measured 2026-09-13 over 12
                    # production scans: every single outcome clearing the 0.70
                    # floor died on this pair, and the merged counter could not
                    # say which half. The two call for opposite fixes — "no
                    # whitelisted book quotes it" is a coverage problem (widen
                    # BOOKMAKER_WHITELIST), "priced under min_book_odds" is the
                    # confidence-floor/odds-gate contradiction — so they must be
                    # countable apart.
                    if best is None:
                        _funnel["sans_prix"] += 1
                        continue

                    novig = _novig_fair_prob(
                        event, m["outcome_name"], market_key, point, group_names)

                    # FAVORIS CALIBRES — the agreement channel. Both the model
                    # AND the de-vigged market put this outcome at or above the
                    # confidence floor. No edge is claimed; the value gates
                    # below do not apply. Emitted BEFORE the min-odds gate
                    # because a genuine favourite prices under 1.50 by
                    # definition — that gate is the very reason the value
                    # channel goes quiet when the model is honest.
                    if (favorites_channel and not _is_totals
                            and model_prob >= favorites_min_prob
                            and novig is not None
                            and novig >= favorites_min_prob
                            and best.price >= favorites_min_odds
                            and (max_book_odds <= 0.0 or best.price <= max_book_odds)):
                        _funnel["favoris"] += 1
                        _favorites.append(ValueBet(
                            event_id=event_id, sport_key=sport_key,
                            home_team=home, away_team=away,
                            league_label=league_label, market=market_key,
                            selection_code=m["code"], selection_label=m["label"],
                            model_prob=round(model_prob, 4),
                            best_odds=best.price, best_book=best.bookmaker,
                            value_edge=round(model_prob * best.price - 1.0, 4),
                            kelly_stake=0.0,   # no edge claimed -> no Kelly sizing
                            lambda_home=probs.lambda_home,
                            lambda_away=probs.lambda_away,
                            model_type=probs.model,
                            reliability=1.0,
                            commence_time=event.get("commence_time", "") or "",
                            market_prob=round(novig, 4),
                            channel="favoris",
                        ))

                    # A probe has no stake, so price bounds are meaningless
                    # for it — and they were what starved the totals sample a
                    # second time (0 probes in 4 days: the Under side of a
                    # favourite prices ~1.30-1.45, under the 1.50 value gate).
                    if not _is_totals and best.price < min_book_odds:
                        _funnel["cote_basse"] += 1
                        continue
                    # Discipline (anti "value-trap") : cap extreme longshots —
                    # model error grows with odds — and require real conviction on
                    # underdogs. The edge formula (prob×odds−1) is easiest to
                    # satisfy on high-odds outcomes the market priced as unlikely
                    # (and is usually right about), so we gate those out.
                    if not _is_totals and max_book_odds > 0.0 and best.price > max_book_odds:
                        _funnel["cote_trop_haute"] += 1
                        continue
                    if underdog_odds > 0.0 and best.price >= underdog_odds and model_prob < underdog_min_prob:
                        _funnel["outsider_refuse"] += 1
                        continue

                    # No-vig gate (adverse-selection guard) : require the model to
                    # beat the market's *fair* (vig-removed) CONSENSUS line — not
                    # merely the single best price, which is often the one book
                    # whose line is most stale. With novig_required, a pick with
                    # NO consensus to validate against is DROPPED rather than
                    # silently allowed (thin markets are where the model is worst).
                    # Computed unconditionally: it is the reference the model
                    # is judged against, so it must be recorded even when the
                    # gate that consumes it is switched off.
                    # SHADOW TOTALS SKIP THE VALUE GATES — measurement, not value.
                    #
                    # A probe exists to answer "does the fixed goals model predict
                    # Overs?", which needs the calibration sample, not the +EV one.
                    # Requiring a probe to beat the price by 4% both starves the
                    # sample (zero probes recorded in the first 8 days of the
                    # season) and BIASES it: keeping only price-beating probes
                    # measures the tail, not the model. Floor, sampling
                    # contingent, Poisson-only and the odds bounds still apply;
                    # a probe is never emailed, never staked, never in the ROI.
                    if not _is_totals:
                        if min_edge_vs_novig > 0.0:
                            if novig is None or novig <= 0.0:
                                if novig_required:
                                    _funnel["novig_refuse"] += 1
                                    continue
                            elif (model_prob / novig - 1.0) < min_edge_vs_novig:
                                _funnel["novig_refuse"] += 1
                                continue

                    # value_edge stays computed against the BEST available price —
                    # that's the real EV of the bet you'd actually place.
                    edge = round(model_prob * best.price - 1.0, 4)
                    if not _is_totals and edge < min_value_edge:
                        _funnel["edge_insuffisant"] += 1
                        continue
                    # Upper edge cap. On a market quoted by several books, a
                    # 30%+ edge is not an opportunity — it is the model being
                    # wrong, or a stale price. `is_edge_suspicious` has existed
                    # in calibration.py since day one and was NEVER called from
                    # the production path. Production evidence: the picks above
                    # +20% edge lost consistently, and reliability actively made
                    # it worse by sizing them LARGER (draw_no_bet averaged the
                    # biggest stake in the whole database).
                    if not _is_totals and max_value_edge > 0.0 and is_edge_suspicious(edge, max_value_edge):
                        logger.debug(
                            "drop %s %s/%s : edge %+.1f%% > plafond %+.1f%% (artefact probable)",
                            event_id, m["code"], m["outcome_name"], edge * 100, max_value_edge * 100,
                        )
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
                                        kelly_fraction, reliability=reliability,
                                        kelly_edge_cap=kelly_edge_cap)
                    if _is_totals:
                        # A probe is never placed: zero stake, and the
                        # positive-stake requirement does not apply to it.
                        stake = 0.0
                    elif require_positive_stake and stake == 0.0:
                        # A genuine edge zeroed by low reliability is dropped here —
                        # surface it so filtered picks aren't silently invisible.
                        # (The ×1000 parlay pool passes require_positive_stake=False:
                        # leg eligibility there is about EV, not stake sizing.)
                        _funnel["mise_nulle"] += 1
                        logger.debug(
                            "drop %s %s/%s edge=%+.1f%% rel=%.2f → stake 0",
                            event_id, m["code"], m["outcome_name"], edge * 100, reliability,
                        )
                        continue
                    _funnel["retenus_totals" if _is_totals else "retenus"] += 1

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
                        commence_time=event.get("commence_time", "") or "",
                        market_prob=(round(novig, 4) if novig else None),
                        shadow=_is_totals,
                    ))

            # ---- Stage 3 : derived markets (Double Chance + Draw No Bet).
            # Deterministic functions of the CALIBRATED 1/X/2 above, priced from
            # each book's own 1X2 (vig-inclusive). They add lower-variance options
            # and ideal favorite legs for combos at ZERO extra quota. Skipped for
            # tennis/basketball (no draw) and when 1/X/2 wasn't fully produced.
            # Derived DC/DNB amplify model error on leagues WITHOUT team stats
            # (the consensus fallback): the model's disagreement with a coherent
            # market is just noise there, and the derivation turns it into a large
            # phantom edge (e.g. a symmetric 1X2 → DNB=2.0). Only derive when the
            # blended model actually had team data for this match.
            _is_consensus = str(probs.model or "").startswith("consensus")
            if derive_dc_dnb and not is_tennis and not is_basketball and not _is_consensus:
                h2h_members = {m["code"]: m for m in groups.get(("h2h", None), [])}
                if {"1", "X", "2"} <= h2h_members.keys():
                    p1 = h2h_members["1"]["model_prob"]
                    px = h2h_members["X"]["model_prob"]
                    p2 = h2h_members["2"]["model_prob"]
                    win_no_draw = p1 + p2

                    # Market reference for the derived markets, built with the
                    # SAME algebra the model uses on its own 1/X/2 — so the
                    # later Brier comparison stays apples-to-apples. Double
                    # chance is the one market the model is not beaten on, so
                    # it is the one that most needs a recorded reference.
                    _h2h_names = {m["outcome_name"] for m in groups.get(("h2h", None), [])}
                    _fair: dict[str, float | None] = {}
                    for _code in ("1", "X", "2"):
                        _fair[_code] = _novig_fair_prob(
                            event, h2h_members[_code]["outcome_name"],
                            "h2h", None, _h2h_names)
                    _have_fair = all(_fair[c] and _fair[c] > 0 for c in ("1", "X", "2"))
                    _fair_no_draw = ((_fair["1"] + _fair["2"]) if _have_fair else 0.0)
                    market_by_code: dict[str, float | None] = {
                        "1X": (_fair["1"] + _fair["X"]) if _have_fair else None,
                        "X2": (_fair["X"] + _fair["2"]) if _have_fair else None,
                        "12": (_fair["1"] + _fair["2"]) if _have_fair else None,
                        "DNB1": (_fair["1"] / _fair_no_draw) if _fair_no_draw > 0 else None,
                        "DNB2": (_fair["2"] / _fair_no_draw) if _fair_no_draw > 0 else None,
                    }

                    dc_dnb_odds, dc_dnb_cons = _derive_dc_dnb_odds(event, home, away)
                    derived_specs = [
                        ("1X",   "Double chance 1X (domicile ou nul)",       "double_chance", p1 + px),
                        ("X2",   "Double chance X2 (nul ou extérieur)",      "double_chance", px + p2),
                        ("12",   "Double chance 12 (domicile ou extérieur)", "double_chance", p1 + p2),
                        ("DNB1", "Domicile — remb. si nul (Draw No Bet)",    "draw_no_bet",
                         (p1 / win_no_draw) if win_no_draw > 0 else 0.0),
                        ("DNB2", "Extérieur — remb. si nul (Draw No Bet)",   "draw_no_bet",
                         (p2 / win_no_draw) if win_no_draw > 0 else 0.0),
                    ]
                    # Double Chance and Draw No Bet are built from the SAME 1/X/2
                    # but by different algebra, and they behave in opposite ways.
                    # DC is a SUM (p1+pX) — errors partly cancel: overconfidence
                    # +0.53 pt, Brier 0.1936 vs 0.1977 for the price, the only
                    # market where the model is not beaten. DNB is a RATIO
                    # p1/(p1+p2) — errors amplify: overconfidence +17.95 pts,
                    # ROI -18.7%. Matched on probability (0.60-0.80), 11.6 of the
                    # 17.4-point gap survives, so it is the derivation itself and
                    # not a probability-level effect. DNB also carried 44.8% of
                    # picks and 49% of the Kelly exposure. Hence its own switch.
                    if not derive_dnb:
                        derived_specs = [d for d in derived_specs if d[2] != "draw_no_bet"]
                    for code, label, mkt_key, model_prob in derived_specs:
                        if model_prob < min_model_prob:
                            continue
                        best = dc_dnb_odds.get(code)
                        # DC/DNB are low-odds by nature → their own (lower) odds
                        # floor, not min_book_odds which is tuned for 1X2/totals.
                        if best is None or best.price < derived_min_odds:
                            continue
                        # Fake-edge guard for DERIVED markets on illiquid leagues:
                        # a single stale/placeholder book (symmetric 1X2 → DNB=2.00)
                        # would set an outlier "best" and invent a huge phantom edge.
                        # Require ≥2 books to derive coherently, and reject a best
                        # price that's an outlier above the cross-book median.
                        cons = dc_dnb_cons.get(code)
                        if cons is None or cons["n"] < 2:
                            continue
                        if best.price > cons["median"] * DERIVED_ODDS_OUTLIER_MAX:
                            continue
                        if max_book_odds > 0.0 and best.price > max_book_odds:
                            continue
                        if underdog_odds > 0.0 and best.price >= underdog_odds and model_prob < underdog_min_prob:
                            continue
                        # Edge is computed on the median-consistent best price —
                        # the outlier filter above is what keeps it honest.
                        edge = round(model_prob * best.price - 1.0, 4)
                        if edge < derived_min_edge:
                            continue
                        # Same cap as the direct path — derived markets are
                        # where the phantom edges were largest (DNB averaged
                        # +47.7% before the overround fix above).
                        if max_value_edge > 0.0 and is_edge_suspicious(edge, max_value_edge):
                            logger.debug(
                                "drop derive %s %s : edge %+.1f%% > plafond %+.1f%%",
                                event_id, code, edge * 100, max_value_edge * 100,
                            )
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
                                            kelly_fraction, reliability=reliability,
                                            kelly_edge_cap=kelly_edge_cap)
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
                            commence_time=event.get("commence_time", "") or "",
                            market_prob=(round(market_by_code[code], 4)
                                         if market_by_code.get(code) else None),
                        ))

    # THE FUNNEL. On restart weekend 2026-08-22, 141 same-day fixtures and 132
    # Poisson teams produced ZERO singles, and nothing in the logs said which
    # gate was killing every candidate. A pipeline that can only say "0 found"
    # cannot be told apart from a broken one — this line makes the difference
    # observable.
    # One selection, one channel: if the value gates retained a pick, its
    # favourite twin is redundant (the value claim is strictly stronger).
    _value_keys = {(b.event_id, b.market, b.selection_code) for b in all_bets}
    _favorites[:] = [f for f in _favorites
                     if (f.event_id, f.market, f.selection_code) not in _value_keys]
    all_bets.extend(_favorites)

    _dropped = {k: v for k, v in _funnel.items()
                if v and k not in ("candidats", "retenus", "retenus_totals", "favoris")}
    logger.info(
        "Entonnoir : %d issue(s) examinée(s) → %d valeur, %d favori(s), %d sonde(s) totals | pertes : %s",
        _funnel["candidats"], _funnel["retenus"], len(_favorites), _funnel["retenus_totals"],
        ", ".join("%s=%d" % kv for kv in sorted(_dropped.items())) or "aucune",
    )
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
