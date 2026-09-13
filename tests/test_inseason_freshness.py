"""
The in-season refresh must not re-pay on every worker restart.

It costs roughly 34 leagues x 2 seasons x pagination of api-football calls and
fired unconditionally at boot. On 2026-08-18 a day of deployments restarted the
worker about ten times and drained the 7,500/day allowance; after that every
league refreshed to "0 leagues, 0 teams" and the API answered every endpoint
with "You have reached the request limit for the day" — while its own headers
still advertised 7,499 remaining.

Same shape as the Odds API drain fixed earlier in the same codebase: a
catch-up job that re-pays on every restart.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from betbot.stats_inseason import refresh_inseason_stats


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")


def _no_fetch(monkeypatch):
    """Any api-football call in these tests is a failure by definition."""
    import betbot.stats_inseason as mod

    def boom(*a, **k):
        raise AssertionError("no api-football call may happen")

    monkeypatch.setattr(mod.api_football, "get_finished_matches", boom)


def test_the_gate_is_opt_in():
    """The scheduled daily job passes nothing and must always refresh."""
    params = inspect.signature(refresh_inseason_stats).parameters
    assert "max_age_hours" in params
    assert params["max_age_hours"].default is None


def test_fresh_data_skips_the_refresh_entirely(monkeypatch):
    import betbot.stats_inseason as mod

    _no_fetch(monkeypatch)
    monkeypatch.setattr(mod, "_hours_since_last_refresh", lambda: 2.0)

    out = refresh_inseason_stats(MagicMock(), max_age_hours=12)

    assert out["skipped"] == "fresh"
    assert out["age_hours"] == 2.0
    assert out["leagues_done"] == 0


def test_stale_data_refreshes(monkeypatch):
    import betbot.stats_inseason as mod

    monkeypatch.setattr(mod, "_hours_since_last_refresh", lambda: 30.0)
    called = {"n": 0}

    def fake(league_id, season, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(mod.api_football, "get_finished_matches", fake)
    refresh_inseason_stats(MagicMock(), max_age_hours=12)

    assert called["n"] > 0, "stale data must be refreshed"


def test_never_refreshed_before_still_refreshes(monkeypatch):
    """A fresh install has no rows at all — the gate must not lock it out."""
    import betbot.stats_inseason as mod

    monkeypatch.setattr(mod, "_hours_since_last_refresh", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(mod.api_football, "get_finished_matches",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])

    refresh_inseason_stats(MagicMock(), max_age_hours=12)

    assert called["n"] > 0


def test_the_daily_job_ignores_freshness(monkeypatch):
    """Summer leagues play midweek: the scheduled run must refresh even if a
    boot happened an hour ago."""
    import betbot.stats_inseason as mod

    monkeypatch.setattr(mod, "_hours_since_last_refresh", lambda: 0.5)
    called = {"n": 0}
    monkeypatch.setattr(mod.api_football, "get_finished_matches",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])

    refresh_inseason_stats(MagicMock())      # no ceiling passed

    assert called["n"] > 0


def test_boot_asks_for_the_gate():
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    assert "INSEASON_BOOT_MAX_AGE_H" in src
    assert "refresh_inseason_stats(\n            db, max_age_hours=" in src


def test_freshness_only_counts_rows_this_pipeline_wrote():
    """A football-data refresh of the European leagues must not make the
    in-season data look fresher than it is."""
    from pathlib import Path

    src = Path("betbot/stats_inseason.py").read_text(encoding="utf-8")
    fn = src[src.index("def _hours_since_last_refresh"):]
    fn = fn[:fn.index("\ndef ", 5)]
    assert 'like("AF-%")' in fn
