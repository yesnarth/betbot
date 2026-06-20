"""Selection discipline (anti "value-trap") — D2.

The edge formula (prob×odds−1) is easiest to satisfy on high-odds outcomes the
market priced as unlikely (and is usually right about). These gates drop the
weakest such picks:
  - max_book_odds        : cap extreme longshots (model error grows with odds)
  - underdog_odds/min_prob: require real conviction on underdogs
  - novig_required        : drop a pick when there's no consensus to validate it
All default OFF on detect_value_bets (so existing call sites/tests are unchanged);
config.py turns them on for the live scan paths.
"""
from betbot.analysis import detect_value_bets


def _h2h(home, draw, away):
    return {"key": "h2h", "outcomes": [
        {"name": "Alpha", "price": home},
        {"name": "Draw", "price": draw},
        {"name": "Beta", "price": away},
    ]}


def _longshot_event():
    # Heavy home favorite; one book quotes a FAT 9.0 outlier on the away longshot
    # → an artificial 'edge' on a low-probability outcome (the value trap).
    return {
        "id": "evt-ls-1", "home_team": "Alpha", "away_team": "Beta",
        "bookmakers": [
            {"key": "pinnacle", "title": "P", "markets": [_h2h(1.50, 4.50, 7.00)]},
            {"key": "bet365",   "title": "B", "markets": [_h2h(1.52, 4.40, 6.80)]},
            {"key": "unibet",   "title": "U", "markets": [_h2h(1.55, 4.20, 9.00)]},
        ],
    }


def _scan(events, **kw):
    # min_model_prob=0 so the low-prob longshot isn't blocked by that floor;
    # min_edge_vs_novig=0 isolates the gate under test (no-vig gate off).
    return detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                             min_model_prob=0.0, min_book_odds=1.50,
                             min_edge_vs_novig=0.0, **kw)


def test_longshot_surfaces_without_discipline(monkeypatch):
    monkeypatch.setattr("betbot.analysis.ml_calibrate", lambda p, *a, **k: p)
    bets = _scan({"soccer_epl": [_longshot_event()]})
    assert any(b.selection_code == "2" for b in bets), \
        "the 9.0 away longshot should surface as 'value' with no discipline"


def test_max_book_odds_drops_longshot(monkeypatch):
    monkeypatch.setattr("betbot.analysis.ml_calibrate", lambda p, *a, **k: p)
    bets = _scan({"soccer_epl": [_longshot_event()]}, max_book_odds=5.0)
    assert not any(b.selection_code == "2" for b in bets)


def test_underdog_floor_drops_low_conviction_longshot(monkeypatch):
    monkeypatch.setattr("betbot.analysis.ml_calibrate", lambda p, *a, **k: p)
    bets = _scan({"soccer_epl": [_longshot_event()]},
                 underdog_odds=3.0, underdog_min_prob=0.42)
    assert not any(b.selection_code == "2" for b in bets)


def test_novig_required_drops_pick_without_consensus(monkeypatch):
    # Force "no consensus available" → with novig_required the pick is dropped;
    # without it the gate abstains and the pick is allowed.
    monkeypatch.setattr("betbot.analysis.ml_calibrate", lambda p, *a, **k: p)
    monkeypatch.setattr("betbot.analysis._novig_fair_prob", lambda *a, **k: None)
    events = {"soccer_epl": [_longshot_event()]}
    abstain = detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                                min_model_prob=0.0, min_book_odds=1.50,
                                min_edge_vs_novig=0.05, novig_required=False)
    assert any(b.selection_code == "2" for b in abstain)
    dropped = detect_value_bets(events, {}, bankroll=100.0, min_value_edge=0.04,
                                min_model_prob=0.0, min_book_odds=1.50,
                                min_edge_vs_novig=0.05, novig_required=True)
    assert not any(b.selection_code == "2" for b in dropped)
