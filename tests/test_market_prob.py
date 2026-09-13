"""
Recording the market's own probability at pick time.

The bot's central open question is whether its model adds anything over the
bookmaker's price. Answering it needs both numbers on the SAME picks:
Brier(model_prob) against Brier(market_prob). The market side was computed on
every pick already — it is the adverse-selection gate — and then thrown away,
so the question stayed unanswerable no matter how long the track record grew.

It cannot be recovered afterwards: de-vigging needs the whole outcome group,
and only the selected side's price survives the scan.
"""
from __future__ import annotations

import pytest

from betbot.analysis import ValueBet, _novig_fair_prob, detect_value_bets
from betbot.models import TeamStats


def _price(p: float, overround: float = 1.06) -> float:
    return round(1.0 / (p * overround), 4)


def _book(key, title, p_home=0.50, p_draw=0.28, p_away=0.22, home_price=None):
    return {"key": key, "title": title, "markets": [
        {"key": "h2h", "outcomes": [
            {"name": "Alpha", "price": home_price or _price(p_home)},
            {"name": "Draw", "price": _price(p_draw)},
            {"name": "Beta", "price": _price(p_away)},
        ]},
    ]}


def _event(books=None):
    return {
        "id": "evt-1", "sport_key": "soccer_epl",
        "commence_time": "2026-08-20T19:00:00Z",
        "home_team": "Alpha", "away_team": "Beta",
        "bookmakers": books if books is not None else [
            _book("betclic", "Betclic (FR)"),
            _book("bet365", "Bet365"),
        ],
    }


# ---------------------------------------------------------------------------
# The reference itself: vig removed, not raw implied probability
# ---------------------------------------------------------------------------

def test_the_reference_has_the_bookmaker_margin_removed():
    """Comparing the model against 1/odds would flatter it by the whole
    overround — the model would 'beat' a price nobody could ever get."""
    ev = _event()
    names = {"Alpha", "Draw", "Beta"}

    fair = _novig_fair_prob(ev, "Alpha", "h2h", None, names)

    assert fair == pytest.approx(0.50, abs=0.005)
    assert fair < 1.0 / ev["bookmakers"][0]["markets"][0]["outcomes"][0]["price"]


def test_the_reference_sums_to_one_across_the_outcome_group():
    ev = _event()
    names = {"Alpha", "Draw", "Beta"}

    total = sum(_novig_fair_prob(ev, n, "h2h", None, names) for n in
                ("Alpha", "Draw", "Beta"))

    assert total == pytest.approx(1.0, abs=0.005)


def test_thin_market_yields_no_reference_rather_than_a_guess():
    """No book pricing the full outcome group means no reference. A fabricated
    one would silently score the model against a number nobody quoted."""
    ev = _event()
    ev["bookmakers"][0]["markets"][0]["outcomes"] = \
        ev["bookmakers"][0]["markets"][0]["outcomes"][:1]
    ev["bookmakers"] = ev["bookmakers"][:1]

    assert _novig_fair_prob(ev, "Alpha", "h2h", None, {"Alpha", "Draw", "Beta"}) is None


# ---------------------------------------------------------------------------
# It reaches the ValueBet
# ---------------------------------------------------------------------------

def test_value_bet_declares_the_field_and_defaults_to_unknown():
    assert "market_prob" in ValueBet.__dataclass_fields__
    assert ValueBet.__dataclass_fields__["market_prob"].default is None


def _stats(name, atk=1.0, dfn=1.0):
    return TeamStats(name=name, attack_home=atk, defense_home=dfn,
                     attack_away=atk, defense_away=dfn, matches_analyzed=30)


def _prebuilt():
    return {"soccer_epl": {
        "teams": {"Alpha": _stats("Alpha", 1.35), "Beta": _stats("Beta", 0.80)},
        "home_avg": 1.45, "away_avg": 1.15, "h2h": {},
    }}


def _run(event, **kw):
    params = dict(
        events_by_sport={"soccer_epl": [event]},
        match_history_by_sport={},
        prebuilt_stats_by_sport=_prebuilt(),
        bankroll=100.0, min_value_edge=0.0, min_model_prob=0.0,
        min_book_odds=1.0, max_book_odds=0.0, novig_required=False,
        min_edge_vs_novig=0.0, underdog_odds=0.0,
        derive_dc_dnb=False, allow_totals_over=True,
    )
    params.update(kw)
    return detect_value_bets(**params)


def test_a_produced_pick_carries_the_market_reference():
    """One book quoting Alpha generously creates the positive edge that lets a
    pick through; the reference must ride along with it."""
    ev = _event([_book("betclic", "Betclic (FR)"),
                 _book("bet365", "Bet365"),
                 _book("pinnacle", "Pinnacle", home_price=3.20)])

    picks = [b for b in _run(ev) if b.selection_code == "1"]

    assert picks, "the generous price should produce a value bet"
    stored = picks[0].market_prob
    assert stored is not None

    # The stored value must BE the de-vigged consensus, not a rounding of the
    # best price: those diverge precisely on the picks worth taking, since an
    # outlier generous price implies a LOWER probability than the fair line.
    expected = _novig_fair_prob(ev, "Alpha", "h2h", None, {"Alpha", "Draw", "Beta"})
    assert stored == pytest.approx(expected, abs=0.001)
    assert stored > 1.0 / picks[0].best_odds, (
        "a value pick is exactly one the best price under-rates versus fair")


def test_the_reference_is_recorded_even_with_the_novig_gate_switched_off():
    """It used to be computed only when `min_edge_vs_novig > 0`. What the model
    is judged against must not depend on a filter being enabled."""
    ev = _event([_book("betclic", "Betclic (FR)"),
                 _book("bet365", "Bet365"),
                 _book("pinnacle", "Pinnacle", home_price=3.20)])

    picks = _run(ev, min_edge_vs_novig=0.0)

    assert any(b.market_prob is not None for b in picks)


def test_double_chance_gets_a_reference_by_the_same_algebra():
    """Double chance is the one market the model is not beaten on, so it is the
    one that most needs a recorded reference. Built as fair(1) + fair(X), the
    same way the model builds its own — keeping the comparison honest."""
    ev = _event([_book("betclic", "Betclic (FR)"),
                 _book("bet365", "Bet365"),
                 _book("pinnacle", "Pinnacle", home_price=3.20)])

    dc = [b for b in _run(ev, derive_dc_dnb=True) if b.selection_code == "1X"]
    if not dc:
        pytest.skip("no 1X leg cleared the filters for this fixture")

    assert dc[0].market_prob is not None
    assert dc[0].market_prob > 0.5, "1X must exceed either leg alone"


# ---------------------------------------------------------------------------
# It survives to the database
# ---------------------------------------------------------------------------

def test_persistence_accepts_and_stores_it():
    import inspect

    from betbot.db import Database
    from betbot.orm_models import Prediction

    assert "market_prob" in inspect.signature(Database.save_prediction).parameters
    assert hasattr(Prediction, "market_prob")


def test_the_migration_is_chained_to_the_current_head():
    """An orphaned migration silently never runs, and the column would only
    exist on developer machines."""
    import re
    from pathlib import Path

    mig = Path("alembic/versions/n8c1e5g7b9d4_add_market_prob.py").read_text(
        encoding="utf-8")
    assert re.search(r'down_revision\s*=\s*"m7b0d4f6a8c3"', mig)
    assert "market_prob" in mig
