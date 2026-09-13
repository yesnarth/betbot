"""
Every path that creates a counted bet obeys the same discipline.

Under AUTO_CONFIRM_PICKS a pick from any mode is recorded as a placed bet, so
a filter missing on one endpoint is not a lesser version of the product — it is
a hole through which the losing population enters the track record.

The measured stakes, on the clean population (quarantine excluded, n=258):

    below 0.70 confidence : 167 picks, 37.1% hit, avg odds 2.12, ROI -24.6%
    at or above 0.70      :  91 picks, 75.8% hit, avg odds 1.60, ROI +22.6%

and picks above the 2.22 odds ceiling returned -47.3%. Those two filters are
the system.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

_RECOMMEND = Path("betbot_api/routers/recommend.py").read_text(encoding="utf-8")


def _call_block(marker: str, span: int = 1400) -> str:
    """The source of one endpoint's detector call."""
    i = _RECOMMEND.index(marker)
    return _RECOMMEND[i:i + span]


def _endpoint(path: str) -> str:
    """Everything from an endpoint's decorator to the next one."""
    i = _RECOMMEND.index(f'@router.post("{path}"')
    j = _RECOMMEND.find("@router.post(", i + 10)
    return _RECOMMEND[i:j if j > 0 else len(_RECOMMEND)]


# ---------------------------------------------------------------------------
# Live — historised and auto-confirmed like the rest
# ---------------------------------------------------------------------------

def test_live_accepts_a_confidence_floor():
    from betbot.live import scan_live

    assert "min_model_prob" in inspect.signature(scan_live).parameters


def test_live_accepts_the_odds_ceiling():
    """The strongest measured filter in the system, and in-play was the one
    path with neither cap nor floor wired."""
    from betbot.live import scan_live

    assert "max_book_odds" in inspect.signature(scan_live).parameters


def test_live_endpoint_actually_passes_the_floor():
    """The parameter existed on scan_live and was never passed from the
    endpoint, so it sat at its 0.0 default. A half-finished fix reads exactly
    like a finished one."""
    block = _call_block("picks = scan_live(")
    assert "min_model_prob=s.min_model_prob" in block
    assert "max_book_odds=s.max_book_odds" in block


def test_live_endpoint_clamps_its_sliders():
    block = _call_block("picks = scan_live(")
    assert "max(filters.min_edge, s.min_value_edge)" in block
    assert "max(filters.min_odds, s.min_book_odds)" in block


def test_live_refuses_a_price_above_the_ceiling():
    from betbot.live import scan_live

    assert scan_live({}, {}, {}, bankroll=100.0, min_model_prob=0.70,
                     max_book_odds=2.22) == []


# ---------------------------------------------------------------------------
# x1000 — more legs, not longer ones
# ---------------------------------------------------------------------------

def test_x1000_applies_the_odds_ceiling():
    """It was missing here alone, so the lottery mode could build its legs out
    of precisely the population every other mode refuses."""
    block = _endpoint("/recommend/parlay-target")
    assert "max_book_odds=s.max_book_odds" in block


def test_x1000_clamps_edge_and_leg_odds():
    block = _endpoint("/recommend/parlay-target")
    assert "max(filters.min_edge, s.min_value_edge)" in block
    assert "max(filters.min_leg_odds, s.min_book_odds)" in block


def test_x1000_keeps_the_confidence_floor():
    block = _endpoint("/recommend/parlay-target")
    assert "max(filters.min_prob, s.min_model_prob)" in block


def test_x1000_carries_the_remaining_server_guards():
    """underdog rules and the no-vig gate were server-side everywhere else."""
    block = _endpoint("/recommend/parlay-target")
    for guard in ("underdog_odds=s.underdog_odds",
                  "underdog_min_prob=s.underdog_min_prob",
                  "novig_required=s.novig_required"):
        assert guard in block, f"missing {guard}"


# ---------------------------------------------------------------------------
# No mode may quietly hand a slider straight to the engine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("param", ["min_model_prob", "min_book_odds", "min_value_edge"])
def test_no_endpoint_passes_a_raw_slider_for_a_disciplined_filter(param):
    """Catches the shape of the original bug: `min_model_prob=filters.min_prob`
    with no clamp, which let a 0.40 sidebar default silently override a 0.70
    server floor on every dashboard scan."""
    raw = re.findall(rf"{param}=filters\.\w+", _RECOMMEND)
    assert not raw, f"unclamped slider reaches the engine: {raw}"
