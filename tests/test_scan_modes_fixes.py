"""
Regression tests for the 2026-08-06 scan-mode audit (5 critical findings).

Each test pins one way a scan mode was fabricating counted bets outside the
0.70 confidence zone, or crashing.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# #2 — Live: the API passes discipline kwargs; scan_live must accept them
# ---------------------------------------------------------------------------

def test_scan_live_accepts_the_discipline_kwargs():
    """The API call was extended with max_value_edge / derive_dnb /
    allow_totals_over / kelly_edge_cap without updating scan_live's signature.
    Result: TypeError -> HTTP 500, but ONLY when at least one match was live —
    with no live match the mode returned a clean '0 picks' and looked healthy.
    It failed exactly when it was supposed to serve."""
    from betbot.live import scan_live

    params = inspect.signature(scan_live).parameters
    for kw in ("max_value_edge", "derive_dnb", "allow_totals_over",
               "kelly_edge_cap", "min_model_prob"):
        assert kw in params, f"scan_live must accept {kw}"


def test_scan_live_runs_with_the_full_api_call_shape():
    """Exact kwargs the endpoint sends — must not raise."""
    from betbot.live import scan_live

    out = scan_live(
        {}, {}, {},
        bankroll=100.0, kelly_fraction=0.25,
        min_value_edge=0.04, max_value_edge=0.0,
        derive_dnb=False, allow_totals_over=False, kelly_edge_cap=0.10,
        min_book_odds=1.50, min_edge_vs_novig=0.03,
        min_model_prob=0.70,
    )
    assert out == []


# ---------------------------------------------------------------------------
# #1 — Auto-scan ladder: persisted singles stay strict
# ---------------------------------------------------------------------------

def test_ladder_returns_strict_singles_even_when_relaxing_for_combos(monkeypatch):
    """`_ensure_min_combos` relaxes down to prob 0.30 / odds 1.20 to fill
    parlays. It used to return the LAST attempt's ranked list, so the daily
    auto-scan itself persisted 0.30-confidence picks born 'confirmed'."""
    import betbot.main as main_mod

    strict_pick = MagicMock(name="strict")
    loose_pick = MagicMock(name="loose")
    calls = {"n": 0}

    def fake_detect(**kwargs):
        calls["n"] += 1
        return [strict_pick] if calls["n"] == 1 else [strict_pick, loose_pick]

    monkeypatch.setattr(main_mod, "detect_value_bets", fake_detect)
    monkeypatch.setattr(main_mod, "rank_value_bets", lambda bets: list(bets))
    # No attempt ever reaches min_combos -> full ladder runs.
    monkeypatch.setattr(main_mod, "build_parlays", lambda *a, **k: [])

    settings = MagicMock(
        bankroll=100.0, kelly_fraction=0.25, min_value_edge=0.04,
        max_value_edge=0.0, min_model_prob=0.70, min_book_odds=1.50,
        min_edge_vs_novig=0.03, max_book_odds=2.22, underdog_odds=3.0,
        underdog_min_prob=0.42, novig_required=True, derive_dc_dnb=True,
        derive_dnb=False, allow_totals_over=False, kelly_edge_cap=0.10,
        derived_min_edge=0.02, derived_min_odds=1.10, top_bets=25, top_combos=3,
    )
    singles, parlays = main_mod._ensure_min_combos(
        {}, {}, settings, min_combos=3, logger=MagicMock(),
    )
    assert singles == [strict_pick], (
        "persisted singles must come from the STRICT attempt only — "
        "the relaxed passes exist to fill parlays, not the track record"
    )
    assert calls["n"] == 5, "the full ladder still runs for parlays"


# ---------------------------------------------------------------------------
# #5 — One counted bet per match
# ---------------------------------------------------------------------------

def test_historize_keeps_only_the_most_confident_selection_per_event():
    """One event used to yield up to 3+ correlated selections ('1', '1X',
    'U25'), each historised and counted as an independent 1-unit bet."""
    from betbot_api.routers.recommend import _historize_picks

    db = MagicMock()
    db.save_prediction.return_value = True
    picks = [
        {"event_id": "e1", "market": "h2h", "selection_code": "1",
         "model_prob": 0.71, "best_odds": 1.9},
        {"event_id": "e1", "market": "double_chance", "selection_code": "1X",
         "model_prob": 0.88, "best_odds": 1.25},
        {"event_id": "e1", "market": "totals", "selection_code": "U25",
         "model_prob": 0.72, "best_odds": 1.8},
        {"event_id": "e2", "market": "h2h", "selection_code": "2",
         "model_prob": 0.75, "best_odds": 1.6},
    ]
    saved = _historize_picks(db, picks, source="test")

    assert saved == 2, "one selection per event, two events"
    selections = {c.kwargs["selection"] for c in db.save_prediction.call_args_list}
    assert selections == {"1X", "2"}, "the most confident selection wins"


def test_historize_without_event_id_is_skipped_not_crashed():
    from betbot_api.routers.recommend import _historize_picks

    db = MagicMock()
    assert _historize_picks(db, [{"market": "h2h", "selection_code": "1"}], "t") == 0
    db.save_prediction.assert_not_called()
