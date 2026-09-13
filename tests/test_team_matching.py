"""
Team-name matching: a false match is worse than no match at all.

A miss degrades the match to the market-consensus model — weaker, but honest.
A FALSE match prices one club with another club's attack and defense, produces
a confident-looking probability, and under AUTO_CONFIRM_PICKS is recorded as a
real bet. Measured in production on 2026-08-09, two of these were live at once.
"""
from __future__ import annotations

import pytest

from betbot.analysis import _fuzzy_lookup, _invalidate_norm_cache, _normalize_name


def _cache(*names):
    return {n: f"stats-of-{n}" for n in names}


def _lookup(name, *stored):
    cache = _cache(*stored)
    _invalidate_norm_cache(cache)
    try:
        return _fuzzy_lookup(name, cache)[1]
    finally:
        _invalidate_norm_cache(cache)


# ---------------------------------------------------------------------------
# The two production failures
# ---------------------------------------------------------------------------

def test_dundee_united_is_not_dundee():
    """Both play the Scottish Premiership and face each other. The Odds API
    says "Dundee United", api-football stores "Dundee Utd", and the old
    subset rule handed over "Dundee"."""
    assert _lookup("Dundee United", "Dundee", "Dundee Utd") == "Dundee Utd"


def test_dundee_stays_dundee():
    assert _lookup("Dundee", "Dundee", "Dundee Utd") == "Dundee"


def test_dynamo_makhachkala_is_not_dynamo_moscow():
    """Stored as "Dinamo Makhachkala"; the old rule matched bare "Dynamo"."""
    assert _lookup("FC Dynamo Makhachkala",
                   "Dynamo", "Dinamo Makhachkala") == "Dinamo Makhachkala"


def test_dynamo_moscow_still_resolves_to_the_bare_name():
    """The mirror image of the bug: Dynamo Moscow used to match NOTHING while
    its stats were being handed to Makhachkala."""
    assert _lookup("Dinamo Moscow", "Dynamo", "Dinamo Makhachkala") == "Dynamo"


# ---------------------------------------------------------------------------
# A short stored name must not swallow a longer query
# ---------------------------------------------------------------------------

def test_short_name_does_not_swallow_a_discriminated_query():
    """Even when the right club is ABSENT from the cache, answering with the
    wrong one is not acceptable — a miss falls back to consensus."""
    assert _lookup("Dundee United", "Dundee") is None


@pytest.mark.parametrize("query,stored", [
    ("Manchester United", "Manchester City"),
    ("Sheffield United", "Sheffield Wednesday"),
    ("Bristol City", "Bristol Rovers"),
    ("Nottingham Forest", "Nottingham"),
])
def test_same_town_rivals_never_match(query, stored):
    assert _lookup(query, stored) is None


def test_ambiguous_evidence_is_declined():
    """Two candidates fitting equally well means we cannot tell them apart."""
    assert _lookup("Racing", "Racing Club", "Racing Santander") is None


# ---------------------------------------------------------------------------
# Legitimate matches must survive the tightening
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,stored,expected", [
    ("Zenit St Petersburg", "Zenit", "Zenit"),
    ("Hammarby IF", "Hammarby FF", "Hammarby FF"),
    ("Falkirk F.C.", "Falkirk", "Falkirk"),
    ("Jagiellonia Bialystok", "Jagiellonia", "Jagiellonia"),
    ("Zaglebie Lubin", "Zaglebie Lubin", "Zaglebie Lubin"),
    ("Spartak Moscow", "Spartak Moscow", "Spartak Moscow"),
])
def test_genuine_variants_still_resolve(query, stored, expected):
    assert _lookup(query, stored) == expected


def test_accents_are_still_bridged():
    assert _lookup("Zagłębie Lubin", "Zaglebie Lubin") == "Zaglebie Lubin"


def test_utd_and_united_normalize_together():
    assert _normalize_name("Dundee Utd") == _normalize_name("Dundee United")


def test_dinamo_and_dynamo_normalize_together():
    assert _normalize_name("Dinamo Kyiv") == _normalize_name("Dynamo Kyiv")


def test_empty_cache_is_a_clean_miss():
    assert _lookup("Anyone") is None


# ---------------------------------------------------------------------------
# Abbreviated spellings of the RIGHT club must beat a shorter WRONG one
# ---------------------------------------------------------------------------

def test_independiente_rivadavia_is_not_independiente():
    """Both play the Argentine Primera. api-football abbreviates the Mendoza
    club to "Independ. Rivadavia", so token containment fired first and
    returned Avellaneda's "Independiente" on one shared token — while the
    right club sat one fuzzy comparison away at 0.93."""
    assert _lookup("Independiente Rivadavia",
                   "Independiente", "Independ. Rivadavia") == "Independ. Rivadavia"


def test_independiente_alone_still_resolves():
    assert _lookup("Independiente",
                   "Independiente", "Independ. Rivadavia") == "Independiente"


@pytest.mark.parametrize("query,expected", [
    ("Talleres", "Talleres Cordoba"),
    ("Estudiantes", "Estudiantes L.P."),
    ("PAOK Thessaloniki", "PAOK"),
    ("Parma", "Parma Calcio 1913"),
    ("AZ Alkmaar", "AZ"),
    ("Feyenoord", "Feyenoord Rotterdam"),
])
def test_real_production_pairs_still_resolve(query, expected):
    """Sampled from the 298 live matches — the tightening must not cost these."""
    assert _lookup(query, expected) == expected


# ---------------------------------------------------------------------------
# Champions-League night traps — caught live 2026-09-08
# ---------------------------------------------------------------------------
# A continental pool holds near-namesakes from many countries at once. Every
# case below resolved to the WRONG COUNTRY'S club in production — and a
# Poisson priced on a foreign namesake rarely reaches the 0.70 agreement
# floor, so the false match didn't just misprice: it silently VETOED
# favourites the market itself validated.

def test_sporting_lisbon_is_not_sporting_gijon():
    assert _lookup("Sporting Lisbon", "Sporting Gijon",
                   "Sporting Clube de Portugal") == "Sporting Clube de Portugal"


def test_sporting_gijon_still_resolves_itself():
    assert _lookup("Sporting Gijon", "Sporting Gijon",
                   "Sporting Clube de Portugal") == "Sporting Gijon"


def test_barcelona_is_not_the_ecuadorian_namesake():
    """'FC Barcelona' and 'Barcelona SC' both strip to 'barcelona' — the
    normalized index let whichever was inserted LAST answer for both."""
    assert _lookup("Barcelona", "FC Barcelona", "Barcelona SC") == "FC Barcelona"
    assert _lookup("Barcelona", "Barcelona SC", "FC Barcelona") == "FC Barcelona"


def test_a_normalization_collision_declines_an_unaliased_bare_query():
    """Same collision, no alias to arbitrate: the bare query must decline to
    consensus, never be settled by dict insertion order."""
    assert _lookup("Testville", "FC Testville", "Testville SC") is None
    assert _lookup("Testville", "Testville SC", "FC Testville") is None


def test_a_normalization_collision_keeps_both_full_names_resolvable():
    assert _lookup("FC Testville", "FC Testville", "Testville SC") == "FC Testville"
    assert _lookup("Testville SC", "FC Testville", "Testville SC") == "Testville SC"


def test_sparta_prague_declines_rather_than_borrow_a_namesake():
    """The production pool holds SK Slavia Praha, Sparta Rotterdam AND Spartak
    Moscow — and no Sparta Praha. The first fix stopped Rotterdam but slid to
    SLAVIA on the shared city token (caught in the container, not by a test
    built on an invented cache): the club tokens must discriminate too."""
    assert _lookup("Sparta Prague", "SK Slavia Praha",
                   "Sparta Rotterdam", "Spartak Moscow") is None


def test_slavia_prague_still_reaches_praha():
    """The prague→praha synonym must keep the TRUE Czech matches working."""
    assert _lookup("Slavia Prague", "SK Slavia Praha",
                   "Sparta Rotterdam", "Spartak Moscow") == "SK Slavia Praha"


def test_sparta_rotterdam_still_resolves_itself():
    assert _lookup("Sparta Rotterdam", "SK Slavia Praha",
                   "Sparta Rotterdam", "Spartak Moscow") == "Sparta Rotterdam"


def test_red_star_belgrade_is_not_the_paris_club():
    assert _lookup("Red Star Belgrade", "RED Star FC 93") is None
    assert _lookup("Red Star Belgrade", "RED Star FC 93",
                   "FK Crvena Zvezda") == "FK Crvena Zvezda"


def test_the_paris_red_star_still_resolves_domestically():
    assert _lookup("Red Star", "RED Star FC 93", "Paris FC") == "RED Star FC 93"


def test_brest_reaches_its_official_name():
    """No shared token and a low ratio: only an alias bridges 'Brest' to
    'Stade Brestois 29'. It had been falling to consensus all season — in
    Ligue 1, a league with full stats and top xG coverage."""
    assert _lookup("Brest", "Stade Brestois 29", "Stade Rennais") == "Stade Brestois 29"


# ---------------------------------------------------------------------------
# /participants audit regressions — measured 2026-09-10
# ---------------------------------------------------------------------------
# 2 516 canonical Odds-API names replayed through this matcher exposed two
# systemic holes. (1) The one-side discriminating guard is blind when both
# clubs SHARE the generic token: with the right club absent from the cache,
# one shared 'united'/'city'/'atletico' was enough to win. (2) Pure string
# similarity with zero shared tokens accepted different clubs at 0.75-0.80.
# Every pair below is two REAL different clubs the audit caught live.

@pytest.mark.parametrize("query,wrong", [
    ("Colchester United", "Manchester United FC"),   # an actual EFL-Cup risk
    ("Manchester United", "West Ham United FC"),
    ("Manchester City", "Leicester City FC"),
    ("Bradford City", "Salford City"),
    ("Huddersfield Town", "Mansfield Town"),
    ("Doncaster Rovers", "Bristol Rovers"),
    ("Oldham Athletic", "Charlton Athletic FC"),
    ("Newport County", "Derby County FC"),
    ("Wycombe Wanderers", "Bolton Wanderers FC"),
    ("Brighton and Hove Albion", "West Bromwich Albion FC"),
    ("Atlético Huracán", "Atletico Tucuman"),
    ("Atlético Rafaela", "Atletico Tucuman"),
    ("Deportes Iquique", "Deportes Limache"),
    ("Deportivo Pereira", "Deportivo La Guaira"),
    ("Universidad Católica (CHI)", "Universidad de Chile"),
    ("Unión Española", "Union La Calera"),
    ("Curicó Unido", "Coquimbo Unido"),
    ("Nacional Potosí", "Club Nacional"),
    ("Defensor Sporting", "Sporting Cristal"),
    ("Sport Huancayo", "Sport Recife"),
    ("São Bernardo", "Sao Paulo"),
    ("Botafogo-SP", "Botafogo"),
    ("Colon de Santa Fe", "Union Santa Fe"),
    ("GV San José", "San Lorenzo"),
    ("Olimpia Asunción", "Libertad Asuncion"),
    ("Cerro Largo", "Cerro Porteno"),
    ("Alianza Lima", "Alianza Atletico"),
    ("Grêmio Novorizontino", "Gremio"),
    ("Austria Klagenfurt", "Austria Lustenau"),
    ("RFC Liège", "Standard Liege"),
    ("Seraing United", "Lommel United"),
    ("Atletico Mineiro", "America Mineiro"),   # the Belo Horizonte rivals
    ("Shenzhen Peng City FC", "Chengdu Better City"),
])
def test_shared_generic_token_is_not_identity(query, wrong):
    assert _lookup(query, wrong) is None


@pytest.mark.parametrize("query,wrong", [
    ("CD Cobreloa", "Cobresal"),      # ratio exactly 0.75 on the old floor
    ("Barnsley", "Burnley FC"),       # ~0.80
    ("Dartford FC", "Watford FC"),    # ~0.80
])
def test_zero_token_similarity_below_082_is_refused(query, wrong):
    assert _lookup(query, wrong) is None


@pytest.mark.parametrize("query,wrong", [
    # Second audit pass: club-family prefixes shared across BORDERS
    ("AEK Larnaca", "AEK Athens FC"),                 # Cyprus vs Greece
    ("Aris Limassol FC", "Aris Thessalonikis"),       # Cyprus vs Greece
    ("Dynamo Kyiv", "Dynamo"),                        # the bare row is Moscow
    ("FC Dinamo Batumi", "Dynamo"),
    ("FC Dinamo Minsk", "Dynamo"),
    ("PFC CSKA Sofia", "Levski Sofia"),               # the Sofia rivals
    ("Viktoria Plzeň", "FC Viktoria Köln"),           # Czechia vs Germany
    ("VfL Wolfsburg", "VfL Bochum"),                  # shared corporate prefix
    ("Víkingur Gøta", "Vikingur Reykjavik"),          # Faroe vs Iceland
    ("FK Žalgiris", "Kauno Žalgiris"),                # Vilnius vs Kaunas
    ("AC Virtus", "Virtus Entella"),                  # San Marino vs Italy
    ("Inter Club d'Escaldes", "Racing Club de Lens"), # shared 'club', nothing else
    ("SC Dnipro-1", "1. FC Union Berlin"),            # shared token was '1'
])
def test_cross_border_family_prefixes_are_not_identity(query, wrong):
    assert _lookup(query, wrong) is None


@pytest.mark.parametrize("query,cache,expected", [
    ("Dynamo Moscow", ("Dynamo", "Dinamo Makhachkala"), "Dynamo"),   # via alias now
    ("PFC Levski Sofia", ("Levski Sofia", "CSKA 1948"), "Levski Sofia"),
    ("Union Berlin", ("1. FC Union Berlin",), "1. FC Union Berlin"),
    ("FSV Mainz 05", ("1. FSV Mainz 05",), "1. FSV Mainz 05"),
    ("Bayern Munich", ("FC Bayern München", "VfL Bochum"), "FC Bayern München"),
    ("Aris", ("Aris Thessalonikis",), "Aris Thessalonikis"),
    ("Aris Thessaloniki", ("Aris Thessalonikis",), "Aris Thessalonikis"),
    ("OFI Crete", ("OFI",), "OFI"),
    ("Club Brugge", ("Club Brugge KV", "Cercle Brugge"), "Club Brugge KV"),
    ("Kauno Žalgiris", ("Kauno Žalgiris",), "Kauno Žalgiris"),
    ("Sarpsborg", ("Sarpsborg 08 FF",), "Sarpsborg 08 FF"),
])
def test_family_prefix_trues_still_resolve(query, cache, expected):
    assert _lookup(query, *cache) == expected


@pytest.mark.parametrize("query,cache,expected", [
    # Sharing the generic token is fine when a NON-generic token also matches
    ("Austria Wien", ("Austria Vienna", "Austria Lustenau"), "Austria Vienna"),
    ("Rapid Wien", ("Rapid Vienna", "SK Rapid II"), "Rapid Vienna"),
    ("Clube Atlético Mineiro", ("Atletico-MG", "America Mineiro"), "Atletico-MG"),
    ("America MG", ("America Mineiro", "Atletico-MG"), "America Mineiro"),
    ("Athletico Paranaense-PR", ("Atletico Paranaense",), "Atletico Paranaense"),
    ("Union Santa Fe", ("Union Santa Fe", "Colon"), "Union Santa Fe"),
    ("Universidad de Chile", ("Universidad de Chile",), "Universidad de Chile"),
    ("Bragantino-SP", ("RB Bragantino",), "RB Bragantino"),
    ("Sport Recife", ("Sport Recife",), "Sport Recife"),
    ("Real Sociedad", ("Real Sociedad de Fútbol", "Real Oviedo"),
     "Real Sociedad de Fútbol"),
    # …and when the right club appears, the redirect lands on it
    ("Atlético Huracán", ("Huracan", "Atletico Tucuman"), "Huracan"),
    ("Brighton and Hove Albion", ("Brighton & Hove Albion FC",),
     "Brighton & Hove Albion FC"),
    ("Leuven", ("OH Leuven",), "OH Leuven"),
    ("AGF Aarhus", ("Aarhus",), "Aarhus"),
])
def test_true_resolutions_survive_the_new_guards(query, cache, expected):
    assert _lookup(query, *cache) == expected
