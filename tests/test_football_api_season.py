"""Off-season fallback in FootballDataClient.get_recent_matches.

Between June and August the CURRENT season has 0 finished matches, which starved
the model / backtest / calibration cold-start. get_recent_matches now falls back
to the most recent COMPLETED season when the current one is empty.
"""
from betbot.football_api import FootballDataClient


def test_season_fallback_when_current_empty(monkeypatch):
    c = FootballDataClient("dummy")
    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params or {}))
        # Current season (no season param) empty; any pinned season has data.
        if "season" not in (params or {}):
            return {"matches": []}
        return {"matches": [{"x": 1}, {"x": 2}]}

    monkeypatch.setattr(c, "_get", fake_get)
    m = c.get_recent_matches("PL", limit=300)
    assert len(m) == 2                                  # got the fallback season's data
    assert any("season" in p for p in calls)           # a season-pinned retry happened


def test_no_fallback_when_current_has_data(monkeypatch):
    c = FootballDataClient("dummy")
    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params or {}))
        return {"matches": [{"x": 1}]}                  # current season already has data

    monkeypatch.setattr(c, "_get", fake_get)
    m = c.get_recent_matches("PL")
    assert len(m) == 1
    assert len(calls) == 1                              # no extra fallback call


def test_explicit_season_is_not_overridden(monkeypatch):
    c = FootballDataClient("dummy")
    seen = []

    def fake_get(path, params=None):
        seen.append(dict(params or {}))
        return {"matches": []}                          # even empty, no auto-fallback

    monkeypatch.setattr(c, "_get", fake_get)
    c.get_recent_matches("PL", season=2025)
    assert len(seen) == 1 and seen[0].get("season") == 2025
