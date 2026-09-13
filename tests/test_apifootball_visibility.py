"""
The second subscription must be visible before it bites.

api-football was invisible: no quota gauge, no expiry warning. Its daily
allowance ran dry on 2026-08-18 and every league silently refreshed to
"0 leagues, 0 teams" — discovered by accident, hours later. The owner recharges
both plans on the same monthly cycle, so both deadlines belong in one glance.

Two traps this module exists to avoid, both met head-on that day:

* The response HEADERS lie. With the daily allowance spent, api-football still
  advertised 7,499 of 7,500 remaining while refusing every endpoint.
* The BODY tells the truth in `errors.requests`, and its `response` is empty —
  so code reading `response.subscription` sees None and reports "unknown",
  which is how an exhausted quota passes for a misconfiguration.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from betbot.data_sources import api_football as af


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    af._STATUS_CACHE.update(at=0.0, value=None)
    yield
    af._STATUS_CACHE.update(at=0.0, value=None)


def _reply(payload):
    return MagicMock(status_code=200, json=lambda: payload)


def _ok_payload(end="2026-09-18T13:34:41+00:00", active=True,
                current=1200, limit=7500):
    return {"errors": [], "response": {
        "subscription": {"plan": "Pro", "active": active, "end": end},
        "requests": {"current": current, "limit_day": limit},
    }}


# ---------------------------------------------------------------------------
# Reading the truth
# ---------------------------------------------------------------------------

def test_a_healthy_account_reports_its_plan_and_deadline():
    with patch.object(af.requests, "get", return_value=_reply(_ok_payload())):
        st = af.account_status(force=True)

    assert st["state"] == "ok"
    assert st["plan"] == "Pro"
    assert st["requests_used"] == 1200
    assert st["requests_limit"] == 7500
    assert st["days_left"] is not None


def test_an_exhausted_day_is_named_as_such_not_as_unknown():
    """The payload carries an empty `response`, so anything reading
    `response.subscription` concludes "unknown" — and an exhausted quota then
    looks like a configuration problem."""
    payload = {"errors": {"requests": "You have reached the request limit for the day"},
               "response": []}
    with patch.object(af.requests, "get", return_value=_reply(payload)):
        st = af.account_status(force=True)

    assert st["state"] == "daily_limit_reached"


def test_an_inactive_subscription_is_distinguished_from_a_spent_day():
    """Two different problems needing two different actions: renew, or wait."""
    with patch.object(af.requests, "get",
                      return_value=_reply(_ok_payload(active=False))):
        st = af.account_status(force=True)

    assert st["state"] == "inactive"


def test_a_missing_key_is_not_reported_as_a_quota_problem(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "")
    assert af.account_status(force=True)["state"] == "unconfigured"


def test_an_unreachable_provider_never_raises():
    with patch.object(af.requests, "get", side_effect=OSError("boom")):
        assert af.account_status(force=True)["state"] == "unreachable"


# ---------------------------------------------------------------------------
# Reading the quota must not be what exhausts it
# ---------------------------------------------------------------------------

def test_the_status_probe_is_cached():
    """/health is polled every few seconds and /status costs a request like
    any other."""
    with patch.object(af.requests, "get",
                      return_value=_reply(_ok_payload())) as g:
        af.account_status(force=True)
        for _ in range(20):
            af.account_status()

    assert g.call_count == 1


def test_force_bypasses_the_cache():
    with patch.object(af.requests, "get",
                      return_value=_reply(_ok_payload())) as g:
        af.account_status(force=True)
        af.account_status(force=True)

    assert g.call_count == 2


# ---------------------------------------------------------------------------
# Days remaining
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iso,expected_sign", [
    ("2099-01-01T00:00:00+00:00", 1),
    ("2000-01-01T00:00:00+00:00", -1),
])
def test_days_until_handles_past_and_future(iso, expected_sign):
    days = af._days_until(iso)
    assert days is not None
    assert (1 if days >= 0 else -1) == expected_sign


def test_days_until_tolerates_garbage():
    assert af._days_until(None) is None
    assert af._days_until("pas une date") is None


# ---------------------------------------------------------------------------
# It reaches the surfaces the owner actually looks at
# ---------------------------------------------------------------------------

def test_the_health_payload_carries_it():
    from betbot_api.schemas import HealthResponse

    assert "apifootball" in HealthResponse.model_fields


def test_the_daily_email_warns_before_expiry():
    from betbot.notifier import _render_quota_section

    html = _render_quota_section({
        "remaining": 19000, "reserve": 30, "leagues": 46, "scan_cost": 92,
        "apifootball": {"state": "ok", "days_left": 2},
    })
    assert "api-football expire dans 2 jour" in html


def test_the_email_stays_calm_when_both_plans_are_healthy():
    from betbot.notifier import _render_quota_section

    html = _render_quota_section({
        "remaining": 19000, "reserve": 30, "leagues": 46, "scan_cost": 92,
        "apifootball": {"state": "ok", "days_left": 25},
    })
    assert "expire dans" not in html
    assert "25 jour(s) d'abonnement" in html


def test_a_spent_day_does_not_alarm_the_email():
    """It costs nothing in picks: scans run on the Odds API."""
    from betbot.notifier import _render_quota_section

    html = _render_quota_section({
        "remaining": 19000, "reserve": 30, "leagues": 46, "scan_cost": 92,
        "apifootball": {"state": "daily_limit_reached"},
    })
    assert "Sans effet sur les pronostics" in html


def test_the_email_survives_an_unknown_state():
    from betbot.notifier import _render_quota_section

    html = _render_quota_section({
        "remaining": 19000, "reserve": 30, "leagues": 46, "scan_cost": 92,
        "apifootball": {},
    })
    assert "19000" in html
