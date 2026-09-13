"""
Quota gates: never pay the Odds API for a question that has no answer.

Two jobs used to select every unresolved pick, group by `sport_key`, and issue
one billed call per group — before any filter on whether the call could
possibly return something useful.

Measured on production 2026-08-01, against a 500 requests/month quota:

  * `resolve_pending` — 118 pending picks over 25 leagues, of which 110 were
    older than the `/scores` `daysFrom` ceiling of 3 days. The catch-up job
    runs at every worker boot and spent ~50 credits to resolve exactly zero.
    A handful of deploys in one day drained the month.
  * `snapshot_closing_odds` — 25 leagues x 2 regions x 2 markets = 100 credits
    per 10-minute cycle, because the row carried no kickoff to filter on. This
    is why CLV shipped disabled.

Both now filter first and call second.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from betbot.resolver import resolve_pending


def _now():
    return datetime.now(timezone.utc)


def _pick(sport: str, days_ago: float, event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or f"evt-{sport}-{days_ago}",
        "sport_key": sport,
        "market": "h2h",
        "selection": "1",
        "home_team": "A",
        "away_team": "B",
        "created_at": (_now() - timedelta(days=days_ago)).isoformat(),
    }


def _db(picks: list[dict]) -> MagicMock:
    db = MagicMock()
    db.get_pending_predictions.return_value = picks
    return db


def test_all_picks_out_of_window_costs_zero_requests():
    """The production case: every pending pick is older than the /scores window.

    Before the gate this issued one billed call per league — 25 calls, ~50
    credits, zero resolutions — on every single worker boot.
    """
    picks = [_pick(f"league_{i}", days_ago=10) for i in range(25)]
    client = MagicMock()
    client.get_scores.side_effect = AssertionError(
        "get_scores must not be called for out-of-window picks"
    )

    result = resolve_pending(_db(picks), client)

    assert client.get_scores.call_count == 0
    assert result == {"resolved": 0, "still_pending": 25, "errors": 0}


def test_only_in_window_leagues_are_queried():
    """A few recent picks must not drag every stale league into the call set."""
    stale = [_pick(f"stale_{i}", days_ago=10) for i in range(22)]
    fresh = [_pick(f"fresh_{i}", days_ago=1) for i in range(3)]
    client = MagicMock()
    client.get_scores.return_value = []

    resolve_pending(_db(stale + fresh), client)

    assert client.get_scores.call_count == 3, "one call per in-window league only"
    queried = {c.args[0] if c.args else c.kwargs["sport"]
               for c in client.get_scores.call_args_list}
    assert queried == {"fresh_0", "fresh_1", "fresh_2"}


def test_still_pending_counts_every_pick_not_just_the_queried_ones():
    """Skipping a pick for cost reasons must not make it disappear from the
    tally — it is still pending, it just cannot be graded by this path."""
    picks = [_pick("stale", days_ago=10), _pick("fresh", days_ago=1)]
    client = MagicMock()
    client.get_scores.return_value = []

    result = resolve_pending(_db(picks), client)

    assert result["still_pending"] == 2


def test_no_pending_picks_is_free():
    client = MagicMock()
    client.get_scores.side_effect = AssertionError("must not be called")
    assert resolve_pending(_db([]), client) == {
        "resolved": 0, "still_pending": 0, "errors": 0,
    }


@pytest.mark.parametrize("days_ago,expected_calls", [
    (0.5, 1),    # today
    (2.5, 1),    # inside the 3-day window
    (3.5, 0),    # past the ceiling
    (30.0, 0),   # long past
])
def test_window_boundary(days_ago, expected_calls):
    client = MagicMock()
    client.get_scores.return_value = []
    resolve_pending(_db([_pick("league", days_ago=days_ago)]), client)
    assert client.get_scores.call_count == expected_calls


def test_days_from_parameter_moves_the_window():
    """A caller asking for a wider window gets a wider window."""
    client = MagicMock()
    client.get_scores.return_value = []
    resolve_pending(_db([_pick("league", days_ago=5)]), client, days_from=7)
    assert client.get_scores.call_count == 1


# ---------------------------------------------------------------------------
# Pre-flight: refuse a scan the quota cannot cover end to end
# ---------------------------------------------------------------------------

def _client_with(monkeypatch, *, leagues: int, remaining: int,
                 regions: str = "eu,uk", minimum: str = "30"):
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    monkeypatch.setenv("ODDS_REGIONS", regions)
    monkeypatch.setenv("ODDS_QUOTA_MINIMUM", minimum)
    monkeypatch.setenv("SCAN_ALL_SOCCER", "1")
    client = OddsAPIClient("key")
    active = {f"soccer_league_{i:03d}" for i in range(leagues)}
    return client, patch.multiple(
        OddsAPIClient,
        get_active_sports=lambda self: active,
        probe_quota=lambda self: remaining,
    )


def test_scan_is_refused_when_quota_cannot_cover_every_league(monkeypatch):
    """`to_query` is sorted, so a run that dies halfway always drops the SAME
    tail of leagues — a selection bias stable over time, not a coverage gap.

    Real case: 43 leagues x 2 regions x 2 markets = 172 requests, 156 credits
    left, 30 held in reserve. The scan would have covered 31 leagues and
    silently skipped the last 12, every time.
    """
    from unittest.mock import patch

    from betbot.api import OddsAPIClient, QuotaExhaustedError

    client, ctx = _client_with(monkeypatch, leagues=43, remaining=156)
    with ctx, patch.object(
        OddsAPIClient, "get_events_with_odds",
        side_effect=AssertionError("no billed call may happen"),
    ):
        with pytest.raises(QuotaExhaustedError) as exc:
            client.fetch_all_sports()
    assert "172" in str(exc.value), "message must state the real cost"
    assert "156" in str(exc.value), "message must state each key's remaining"


def test_scan_proceeds_when_the_budget_covers_it(monkeypatch):
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    client, ctx = _client_with(monkeypatch, leagues=10, remaining=500)
    with ctx, patch.object(OddsAPIClient, "get_events_with_odds", return_value=[]):
        result = client.fetch_all_sports()
    assert len(result) == 10


def test_single_region_halves_the_cost_and_unblocks_the_scan(monkeypatch):
    """Same 43 leagues and same 156 credits, but regions=eu: 86 requests,
    which fits in the 126-credit budget."""
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    client, ctx = _client_with(monkeypatch, leagues=43, remaining=156, regions="eu")
    with ctx, patch.object(OddsAPIClient, "get_events_with_odds", return_value=[]):
        assert len(client.fetch_all_sports()) == 43


def test_unknown_quota_does_not_block(monkeypatch):
    """probe_quota returns -1 when the header was never observed. Refusing on
    an unknown quota would make the bot unusable after a cold start."""
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    client, ctx = _client_with(monkeypatch, leagues=43, remaining=-1)
    with ctx, patch.object(OddsAPIClient, "get_events_with_odds", return_value=[]):
        assert len(client.fetch_all_sports()) == 43


# ---------------------------------------------------------------------------
# A refused scan must READ as a decision, not as a crash
# ---------------------------------------------------------------------------

def test_refused_scan_returns_429_with_the_explanation(monkeypatch):
    """`fetch_all_sports` raises QuotaExhaustedError deliberately, with a message
    stating the exact cost, the budget and the ways out. Uncaught, FastAPI
    turned it into a bare 500 and the dashboard displayed "Erreur interne du
    backend" — the user saw a broken app while the bot was telling them
    something useful. Three scans failed that way in production.
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    import betbot_api.main as api_main
    from betbot.api import OddsAPIClient, QuotaExhaustedError
    from betbot_api.auth import require_auth

    message = (
        "Scan refusé : 45 ligue(s) × 4 = 180 requêtes nécessaires, mais "
        "seulement 84 disponibles. Change de clé Odds API."
    )
    api_main.app.dependency_overrides[require_auth] = lambda: "ok"
    try:
        client = TestClient(api_main.app, raise_server_exceptions=False)
        with patch.object(
            OddsAPIClient, "fetch_all_sports",
            side_effect=QuotaExhaustedError(message),
        ):
            resp = client.post("/recommend/manual", json={"today_only": True})
    finally:
        api_main.app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 429, "a refused scan is not a server fault"
    assert "Scan refusé" in resp.json()["detail"], "the explanation must reach the client"


def test_dashboard_labels_a_quota_refusal_accurately():
    """429 covers both the per-minute rate limiter (retrying works) and a quota
    refusal (retrying changes nothing — the quota is monthly). Telling the user
    to "retry in a moment" on a monthly quota sends them in circles."""
    import httpx

    from betbot_dashboard.api_client import _friendly_status

    quota = httpx.Response(
        429, json={"detail": "Scan refusé : quota insuffisant, 84 disponibles."},
        request=httpx.Request("POST", "http://x/recommend/manual"),
    )
    assert "Quota Odds API insuffisant" in _friendly_status(quota)
    assert "réessaie dans un instant" not in _friendly_status(quota)

    burst = httpx.Response(
        429, json={"detail": "10 per 1 minute"},
        request=httpx.Request("POST", "http://x/recommend/manual"),
    )
    assert "réessaie dans un instant" in _friendly_status(burst)

# ---------------------------------------------------------------------------
# Automatic key rotation — the user stops rotating keys by hand
# ---------------------------------------------------------------------------

def _rotation_client(monkeypatch, quotas: dict, *, leagues: int = 43):
    """Client with an ODDS_API_KEYS pool whose per-key quota is `quotas`."""
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    monkeypatch.setenv("ODDS_REGIONS", "eu,uk")
    monkeypatch.setenv("ODDS_QUOTA_MINIMUM", "30")
    monkeypatch.setenv("SCAN_ALL_SOCCER", "1")
    monkeypatch.setenv("ODDS_API_KEYS", ",".join(quotas))
    client = OddsAPIClient(next(iter(quotas)))
    active = {f"soccer_league_{i:03d}" for i in range(leagues)}
    ctx = patch.multiple(
        OddsAPIClient,
        get_active_sports=lambda self: active,
        probe_quota=lambda self: quotas[self._key],
    )
    return client, ctx


def test_rotation_selects_the_first_key_with_budget(monkeypatch):
    """Key A is dry (114 < 180 needed + 30 reserve), key B is fresh: the scan
    must run on B without any human touching the .env — this manual rotation
    was the user's number-one complaint."""
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    client, ctx = _rotation_client(monkeypatch, {"key-aaaa": 114, "key-bbbb": 500})
    used: list[str] = []
    def _record(self, sport):
        used.append(self._key)
        return []
    with ctx, patch.object(OddsAPIClient, "get_events_with_odds", _record):
        result = client.fetch_all_sports()
    assert len(result) == 43
    assert set(used) == {"key-bbbb"}, "every billed call must use the fresh key"


def test_rotation_refusal_names_every_key(monkeypatch):
    """When ALL keys are dry the refusal must say so per key — otherwise the
    user cannot tell a dry pool from a single dry key."""
    from unittest.mock import patch

    from betbot.api import OddsAPIClient, QuotaExhaustedError

    client, ctx = _rotation_client(monkeypatch, {"key-aaaa": 114, "key-bbbb": 100})
    with ctx, patch.object(
        OddsAPIClient, "get_events_with_odds",
        side_effect=AssertionError("no billed call on a dry pool"),
    ):
        with pytest.raises(QuotaExhaustedError) as exc:
            client.fetch_all_sports()
    msg = str(exc.value)
    assert "aaaa=114" in msg and "bbbb=100" in msg


def test_rotation_reselects_on_every_scan(monkeypatch):
    """A key that was dry yesterday may have reset since (monthly quotas).
    The selection must not stick across scans."""
    from unittest.mock import patch

    from betbot.api import OddsAPIClient

    quotas = {"key-aaaa": 114, "key-bbbb": 500}
    client, ctx = _rotation_client(monkeypatch, quotas)
    with ctx, patch.object(OddsAPIClient, "get_events_with_odds", return_value=[]):
        client.fetch_all_sports()
        assert client._selected_key == "key-bbbb"
        quotas["key-aaaa"] = 500  # the monthly reset happened
        client.fetch_all_sports()
        assert client._selected_key == "key-aaaa", "priority order must re-apply"
