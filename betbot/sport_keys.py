"""
Canonical sport keys — bridges The Odds API's naming to the internal one.

The Odds API emits `soccer_france_ligue_one`. Every other file in this codebase
writes `soccer_france_ligue1`: `team_stats` rows, `LEAGUE_MAP`, the Dixon-Coles
rho table, the api-football and football-data.co.uk mappings, the tuner, the
calibrator segments. The two never met, so Ligue 1 — 18 teams with full stats
and the best xG coverage in the database — was invisible end to end: every
fixture fell through to the consensus model as if it were an exotic league.

Renaming the eleven internal sites would have been the other option, but the
internal name is the one written into `predictions.sport_key` for 310 rows and
into `team_stats`, so translating at the boundary is both smaller and
reversible.

The rule is one-way in each direction and applied in exactly one place, the
Odds API client:

    incoming events / scores  ->  to_canonical()   (what the rest of the code sees)
    outgoing HTTP requests    ->  to_api()         (what the provider expects)

Add a pair here whenever a provider renames a competition. `tests/` asserts the
mapping is a bijection so a typo cannot silently create a third spelling.
"""
from __future__ import annotations

# provider key -> internal canonical key
_API_TO_CANONICAL: dict[str, str] = {
    "soccer_france_ligue_one": "soccer_france_ligue1",
}

_CANONICAL_TO_API: dict[str, str] = {v: k for k, v in _API_TO_CANONICAL.items()}


def to_canonical(sport_key: str) -> str:
    """Provider key -> the spelling the rest of the codebase uses."""
    return _API_TO_CANONICAL.get(sport_key, sport_key)


def to_api(sport_key: str) -> str:
    """Internal key -> the spelling The Odds API expects in a request."""
    return _CANONICAL_TO_API.get(sport_key, sport_key)


def known_aliases() -> dict[str, str]:
    """Provider -> canonical, for diagnostics and tests."""
    return dict(_API_TO_CANONICAL)

# ---------------------------------------------------------------------------
# Cup competitions -> the domestic leagues their entrants come from
# ---------------------------------------------------------------------------
#
# A cup has no squad of its own: Arsenal plays the FA Cup, but its attack and
# defense coefficients live under `soccer_epl`. Keyed lookups therefore found
# nothing for every cup tie and silently degraded to the market consensus,
# even though the stats were already in the database.
#
# An explicit map — rather than a global "search every league" index — because
# team names collide across countries (River Plate, Nacional, Arsenal de
# Sarandí). Borrowing a Argentine club's form for an English cup tie would be
# worse than having no stats at all, and it would be invisible.
CUP_PARENT_LEAGUES: dict[str, tuple[str, ...]] = {
    "soccer_fa_cup": (
        "soccer_epl", "soccer_efl_champ",
        "soccer_england_league1", "soccer_england_league2",
    ),
    "soccer_england_efl_cup": (
        "soccer_epl", "soccer_efl_champ",
        "soccer_england_league1", "soccer_england_league2",
    ),
    "soccer_germany_dfb_pokal": (
        "soccer_germany_bundesliga", "soccer_germany_bundesliga2",
        "soccer_germany_liga3",
    ),
    "soccer_concacaf_leagues_cup": ("soccer_usa_mls", "soccer_mexico_ligamx"),
}


def parent_leagues(sport_key: str) -> tuple[str, ...]:
    """Domestic leagues whose team stats apply to this competition.

    Empty for ordinary leagues. Note `soccer_uefa_nations_league` is
    deliberately absent: it is played by NATIONAL teams, for which no club
    stats exist anywhere in the database. Pretending otherwise would be a
    silent lie — it stays on the consensus model.
    """
    return CUP_PARENT_LEAGUES.get(to_canonical(sport_key), ())


# ---------------------------------------------------------------------------
# Continental competitions — entrants come from many domestic leagues
# ---------------------------------------------------------------------------
#
# A national cup draws from a KNOWN, short list of tiers, which is why
# CUP_PARENT_LEAGUES names them explicitly. A continental competition cannot:
# the Champions League qualifying rounds alone pull clubs from thirty
# countries, and the field changes every round.
#
# Measured 2026-08-10 on a live scan: of 12 teams playing in the day's four
# continental fixtures, 5 had no stats under the competition key — while
# Olympique Lyonnais, AEK Athens FC, Viking and Boca Juniors all sat in the
# table under their domestic league. The bot was pricing European nights with
# the consensus model on data it already owned.
CONTINENTAL_COMPETITIONS: frozenset = frozenset([
    "soccer_uefa_champs_league",
    "soccer_uefa_champs_league_qualification",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_copa_sudamericana",
    "soccer_uefa_super_cup",
])


def is_continental(sport_key: str) -> bool:
    """True when a competition's entrants come from other leagues entirely."""
    return to_canonical(sport_key) in CONTINENTAL_COMPETITIONS


# ---------------------------------------------------------------------------
# Confederations — the borrowing frontier
# ---------------------------------------------------------------------------
#
# Measured 2026-09-08 on the live Champions League pool: borrowing indexed 781
# teams from every competition in the table, and the fuzzy matcher then priced
# "Barcelona" with Barcelona SC (Ecuador) and "Sporting Lisbon" with Sporting
# Gijon. A UEFA fixture can only field clubs from UEFA leagues; lending it a
# CONMEBOL or MLS row is never a repair, only bait for the matcher. Stats may
# cross a border, never an ocean.
_CONFED_BY_COUNTRY: dict[str, str] = {
    "argentina": "conmebol", "bolivia": "conmebol", "brazil": "conmebol",
    "chile": "conmebol", "colombia": "conmebol", "ecuador": "conmebol",
    "paraguay": "conmebol", "peru": "conmebol", "uruguay": "conmebol",
    "venezuela": "conmebol",
    "usa": "concacaf", "mexico": "concacaf",
    "australia": "afc", "china": "afc", "japan": "afc", "korea": "afc",
    "qatar": "afc", "saudi": "afc",
    "africa": "caf", "egypt": "caf",
}


def confederation_of(sport_key: str) -> str:
    """Confederation a competition belongs to ('uefa', 'conmebol', ...).

    Prefix first (soccer_uefa_*, soccer_conmebol_*, soccer_concacaf_*), then
    the country token. The default is 'uefa' because every scanned league
    without an entry above is European (epl, spl, efl_*, fa_cup,
    league_of_ireland...); a newly scanned non-European league must be added
    to _CONFED_BY_COUNTRY or it will be lent to European nights.
    """
    key = to_canonical(sport_key)
    parts = key.split("_")
    if len(parts) >= 2:
        if parts[1] in ("uefa", "conmebol", "concacaf"):
            return parts[1]
        confed = _CONFED_BY_COUNTRY.get(parts[1])
        if confed:
            return confed
    return "uefa"
