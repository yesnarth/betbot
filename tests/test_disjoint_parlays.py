"""Hard cross-combo disjointness guarantee — enforce_disjoint_parlays.

A match must never appear in two returned combos, so a single losing pick can
sink at most one combo (never 'one bet cancels another').
"""
from betbot.analysis import enforce_disjoint_parlays


def _parlay(event_ids, odds=10.0):
    return {"combined_odds": odds, "legs": [{"event_id": e} for e in event_ids]}


def test_drops_combo_sharing_a_match():
    parlays = [
        _parlay(["A", "B", "C"]),
        _parlay(["C", "D", "E"]),   # shares C with #1 → dropped
        _parlay(["F", "G", "H"]),   # disjoint → kept
    ]
    kept = enforce_disjoint_parlays(parlays)
    assert len(kept) == 2
    all_events = [leg["event_id"] for p in kept for leg in p["legs"]]
    assert len(all_events) == len(set(all_events))          # no repeated match
    assert kept[0]["legs"][0]["event_id"] == "A"            # greedy: keeps #1
    assert kept[1]["legs"][0]["event_id"] == "F"            # and #3, not #2


def test_keeps_all_when_already_disjoint():
    parlays = [_parlay(["A", "B"]), _parlay(["C", "D"]), _parlay(["E", "F"])]
    assert len(enforce_disjoint_parlays(parlays)) == 3


def test_empty_and_malformed_are_safe():
    assert enforce_disjoint_parlays([]) == []
    # No legs / no event_id → can't enforce → kept (never crashes).
    assert len(enforce_disjoint_parlays([{"legs": []}, {"foo": 1}])) == 2
