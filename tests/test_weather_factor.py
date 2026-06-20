"""Weather goal-expectancy modifier wired into the model (D3).

get_weather_factor returns a multiplicative λ modifier (≤1.0 on heavy rain /
strong wind), or 1.0 (no effect) for non-football, when disabled, unknown
stadium, or budget exhausted. Only ~33 top clubs trigger an HTTP call.
"""
import betbot.data_sources.weather as W


def setup_function():
    W.reset_weather_budget()
    W._weather_cache.clear()


def _boom(*a, **k):
    raise AssertionError("get_match_weather should not be called")


def test_non_football_is_noop(monkeypatch):
    monkeypatch.setenv("FETCH_WEATHER", "1")
    monkeypatch.setattr(W, "get_match_weather", _boom)
    assert W.get_weather_factor("Arsenal", "2026-05-06T19:00:00Z", "basketball_nba") == 1.0
    assert W.get_weather_factor("Arsenal", "2026-05-06T19:00:00Z", "tennis_atp") == 1.0


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("FETCH_WEATHER", "0")
    monkeypatch.setattr(W, "get_match_weather", _boom)
    assert W.get_weather_factor("Arsenal", "2026-05-06T19:00:00Z", "soccer_epl") == 1.0


def test_unknown_stadium_is_noop(monkeypatch):
    monkeypatch.setenv("FETCH_WEATHER", "1")
    monkeypatch.setattr(W, "get_match_weather", _boom)   # must not fetch
    assert W.get_weather_factor("Tiny Unknown FC", "2026-05-06T19:00:00Z", "soccer_epl") == 1.0


def test_known_stadium_applies_modifier(monkeypatch):
    monkeypatch.setenv("FETCH_WEATHER", "1")
    monkeypatch.setattr(W, "get_match_weather",
                        lambda home, ko: {"expected_goal_modifier": 0.88})
    assert W.get_weather_factor("Arsenal", "2026-05-06T19:00:00Z", "soccer_epl") == 0.88


def test_missing_kickoff_is_noop(monkeypatch):
    monkeypatch.setenv("FETCH_WEATHER", "1")
    monkeypatch.setattr(W, "get_match_weather", _boom)
    assert W.get_weather_factor("Arsenal", None, "soccer_epl") == 1.0


def test_budget_exhausted_is_noop(monkeypatch):
    monkeypatch.setenv("FETCH_WEATHER", "1")
    monkeypatch.setattr(W, "get_match_weather",
                        lambda home, ko: {"expected_goal_modifier": 0.88})
    W.reset_weather_budget(0)        # no lookups allowed this scan
    assert W.get_weather_factor("Arsenal", "2026-05-06T19:00:00Z", "soccer_epl") == 1.0
