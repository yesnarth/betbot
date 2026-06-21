"""Model-performance aggregation (would-have ROI / win-rate / calibration) — E1.

Pure helpers over (sport_key, market, model_prob, best_odds, result) rows.
"""
from betbot.perf import (
    _flat_profit, _perf_stats, _group_performance, _calibration_buckets,
)


def _r(sport, market, prob, odds, result):
    return (sport, market, prob, odds, result)


def test_flat_profit_win_loss_void():
    assert _flat_profit(2.5, "win") == 1.5
    assert _flat_profit(2.5, "loss") == -1.0
    assert _flat_profit(2.5, "void") == 0.0   # stake returned


def test_perf_stats_winrate_excludes_voids_and_roi_is_flat():
    rows = [
        _r("soccer_epl", "h2h", 0.5, 3.0, "win"),    # +2.0
        _r("soccer_epl", "h2h", 0.5, 2.0, "loss"),   # -1.0
        _r("soccer_epl", "h2h", 0.5, 2.0, "loss"),   # -1.0
        _r("soccer_epl", "h2h", 0.5, 2.0, "void"),   #  0.0
    ]
    s = _perf_stats(rows)
    assert s["n"] == 4 and s["wins"] == 1 and s["losses"] == 2
    assert s["win_rate"] == round(1 / 3 * 100, 1)             # voids excluded → 33.3
    assert s["roi_pct"] == 0.0                                # (2-1-1+0)/4


def test_perf_stats_empty():
    assert _perf_stats([])["n"] == 0


def test_group_performance_sorted_by_roi_desc():
    rows = [
        _r("soccer_epl", "h2h", 0.4, 5.0, "win"),                 # +400% ROI
        _r("soccer_spain_la_liga", "totals", 0.5, 2.0, "loss"),   # -100% ROI
    ]
    out = _group_performance(rows)
    assert len(out) == 2
    assert out[0]["sport_key"] == "soccer_epl"        # best ROI first
    assert out[0]["roi_pct"] > out[1]["roi_pct"]


def test_calibration_buckets_band_winrate():
    rows = [
        _r("x", "h2h", 0.55, 2.0, "win"),
        _r("x", "h2h", 0.55, 2.0, "loss"),
        _r("x", "h2h", 0.30, 4.0, "win"),
        _r("x", "h2h", 0.30, 4.0, "void"),   # excluded from calibration
    ]
    cal = _calibration_buckets(rows)
    b55 = next(c for c in cal if c["bucket"].startswith("0.50"))
    assert b55["n"] == 2 and b55["actual_win_rate"] == 50.0 and b55["expected_win_rate"] == 55.0
    b_low = next(c for c in cal if c["bucket"].startswith("0.00"))
    assert b_low["n"] == 1 and b_low["actual_win_rate"] == 100.0   # void excluded
