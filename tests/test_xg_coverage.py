"""
xG reached 94 of 816 teams (11.5%) while the scan covered 46 leagues.

Nothing was missing from the paid api-football plan: the endpoint was wired
and the quota was almost untouched. Two narrow maps did it — an xG league map
frozen at 8 entries (one of them a key that exists nowhere else in the code),
and an enrichment loop walking the 10-league curated wishlist.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# One resolver over both league maps
# ---------------------------------------------------------------------------

def test_xg_resolves_leagues_from_the_in_season_map_too():
    """These carry the whole track record and had no xG path at all."""
    from betbot.data_sources.api_football import league_id_for

    for key in ("soccer_usa_mls", "soccer_brazil_campeonato",
                "soccer_argentina_primera_division", "soccer_norway_eliteserien"):
        assert league_id_for(key), f"{key} must resolve to an api-football id"


def test_the_big_leagues_still_resolve():
    from betbot.data_sources.api_football import league_id_for

    assert league_id_for("soccer_epl") == 39
    assert league_id_for("soccer_spain_la_liga") == 140


def test_the_dead_championship_key_is_gone():
    """`soccer_england_championship` appeared in the xG map alone; every other
    module spells it `soccer_efl_champ`, so the entry was unreachable."""
    from betbot.data_sources.api_football import SPORT_TO_LEAGUE_ID, league_id_for

    assert "soccer_england_championship" not in SPORT_TO_LEAGUE_ID
    assert league_id_for("soccer_efl_champ") == 40


def test_unmapped_competition_resolves_to_nothing():
    from betbot.data_sources.api_football import league_id_for

    assert league_id_for("soccer_uefa_nations_league") is None


def test_call_counter_tracks_every_request(monkeypatch):
    from betbot.data_sources import api_football as af

    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    before = af.calls_made()
    with patch.object(af.requests, "get") as g:
        g.return_value = MagicMock(status_code=200, json=lambda: {"response": []})
        af._get("fixtures", {"league": 1})
        af._get("teams", {"league": 1})
    assert af.calls_made() == before + 2


# ---------------------------------------------------------------------------
# Enrichment walks the real league set, under a GLOBAL budget
# ---------------------------------------------------------------------------

def _enrich_with(monkeypatch, leagues, xg_cost_per_league=100, budget=250):
    import betbot.enrichment as mod

    monkeypatch.setattr(mod, "_leagues_with_stats", lambda db: list(leagues))
    monkeypatch.setattr(mod.club_elo, "get_all_elo_ratings", lambda: {})
    # The cross-competition inheritance pass reads the real table; it has its
    # own tests in test_quarantine_and_inheritance.py.
    monkeypatch.setattr(mod, "_inherit_xg_across_leagues", lambda: 0)

    seen: list[tuple[str, int]] = []
    spent = {"n": 0}

    def fake_xg(sport_key, year=None, max_calls=None):
        seen.append((sport_key, max_calls))
        spent["n"] += xg_cost_per_league
        return [{"title": "Arsenal", "xg_per_match": 1.6,
                 "xga_per_match": 0.9, "matches": 6}]

    monkeypatch.setattr(mod.xg, "get_league_xg", fake_xg)
    monkeypatch.setattr(mod, "_af", None, raising=False)

    from betbot.data_sources import api_football as af
    monkeypatch.setattr(af, "calls_made", lambda: spent["n"])

    db = MagicMock()
    db.get_all_team_stats_for_league.return_value = [{"team_name": "Arsenal"}]
    return mod.enrich_team_stats(db, xg_call_budget=budget), seen


def test_enrichment_covers_every_league_that_has_stats(monkeypatch):
    """Not the 10-league wishlist — the leagues actually in the database."""
    leagues = [f"soccer_l{i}" for i in range(6)]
    counts, seen = _enrich_with(monkeypatch, leagues,
                                xg_cost_per_league=0, budget=5000)

    assert counts["leagues_seen"] == 6
    assert [k for k, _ in seen] == leagues


def test_budget_is_global_not_per_league(monkeypatch):
    """A 400-call cap repeated 45 times is an 18,000-call cap. The remaining
    budget must SHRINK as leagues consume it."""
    leagues = [f"soccer_l{i}" for i in range(4)]
    counts, seen = _enrich_with(monkeypatch, leagues,
                                xg_cost_per_league=100, budget=250)

    caps = [cap for _, cap in seen]
    assert caps[0] == 250
    assert caps[1] == 150
    assert caps[2] == 50
    assert len(seen) == 3, "the 4th league must be refused, not given a fresh cap"
    assert counts["xg_budget_exhausted"] == 1


def test_elo_still_runs_for_leagues_that_ran_out_of_xg_budget(monkeypatch):
    """Exhausting the xG budget must not skip the league entirely — Elo is
    free and must keep being filled."""
    leagues = [f"soccer_l{i}" for i in range(4)]
    counts, _ = _enrich_with(monkeypatch, leagues,
                             xg_cost_per_league=100, budget=250)

    assert counts["leagues_seen"] == 4
    assert counts["teams_seen"] == 4, "every league's rows are still walked"


# ---------------------------------------------------------------------------
# xG must not be attached to the wrong club
# ---------------------------------------------------------------------------

def test_xg_uses_the_hardened_matcher_not_substrings(monkeypatch):
    """`title in needle or needle in title` is the same rule that handed
    Dundee United's numbers to Dundee — one layer down, and just as silent."""
    import betbot.enrichment as mod

    monkeypatch.setattr(mod, "_leagues_with_stats", lambda db: ["soccer_spl"])
    monkeypatch.setattr(mod.club_elo, "get_all_elo_ratings", lambda: {})
    monkeypatch.setattr(mod, "_inherit_xg_across_leagues", lambda: 0)
    monkeypatch.setattr(mod.xg, "get_league_xg", lambda *a, **k: [
        {"title": "Dundee", "xg_per_match": 0.8, "xga_per_match": 1.9, "matches": 6},
    ])

    db = MagicMock()
    db.get_all_team_stats_for_league.return_value = [{"team_name": "Dundee United"}]
    mod.enrich_team_stats(db, xg_call_budget=1000)

    written = db.update_team_enrichment.call_args.kwargs
    assert written["team_name"] == "Dundee United"
    assert written["xg_for"] is None, "Dundee's xG must not land on Dundee United"


def test_xg_is_attached_when_the_club_really_matches(monkeypatch):
    import betbot.enrichment as mod

    monkeypatch.setattr(mod, "_leagues_with_stats", lambda db: ["soccer_spl"])
    monkeypatch.setattr(mod.club_elo, "get_all_elo_ratings", lambda: {})
    monkeypatch.setattr(mod, "_inherit_xg_across_leagues", lambda: 0)
    monkeypatch.setattr(mod.xg, "get_league_xg", lambda *a, **k: [
        {"title": "Dundee Utd", "xg_per_match": 1.4, "xga_per_match": 1.1,
         "matches": 6},
    ])

    db = MagicMock()
    db.get_all_team_stats_for_league.return_value = [{"team_name": "Dundee United"}]
    mod.enrich_team_stats(db, xg_call_budget=1000)

    assert db.update_team_enrichment.call_args.kwargs["xg_for"] == 1.4


# ---------------------------------------------------------------------------
# The season boundary again — one layer down
# ---------------------------------------------------------------------------

def _fixtures_stub(monkeypatch, by_season):
    """Stub api-football so we can watch which seasons get asked for."""
    from betbot.data_sources import api_football as af

    asked: list[int] = []

    def fake_get(endpoint, params=None):
        params = params or {}
        if endpoint == "fixtures":
            season = params.get("season")
            asked.append(season)
            return {"response": by_season.get(season, [])}
        if endpoint == "fixtures/statistics":
            return {"response": []}
        return {"response": []}

    monkeypatch.setattr(af, "_get", fake_get)
    return af, asked


def _fixture(fid=1, home=10, away=20):
    return {"fixture": {"id": fid},
            "teams": {"home": {"id": home}, "away": {"id": away}}}


def test_xg_falls_back_to_last_season_when_this_one_is_empty(monkeypatch):
    """In August every autumn-spring league has zero finished fixtures, so the
    xG lookup returned None after a single call and the league stayed blind."""
    af, asked = _fixtures_stub(monkeypatch, {2025: [_fixture()]})

    af.get_recent_team_xg(10, 144, 2026, last=6)

    assert asked == [2026, 2025], "must try this season, then last"


def test_xg_does_not_touch_last_season_when_this_one_has_matches(monkeypatch):
    af, asked = _fixtures_stub(monkeypatch, {2026: [_fixture()]})

    af.get_recent_team_xg(10, 71, 2026, last=6)

    assert asked == [2026], "a live season is never second-guessed"


def test_xg_gives_up_cleanly_when_neither_season_has_matches(monkeypatch):
    af, asked = _fixtures_stub(monkeypatch, {})

    assert af.get_recent_team_xg(10, 999, 2026, last=6) is None
    assert asked == [2026, 2025]


# ---------------------------------------------------------------------------
# The per-minute ceiling: a throttled run must not look like missing data
# ---------------------------------------------------------------------------

def test_a_429_is_retried_not_swallowed(monkeypatch):
    """`_get` logged a warning and returned {} — indistinguishable, to every
    caller, from "this league has no xG". A whole enrichment run came home
    empty from league "conmebol" onward with 6,400 daily calls still unused."""
    from betbot.data_sources import api_football as af

    monkeypatch.setenv("API_FOOTBALL_KEY", "k")
    monkeypatch.setattr(af.time, "sleep", lambda s: None)
    responses = [MagicMock(status_code=429),
                 MagicMock(status_code=200, json=lambda: {"response": ["ok"]})]
    with patch.object(af.requests, "get", side_effect=responses):
        out = af._get("fixtures", {})

    assert out == {"response": ["ok"]}, "must recover, not return {}"


def test_persistent_429_reports_an_error_and_stops(monkeypatch):
    from betbot.data_sources import api_football as af

    monkeypatch.setenv("API_FOOTBALL_KEY", "k")
    monkeypatch.setattr(af.time, "sleep", lambda s: None)
    with patch.object(af.requests, "get",
                      return_value=MagicMock(status_code=429)):
        assert af._get("fixtures", {}) == {}


def test_throttle_waits_before_crossing_the_minute_ceiling(monkeypatch):
    """Proactive: a league-wide xG refresh is a burst of hundreds of calls."""
    from betbot.data_sources import api_football as af

    monkeypatch.setenv("API_FOOTBALL_RATE_LIMIT_PER_MIN", "5")
    af._CALL_TIMES.clear()
    slept: list[float] = []
    monkeypatch.setattr(af.time, "sleep", lambda s: slept.append(s))

    for _ in range(6):
        af._throttle()

    assert slept, "the 6th call must wait rather than earn a 429"
    af._CALL_TIMES.clear()


def test_throttle_is_free_when_well_under_the_ceiling(monkeypatch):
    from betbot.data_sources import api_football as af

    monkeypatch.setenv("API_FOOTBALL_RATE_LIMIT_PER_MIN", "100")
    af._CALL_TIMES.clear()
    slept: list[float] = []
    monkeypatch.setattr(af.time, "sleep", lambda s: slept.append(s))

    for _ in range(10):
        af._throttle()

    assert not slept
    af._CALL_TIMES.clear()
