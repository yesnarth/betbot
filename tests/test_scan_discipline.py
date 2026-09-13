"""
Two guarantees for the scan modes:

1. TIGHTEN-ONLY — dashboard sliders may demand more than the server discipline,
   never less. The sidebar's min_prob slider defaulted to 0.40 and always sent
   an explicit value, so `filters.min_prob if ... is not None else s.min_model_prob`
   silently bypassed the 0.70 confidence floor on every dashboard scan, while
   the worker's auto-scan respected it. Under AUTO_CONFIRM_PICKS every returned
   pick is a counted bet: the track record was mixing two populations.

2. SAME-HOUR SLOT — the user's manual workflow is batch betting: pick one hour,
   every match kicks off together, every bet resolves together.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from betbot.shared import filter_by_kickoff_hour
from betbot_api.routers.recommend import _apply_kickoff_hour, _tighten_only


# ---------------------------------------------------------------------------
# Tighten-only clamp
# ---------------------------------------------------------------------------

_SERVER = SimpleNamespace(min_value_edge=0.04, min_model_prob=0.70, min_book_odds=1.50)


def _filters(edge=None, prob=None, odds=None):
    return SimpleNamespace(min_edge=edge, min_prob=prob, min_odds=odds)


def test_slider_below_the_floor_is_clamped_up():
    """The historical bug: sidebar sent 0.40 while the server floor was 0.70."""
    edge, prob, odds = _tighten_only(_filters(prob=0.40), _SERVER)
    assert prob == 0.70


def test_slider_above_the_floor_tightens():
    edge, prob, odds = _tighten_only(_filters(prob=0.80, edge=0.06, odds=1.60), _SERVER)
    assert (edge, prob, odds) == (0.06, 0.80, 1.60)


def test_absent_filters_fall_back_to_server_discipline():
    edge, prob, odds = _tighten_only(_filters(), _SERVER)
    assert (edge, prob, odds) == (0.04, 0.70, 1.50)


@pytest.mark.parametrize("field,low", [("min_edge", -0.10), ("min_odds", 1.0)])
def test_every_floor_is_unforgeable(field, low):
    f = _filters()
    setattr(f, {"min_edge": "min_edge", "min_odds": "min_odds"}[field], low)
    edge, prob, odds = _tighten_only(f, _SERVER)
    assert edge >= _SERVER.min_value_edge
    assert odds >= _SERVER.min_book_odds


# ---------------------------------------------------------------------------
# Same-hour kickoff slot
# ---------------------------------------------------------------------------

def _event(iso_utc: str, eid: str = "e") -> dict:
    return {"id": eid, "commence_time": iso_utc}


def test_slot_keeps_only_the_requested_paris_hour():
    """19:00 UTC in August = 21:00 Paris (UTC+2). The slot is PARIS-local:
    that is the clock the user reads on Betclic."""
    events = [
        _event("2026-08-08T18:00:00Z", "a"),  # 20:00 Paris
        _event("2026-08-08T19:00:00Z", "b"),  # 21:00 Paris
        _event("2026-08-08T19:45:00Z", "c"),  # 21:45 Paris — same slot
        _event("2026-08-08T20:00:00Z", "d"),  # 22:00 Paris
    ]
    kept = filter_by_kickoff_hour(events, hour=21)
    assert [e["id"] for e in kept] == ["b", "c"]


def test_missing_or_broken_kickoff_is_excluded():
    """A match we cannot place in a slot does not belong to a slot-filtered
    scan — including it would defeat the batch workflow."""
    events = [_event("", "a"), {"id": "b"}, _event("not-a-date", "c"),
              _event("2026-08-08T19:00:00Z", "d")]
    kept = filter_by_kickoff_hour(events, hour=21)
    assert [e["id"] for e in kept] == ["d"]


def test_apply_kickoff_hour_drops_empty_leagues():
    by_sport = {
        "epl": [_event("2026-08-08T19:00:00Z", "a")],   # 21:00 Paris
        "liga": [_event("2026-08-08T14:00:00Z", "b")],  # 16:00 Paris
    }
    out = _apply_kickoff_hour(by_sport, 21)
    assert set(out) == {"epl"}


def test_apply_kickoff_hour_none_is_a_no_op():
    by_sport = {"epl": [_event("2026-08-08T19:00:00Z")]}
    assert _apply_kickoff_hour(by_sport, None) is by_sport
