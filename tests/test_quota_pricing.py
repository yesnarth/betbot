"""
Quota is shown to the user in SCANS, not credits.

The user rotates Odds API keys by hand, off the number the dashboard and the
daily email show them. With `SCAN_ALL_SOCCER=1` a scan reaches ~46 leagues,
and The Odds API bills the cross product `regions x markets` — so "eu,uk"
with h2h+totals costs 4 credits per league, i.e. 184 per scan. A raw "150
credits left" reads as healthy while already buying zero scans.
"""
from __future__ import annotations

import pytest

from betbot.api import ODDS_MARKETS, per_league_cost


# ---------------------------------------------------------------------------
# The price of one league
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("regions,expected", [
    ("eu", 2),          # 1 region x 2 markets
    ("eu,uk", 4),       # 2 regions x 2 markets — the production setting
    ("eu,uk,us", 6),
])
def test_per_league_cost_is_regions_times_markets(monkeypatch, regions, expected):
    monkeypatch.setenv("ODDS_REGIONS", regions)
    assert per_league_cost() == expected


def test_markets_constant_is_the_one_actually_requested():
    """The cost estimate used to hardcode "h2h,totals" independently of the
    fetch default. Two copies of a price drift apart silently."""
    import inspect

    from betbot.api import OddsAPIClient

    default = inspect.signature(OddsAPIClient.get_events_with_odds).parameters["markets"].default
    assert default == ODDS_MARKETS


def test_empty_regions_never_prices_a_scan_at_zero(monkeypatch):
    """A malformed ODDS_REGIONS must not make every scan look free — that
    would turn the pre-flight gate into a no-op."""
    monkeypatch.setenv("ODDS_REGIONS", ",,")
    assert per_league_cost() >= 1


def test_full_soccer_scan_price_matches_the_gate(monkeypatch):
    """46 in-season leagues in production. Pins the number the user plans
    key rotation around."""
    monkeypatch.setenv("ODDS_REGIONS", "eu,uk")
    assert 46 * per_league_cost() == 184


# ---------------------------------------------------------------------------
# The email block: credits -> scans
# ---------------------------------------------------------------------------

def _quota(remaining: int, leagues: int = 46, cost_per: int = 4, reserve: int = 30):
    return {"remaining": remaining, "reserve": reserve,
            "leagues": leagues, "scan_cost": leagues * cost_per}


def test_email_warns_when_the_next_scan_is_unaffordable():
    from betbot.notifier import _render_quota_section

    html = _render_quota_section(_quota(remaining=150))
    assert "rotation de clé" in html.lower()
    assert "refusé" in html.lower()
    assert "184" in html, "must state what a scan actually costs"


def test_email_flags_the_last_scans_before_the_wall():
    from betbot.notifier import _render_quota_section

    html = _render_quota_section(_quota(remaining=336))  # (336-30)//184 = 1
    assert "1 scan" in html
    assert "prochaine clé" in html.lower()


def test_email_is_calm_when_the_budget_is_comfortable():
    from betbot.notifier import _render_quota_section

    html = _render_quota_section(_quota(remaining=1000))  # (1000-30)//184 = 5
    assert "5 scans" in html
    assert "rotation" not in html.lower()


def test_unknown_quota_renders_nothing_rather_than_a_wrong_number():
    from betbot.notifier import _render_quota_section

    assert _render_quota_section(_quota(remaining=-1)) == ""
    assert _render_quota_section(None) == ""
    assert _render_quota_section({}) == ""


def test_no_match_email_still_carries_the_quota_block():
    """On a quiet day `events_by_sport` is empty; pricing on it would hide the
    warning in exactly the case where the user needs to act."""
    from betbot.notifier import EmailNotifier

    html = EmailNotifier("u", "p", "r").render_no_value(quota=_quota(remaining=150))
    assert "rotation de clé" in html.lower()


def test_quota_status_prices_on_queried_leagues_not_surviving_events(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("ODDS_REGIONS", "eu,uk")
    from betbot.main import _quota_status

    client = MagicMock(quota_remaining=336)
    all_events = {f"soccer_l{i}": [] for i in range(46)}  # queried, none upcoming
    st = _quota_status(client, all_events)

    assert st["leagues"] == 46
    assert st["scan_cost"] == 184


# ---------------------------------------------------------------------------
# The daily email must not describe a workflow that no longer exists
# ---------------------------------------------------------------------------

def test_email_does_not_advertise_the_dead_validation_queue():
    """Under AUTO_CONFIRM_PICKS=1 picks are born confirmed: there is no
    "J'ai placé" debit, no "Skipper", no 36h auto-archive."""
    from betbot.notifier import EmailNotifier

    html = EmailNotifier("u", "p", "r").render_html([], [], {}, 100.0)
    for dead in ("J'ai placé", "Skipper", "36h", "auto-archive", "débité"):
        assert dead not in html, f"dead mechanism still advertised: {dead}"
