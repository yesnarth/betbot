"""
Stop paying to discover who is playing.

The Odds API bills `/odds` per region x market and serves `/events` for free —
verified against the live `x-requests-last` header, which reads 0. The bot
never used it, so every scan bought fixtures just to learn which leagues had a
match at all.

Measured on production, 2026-08-19 at 15:52 Paris: 45 in-season leagues,
507 fixtures purchased, 6 usable. `get_active_sports` answers "in season",
which is not the same question as "playing in the next few hours".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import betbot.shared as shared_mod
from betbot.api import OddsAPIClient

# The today-window is UTC-day-bounded BY DESIGN, so "kickoff in 3 hours" stops
# being "today" near midnight — which made these tests fail only when the suite
# ran late in the UTC evening. Freeze the clock at midday instead of depending
# on when CI happens to run.
_FROZEN_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW if tz else _FROZEN_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(shared_mod, "datetime", _FrozenDatetime)


def _iso(minutes_from_now: int) -> str:
    return (_FROZEN_NOW
            + timedelta(minutes=minutes_from_now)).isoformat().replace("+00:00", "Z")


def _event(minutes_from_now: int) -> dict:
    return {"id": f"e{minutes_from_now}", "commence_time": _iso(minutes_from_now),
            "home_team": "A", "away_team": "B"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MIN_BEFORE_KICKOFF", "60")
    monkeypatch.delenv("ODDS_API_KEYS", raising=False)
    return OddsAPIClient("key")


# ---------------------------------------------------------------------------
# Which leagues survive the free listing
# ---------------------------------------------------------------------------

def test_a_league_playing_later_today_is_kept(client):
    with patch.object(OddsAPIClient, "get_events", return_value=[_event(180)]):
        assert client.leagues_with_upcoming(["soccer_epl"]) == ["soccer_epl"]


def test_a_league_with_nothing_today_is_dropped(client):
    """Two days out is in season, and worth zero credits today."""
    with patch.object(OddsAPIClient, "get_events", return_value=[_event(60 * 48)]):
        assert client.leagues_with_upcoming(["soccer_epl"]) == []


def test_a_match_too_close_to_kickoff_does_not_justify_the_spend(client):
    """Inside MIN_BEFORE_KICKOFF the scan would discard it anyway."""
    with patch.object(OddsAPIClient, "get_events", return_value=[_event(15)]):
        assert client.leagues_with_upcoming(["soccer_epl"]) == []


def test_an_empty_league_is_dropped(client):
    with patch.object(OddsAPIClient, "get_events", return_value=[]):
        assert client.leagues_with_upcoming(["soccer_epl"]) == []


# ---------------------------------------------------------------------------
# Failure must never shrink the scan silently
# ---------------------------------------------------------------------------

def test_a_failed_listing_keeps_the_league(client):
    """A network hiccup must not quietly drop a league. That is exactly the
    stable selection bias the pre-flight quota gate exists to prevent,
    arriving through another door."""
    with patch.object(OddsAPIClient, "get_events", side_effect=OSError("boom")):
        assert client.leagues_with_upcoming(["soccer_epl"]) == ["soccer_epl"]


def test_one_failure_does_not_poison_the_others(client):
    def flaky(sport):
        if sport == "soccer_epl":
            raise OSError("boom")
        return [_event(180)] if sport == "soccer_spain_la_liga" else []

    with patch.object(OddsAPIClient, "get_events", side_effect=flaky):
        kept = client.leagues_with_upcoming(
            ["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a"])

    assert kept == ["soccer_epl", "soccer_spain_la_liga"]


# ---------------------------------------------------------------------------
# The saving, end to end
# ---------------------------------------------------------------------------

def _wire(monkeypatch, active, upcoming, remaining=20000):
    monkeypatch.setenv("SCAN_ALL_SOCCER", "1")
    monkeypatch.setenv("ODDS_REGIONS", "eu")
    monkeypatch.setenv("PREFILTER_UPCOMING", "1")
    monkeypatch.delenv("ODDS_API_KEYS", raising=False)
    client = OddsAPIClient("key")
    return client, patch.multiple(
        OddsAPIClient,
        get_active_sports=lambda self: set(active),
        probe_quota=lambda self: remaining,
        leagues_with_upcoming=lambda self, sports, min_before_kickoff=60: list(upcoming),
    )


def test_only_the_leagues_that_play_are_billed(monkeypatch):
    active = {f"soccer_l{i}" for i in range(45)}
    client, ctx = _wire(monkeypatch, active, ["soccer_l3", "soccer_l7"])
    billed: list[str] = []

    def _spy(self, sport, markets=None):
        billed.append(sport)
        return []

    with ctx, patch.object(OddsAPIClient, "get_events_with_odds", _spy):
        client.fetch_all_sports()

    assert sorted(billed) == ["soccer_l3", "soccer_l7"]


def test_a_quiet_slot_costs_nothing_at_all(monkeypatch):
    """The 21:30 slot on a quiet Tuesday used to buy 45 leagues to find
    nothing."""
    client, ctx = _wire(monkeypatch, {f"soccer_l{i}" for i in range(45)}, [])

    with ctx, patch.object(
        OddsAPIClient, "get_events_with_odds",
        side_effect=AssertionError("no billed call may happen"),
    ):
        assert client.fetch_all_sports() == {}


def test_the_prefilter_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PREFILTER_UPCOMING", "0")
    active = {"soccer_l1", "soccer_l2"}
    client, ctx = _wire(monkeypatch, active, [])
    monkeypatch.setenv("PREFILTER_UPCOMING", "0")
    billed: list[str] = []

    with ctx, patch.object(
        OddsAPIClient, "get_events_with_odds",
        lambda self, sport, markets=None: billed.append(sport) or [],
    ):
        client.fetch_all_sports()

    assert sorted(billed) == ["soccer_l1", "soccer_l2"], (
        "switched off, every in-season league is billed as before")
