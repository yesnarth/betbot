"""CLV-by-segment grouping — _group_clv (vue CLV par segment)."""
from betbot.clv import _group_clv


def test_group_clv_buckets_sorts_and_stats():
    rows = [
        (2.20, 2.00, "soccer_epl", "h2h"),            # +10%
        (2.10, 2.00, "soccer_epl", "h2h"),            # +5%
        (1.80, 2.00, "soccer_epl", "totals"),         # -10%
        (3.00, 2.50, "soccer_spain_la_liga", "h2h"),  # +20%
    ]
    out = _group_clv(rows)
    assert len(out) == 3                                  # 3 distinct (league, market)
    assert out[0]["sport_key"] == "soccer_spain_la_liga"  # best CLV sorted first

    epl_h2h = next(s for s in out if s["sport_key"] == "soccer_epl" and s["market"] == "h2h")
    assert epl_h2h["n_with_clv"] == 2
    assert epl_h2h["avg_clv_pct"] == 7.5
    assert epl_h2h["positive_clv_share"] == 100.0

    epl_totals = next(s for s in out if s["market"] == "totals")
    assert epl_totals["avg_clv_pct"] == -10.0
    assert epl_totals["positive_clv_share"] == 0.0


def test_group_clv_empty():
    assert _group_clv([]) == []
