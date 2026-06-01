"""No-vig fair-line gate + adverse-selection filtering — Wave 1, items 1.1 & 1.3.

The gate requires the model to beat the market's *consensus* vig-removed line,
not merely the single best (often stale) price. This guards against the
"winner's curse": always taking the most generous bookmaker odd systematically
selects the book whose line is most out-of-date.
"""
import pytest

from betbot.analysis import _novig_fair_prob, detect_value_bets


def _h2h(home: float, draw: float, away: float) -> dict:
    return {"key": "h2h", "outcomes": [
        {"name": "Alpha", "price": home},
        {"name": "Draw", "price": draw},
        {"name": "Beta", "price": away},
    ]}


def _event() -> dict:
    # Two sharp books agree (~46% home, no-vig); one book is a STALE outlier
    # quoting 2.50 on the home side — the classic adverse-selection trap.
    return {
        "id": "evt-novig-1",
        "home_team": "Alpha",
        "away_team": "Beta",
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "markets": [_h2h(2.00, 3.40, 4.00)]},
            {"key": "bet365",   "title": "Bet365",   "markets": [_h2h(2.05, 3.30, 3.90)]},
            {"key": "unibet",   "title": "Unibet",   "markets": [_h2h(2.50, 3.10, 3.60)]},
        ],
    }


def test_novig_fair_prob_is_weighted_consensus():
    p = _novig_fair_prob(_event(), "Alpha", "h2h", None, {"Alpha", "Draw", "Beta"})
    assert p is not None
    assert 0.44 < p < 0.49   # weight-averaged no-vig consensus ≈ 0.46


def test_novig_fair_prob_abstains_when_group_unpriced():
    # No book quotes the {Over, Under} totals group → return None (gate abstains).
    assert _novig_fair_prob(_event(), "Over", "totals", 2.5, {"Over", "Under"}) is None


def test_gate_rejects_adverse_selection_pick(monkeypatch):
    # Isolate the gate from the ML calibrator (identity) for a deterministic test.
    # (calibrate now takes an optional segment arg → accept *args.)
    monkeypatch.setattr("betbot.analysis.ml_calibrate", lambda p, *a, **k: p)
    events = {"soccer_epl": [_event()]}

    # Gate OFF : the fat best-odds (2.50) edge is surfaced as "value".
    off = detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                            min_model_prob=0.40, min_book_odds=1.50,
                            min_edge_vs_novig=0.0)
    assert any(b.selection_code == "1" for b in off), \
        "gate off should surface the home pick on the outlier price"

    # Gate ON : the model only matches the consensus — no real edge versus the
    # fair line — so the adverse-selection pick is filtered out.
    on = detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                           min_model_prob=0.40, min_book_odds=1.50,
                           min_edge_vs_novig=0.05)
    assert not any(b.selection_code == "1" for b in on), \
        "gate on should reject a pick that doesn't beat the no-vig consensus"
