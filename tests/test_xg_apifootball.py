"""api-football xG (per-fixture, aggregated to team form) + xG source facade.

api-football's live behaviour needs a paid key, so these mock the single HTTP
chokepoint (api_football._get) to validate the parsing/aggregation logic and the
facade's backend selection.
"""
import betbot.data_sources.api_football as af
from betbot.data_sources import xg


def test_get_fixture_xg_parses_expected_goals(monkeypatch):
    monkeypatch.setattr(af, "_get", lambda ep, params=None: {"response": [
        {"team": {"id": 33}, "statistics": [
            {"type": "Total Shots", "value": 12},
            {"type": "expected_goals", "value": "1.5"}]},
        {"team": {"id": 40}, "statistics": [
            {"type": "expected_goals", "value": "0.8"}]},
    ]})
    assert af.get_fixture_xg(999) == {33: 1.5, 40: 0.8}


def test_get_fixture_xg_handles_leagues_without_xg(monkeypatch):
    monkeypatch.setattr(af, "_get", lambda ep, params=None: {"response": [
        {"team": {"id": 33}, "statistics": [{"type": "Total Shots", "value": 5}]},
    ]})
    assert af.get_fixture_xg(1) == {}


def test_get_recent_team_xg_aggregates_for_and_against(monkeypatch):
    def fake_get(ep, params=None):
        if ep == "fixtures":
            return {"response": [
                {"fixture": {"id": 1}, "teams": {"home": {"id": 33}, "away": {"id": 40}}},
                {"fixture": {"id": 2}, "teams": {"home": {"id": 50}, "away": {"id": 33}}},
            ]}
        if ep == "fixtures/statistics":
            if params["fixture"] == 1:
                return {"response": [
                    {"team": {"id": 33}, "statistics": [{"type": "expected_goals", "value": "2.0"}]},
                    {"team": {"id": 40}, "statistics": [{"type": "expected_goals", "value": "0.5"}]}]}
            return {"response": [
                {"team": {"id": 33}, "statistics": [{"type": "expected_goals", "value": "1.0"}]},
                {"team": {"id": 50}, "statistics": [{"type": "expected_goals", "value": "1.5"}]}]}
        return {}
    monkeypatch.setattr(af, "_get", fake_get)
    agg = af.get_recent_team_xg(33, 39, 2025, last=2)
    assert agg["matches"] == 2
    assert agg["xg_per_match"] == 1.5    # (2.0 + 1.0) / 2 — team 33's own xG
    assert agg["xga_per_match"] == 1.0   # (0.5 + 1.5) / 2 — opponents' xG


def test_get_league_xg_builds_team_list(monkeypatch):
    af._XG_LEAGUE_CACHE.clear()

    def fake_get(ep, params=None):
        if ep == "teams":
            return {"response": [{"team": {"id": 33, "name": "Arsenal"}}]}
        if ep == "fixtures":
            return {"response": [{"fixture": {"id": 1},
                                  "teams": {"home": {"id": 33}, "away": {"id": 40}}}]}
        if ep == "fixtures/statistics":
            return {"response": [
                {"team": {"id": 33}, "statistics": [{"type": "expected_goals", "value": "1.8"}]},
                {"team": {"id": 40}, "statistics": [{"type": "expected_goals", "value": "0.9"}]}]}
        return {}
    monkeypatch.setattr(af, "_get", fake_get)
    res = af.get_league_xg("soccer_epl", year=2025, last=1)
    assert res == [{"title": "Arsenal", "matches": 1,
                    "xg_per_match": 1.8, "xga_per_match": 0.9}]


def test_get_league_xg_unmapped_returns_empty():
    assert af.get_league_xg("basketball_nba") == []


def test_facade_prefers_apifootball_when_keyed(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "x")
    monkeypatch.setattr(af, "get_league_xg",
                        lambda sk, yr=None: [{"title": "Arsenal", "xg_per_match": 1.8, "xga_per_match": 0.9}])
    res = xg.get_league_xg("soccer_epl")
    assert res and res[0]["title"] == "Arsenal"


def test_facade_falls_back_to_understat_without_key(monkeypatch):
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    from betbot.data_sources import understat
    monkeypatch.setattr(understat, "get_league_xg",
                        lambda sk, year=None: [{"title": "FromUnderstat", "xg_per_match": 1.0, "xga_per_match": 1.0}])
    res = xg.get_league_xg("soccer_epl")
    assert res[0]["title"] == "FromUnderstat"
