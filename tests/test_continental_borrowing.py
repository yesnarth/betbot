"""
Continental competitions borrow their entrants' stats from domestic leagues.

A national cup draws from a known, short list of tiers — hence the explicit
CUP_PARENT_LEAGUES map. A continental competition cannot: Champions League
qualifying alone pulls clubs from thirty countries and the field turns over
every round.

Measured on a live scan 2026-08-10: of 12 teams in the day's four continental
fixtures, 5 had no stats under the competition key, while Olympique Lyonnais,
AEK Athens FC, Viking and Boca Juniors all sat in the table under their
domestic league. Those matches were priced by the consensus model on data the
bot already owned.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from betbot.analysis import _fuzzy_lookup, _invalidate_norm_cache
from betbot.sport_keys import is_continental


def _row(name, sport_key, matches=30, attack=1.2):
    return {"team_name": name, "sport_key": sport_key,
            "attack_home": attack, "defense_home": 1.0,
            "attack_away": 1.0, "defense_away": 1.0,
            "matches_analyzed": matches, "elo_rating": None,
            "xg_for": None, "xg_against": None}


def _db(by_league):
    db = MagicMock()
    db.get_all_team_stats_for_league.side_effect = lambda k: by_league.get(k, [])
    db.get_league_averages.side_effect = lambda k: None
    db.get_all_h2h_for_league.side_effect = lambda k: {}
    return db


def _load(monkeypatch, by_league, target):
    import betbot.shared as mod

    pool = {}
    for rows in by_league.values():
        for r in rows:
            pool.setdefault(r["team_name"], []).append(r)
    monkeypatch.setattr(mod, "_build_global_team_pool", lambda db: pool)
    return mod.load_team_stats_from_db(_db(by_league), [target])


def _lookup(query, *stored):
    cache = {n: f"stats-{n}" for n in stored}
    _invalidate_norm_cache(cache)
    try:
        return _fuzzy_lookup(query, cache)[1]
    finally:
        _invalidate_norm_cache(cache)


# ---------------------------------------------------------------------------
# Which competitions borrow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("soccer_uefa_champs_league_qualification", True),
    ("soccer_conmebol_copa_libertadores", True),
    ("soccer_conmebol_copa_sudamericana", True),
    ("soccer_epl", False),
    ("soccer_fa_cup", False),          # a national cup has named parents
    ("soccer_brazil_serie_b", False),
])
def test_only_continental_competitions_borrow(key, expected):
    assert is_continental(key) is expected


# ---------------------------------------------------------------------------
# Borrowing works
# ---------------------------------------------------------------------------

def test_a_qualifier_borrows_a_domestic_club(monkeypatch):
    by_league = {
        "soccer_uefa_champs_league_qualification": [],
        "soccer_norway_eliteserien": [_row("Viking", "soccer_norway_eliteserien")],
    }
    out = _load(monkeypatch, by_league, "soccer_uefa_champs_league_qualification")

    assert "Viking" in out["soccer_uefa_champs_league_qualification"]["teams"]


def test_the_competitions_own_row_always_wins(monkeypatch):
    """Stats built from the competition's own fixtures describe that
    competition. A borrowed row never displaces them."""
    by_league = {
        "soccer_conmebol_copa_libertadores": [
            _row("Boca Juniors", "soccer_conmebol_copa_libertadores",
                 matches=6, attack=1.9)],
        "soccer_argentina_primera_division": [
            _row("Boca Juniors", "soccer_argentina_primera_division",
                 matches=21, attack=1.1)],
    }
    out = _load(monkeypatch, by_league, "soccer_conmebol_copa_libertadores")

    boca = out["soccer_conmebol_copa_libertadores"]["teams"]["Boca Juniors"]
    assert boca.matches_analyzed == 6


# ---------------------------------------------------------------------------
# The guard that makes this safe
# ---------------------------------------------------------------------------

def test_an_ambiguous_name_is_never_borrowed(monkeypatch):
    """River Plate exists in Argentina AND Uruguay — same confederation, so
    the ocean filter cannot arbitrate. Lending one club's form to the other
    is the Dundee bug with a passport — and just as invisible."""
    by_league = {
        "soccer_conmebol_copa_libertadores": [],
        "soccer_argentina_primera_division": [
            _row("River Plate", "soccer_argentina_primera_division")],
        "soccer_uruguay_primera": [_row("River Plate", "soccer_uruguay_primera")],
    }
    out = _load(monkeypatch, by_league, "soccer_conmebol_copa_libertadores")

    teams = out.get("soccer_conmebol_copa_libertadores", {}).get("teams", {})
    assert "River Plate" not in teams


def test_a_domestic_league_never_borrows(monkeypatch):
    """Outside continental competitions a missing club is a data gap, and
    reaching into another country to fill it would invent a match."""
    by_league = {
        "soccer_epl": [_row("Arsenal", "soccer_epl")],
        "soccer_argentina_primera_division": [
            _row("Arsenal de Sarandi", "soccer_argentina_primera_division")],
    }
    out = _load(monkeypatch, by_league, "soccer_epl")

    assert set(out["soccer_epl"]["teams"]) == {"Arsenal"}


def test_viking_and_vikingur_stay_distinct(monkeypatch):
    """Both sit in the production table. Borrowing must not merge them."""
    by_league = {
        "soccer_uefa_champs_league_qualification": [
            _row("Vikingur Reykjavik", "soccer_uefa_champs_league_qualification")],
        "soccer_norway_eliteserien": [_row("Viking", "soccer_norway_eliteserien")],
    }
    out = _load(monkeypatch, by_league, "soccer_uefa_champs_league_qualification")

    teams = out["soccer_uefa_champs_league_qualification"]["teams"]
    assert {"Viking", "Vikingur Reykjavik"} <= set(teams)


def test_the_borrowed_pool_does_not_confuse_the_matcher():
    """With both spellings present, each query must still land on its own
    club — the whole point of the hardened matcher."""
    assert _lookup("Viking FK", "Viking", "Vikingur Reykjavik") == "Viking"
    assert _lookup("Vikingur Reykjavik", "Viking",
                   "Vikingur Reykjavik") == "Vikingur Reykjavik"


# ---------------------------------------------------------------------------
# Naming bridges the matcher cannot infer on its own
# ---------------------------------------------------------------------------

def test_lyon_resolves_to_its_official_name():
    """Lyon and Olympique Lyonnais share no token and score low on string
    similarity — only an explicit alias bridges it."""
    assert _lookup("Lyon", "Olympique Lyonnais") == "Olympique Lyonnais"


@pytest.mark.parametrize("query,stored", [
    ("AEK Athens", "AEK Athens FC"),
    ("Viking FK", "Viking"),
    ("Boca Juniors", "Boca Juniors"),
])
def test_suffix_variants_need_no_alias(query, stored):
    """Seen live: these missed only because the club sat in another
    competition, not because the names were unbridgeable."""
    assert _lookup(query, stored) == stored


# ---------------------------------------------------------------------------
# A club playing several competitions is still ONE club
# ---------------------------------------------------------------------------

def test_a_club_with_a_domestic_and_a_continental_row_is_resolved(monkeypatch):
    """Boca Juniors sits under Argentina (21 matches) and the Libertadores
    (6). Those are the same club, so declining would waste real data — the
    deep domestic row is the right one."""
    by_league = {
        "soccer_conmebol_copa_sudamericana": [],
        "soccer_argentina_primera_division": [
            _row("Boca Juniors", "soccer_argentina_primera_division", matches=21)],
        "soccer_conmebol_copa_libertadores": [
            _row("Boca Juniors", "soccer_conmebol_copa_libertadores", matches=6)],
    }
    out = _load(monkeypatch, by_league, "soccer_conmebol_copa_sudamericana")

    boca = out["soccer_conmebol_copa_sudamericana"]["teams"]["Boca Juniors"]
    assert boca.matches_analyzed == 21, "the domestic season, not the thin cup row"


def test_the_ocean_filter_resolves_a_cross_confederation_namesake(monkeypatch):
    """A Portuguese Nacional cannot play the Libertadores, so its row is not
    even a candidate — the Uruguayan club's season is safe to lend where the
    old rule declined and priced the tie by consensus."""
    by_league = {
        "soccer_conmebol_copa_libertadores": [],
        "soccer_portugal_primeira_liga": [
            _row("Nacional", "soccer_portugal_primeira_liga", matches=34)],
        "soccer_uruguay_primera": [
            _row("Nacional", "soccer_uruguay_primera", matches=30)],
    }
    out = _load(monkeypatch, by_league, "soccer_conmebol_copa_libertadores")

    nacional = out["soccer_conmebol_copa_libertadores"]["teams"]["Nacional"]
    assert nacional.matches_analyzed == 30, "the Uruguayan row, not the Portuguese"


def test_only_continental_rows_available_still_lends_when_unique(monkeypatch):
    """A club known only from another continental competition is still that
    club, as long as there is exactly one of it."""
    by_league = {
        "soccer_conmebol_copa_sudamericana": [],
        "soccer_conmebol_copa_libertadores": [
            _row("Some CF", "soccer_conmebol_copa_libertadores", matches=6)],
    }
    out = _load(monkeypatch, by_league, "soccer_conmebol_copa_sudamericana")

    assert "Some CF" in out["soccer_conmebol_copa_sudamericana"]["teams"]


# ---------------------------------------------------------------------------
# Stats may cross a border, never an ocean
# ---------------------------------------------------------------------------
# Measured 2026-09-08 on the live Champions League pool: 781 borrowed teams
# from every competition in the table, including Barcelona SC (Ecuador),
# Sporting Kansas City and Atletico-MG — pure bait for the fuzzy matcher on
# European nights. A UEFA fixture only fields clubs from UEFA leagues.

@pytest.mark.parametrize("key,confed", [
    ("soccer_uefa_champs_league", "uefa"),
    ("soccer_epl", "uefa"),
    ("soccer_spl", "uefa"),
    ("soccer_league_of_ireland", "uefa"),
    ("soccer_france_ligue_one", "uefa"),   # provider spelling, via to_canonical
    ("soccer_conmebol_copa_libertadores", "conmebol"),
    ("soccer_brazil_campeonato", "conmebol"),
    ("soccer_argentina_primera_division", "conmebol"),
    ("soccer_usa_mls", "concacaf"),
    ("soccer_mexico_ligamx", "concacaf"),
    ("soccer_japan_j_league", "afc"),
    ("soccer_saudi_arabia_pro_league", "afc"),
    ("soccer_korea_kleague1", "afc"),
    ("soccer_africa_cup_of_nations", "caf"),
])
def test_confederation_of(key, confed):
    from betbot.sport_keys import confederation_of
    assert confederation_of(key) == confed


def test_uefa_pool_refuses_an_ecuadorian_namesake(monkeypatch):
    """Barcelona SC's only row sits under the Libertadores; lending it to the
    Champions League put the Ecuadorian club one fuzzy step away from every
    'Barcelona' query."""
    by_league = {
        "soccer_conmebol_copa_libertadores": [
            _row("Barcelona SC", "soccer_conmebol_copa_libertadores", matches=9)],
    }
    out = _load(monkeypatch, by_league, "soccer_uefa_champs_league")

    teams = out.get("soccer_uefa_champs_league", {}).get("teams", {})
    assert "Barcelona SC" not in teams


def test_uefa_pool_refuses_other_confederations_but_keeps_its_own(monkeypatch):
    by_league = {
        "soccer_usa_mls": [_row("Sporting Kansas City", "soccer_usa_mls", matches=23)],
        "soccer_spain_segunda_division": [
            _row("Sporting Gijon", "soccer_spain_segunda_division", matches=4)],
    }
    out = _load(monkeypatch, by_league, "soccer_uefa_champs_league")

    teams = out["soccer_uefa_champs_league"]["teams"]
    assert "Sporting Kansas City" not in teams
    assert "Sporting Gijon" in teams       # same confederation still lends


def test_same_confederation_continental_row_still_lends(monkeypatch):
    """FK Crvena Zvezda's only stats come from the CL qualifying rounds —
    same confederation, same club, and the only sample it has."""
    by_league = {
        "soccer_uefa_champs_league_qualification": [
            _row("FK Crvena Zvezda",
                 "soccer_uefa_champs_league_qualification", matches=4)],
    }
    out = _load(monkeypatch, by_league, "soccer_uefa_champs_league")

    assert "FK Crvena Zvezda" in out["soccer_uefa_champs_league"]["teams"]
