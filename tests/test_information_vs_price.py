"""
Where you BET is not what the model is allowed to KNOW.

`BOOKMAKER_WHITELIST` answers one question: which price can the owner actually
strike a bet at. It must never answer a second one: how much does the market
know about this match. Every bookmaker's line is evidence about the fixture
whether or not you hold an account there.

The two were conflated in `_market_total_lambda`, which estimates the expected
goals of a match — pure information — and filtered by the whitelist. Once the
whitelist narrowed to a single bookmaker it was reading the goals level off one
opinion, having discarded Pinnacle, the very book BOOK_WEIGHTS weights highest
because it is the sharpest reference available.
"""
from __future__ import annotations

import pytest

from betbot.models import _market_total_lambda, consensus_match_probs, extract_best_odds


def _totals_book(key, title, over_price, under_price, line=2.5):
    return {"key": key, "title": title, "markets": [
        {"key": "totals", "outcomes": [
            {"name": "Over", "price": over_price, "point": line},
            {"name": "Under", "price": under_price, "point": line},
        ]},
    ]}


def _h2h_book(key, title, o1=2.10, ox=3.40, o2=3.60):
    return {"key": key, "title": title, "markets": [
        {"key": "h2h", "outcomes": [
            {"name": "Alpha", "price": o1},
            {"name": "Draw", "price": ox},
            {"name": "Beta", "price": o2},
        ]},
    ]}


def _event(bookmakers):
    return {"id": "e1", "home_team": "Alpha", "away_team": "Beta",
            "bookmakers": bookmakers}


@pytest.fixture
def _betclic_only(monkeypatch):
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "betclic")


# ---------------------------------------------------------------------------
# Information must reach the model whatever the whitelist says
# ---------------------------------------------------------------------------

def test_the_goals_estimate_reads_every_book(_betclic_only):
    """Betclic alone prices this match high-scoring; the rest of the market
    disagrees. The estimate must reflect the market, not the one account."""
    outlier = [_totals_book("betclic", "Betclic (FR)", 1.35, 3.20)]
    full = outlier + [
        _totals_book("pinnacle", "Pinnacle", 2.05, 1.80),
        _totals_book("unibet", "Unibet", 2.10, 1.78),
        _totals_book("winamax", "Winamax (FR)", 2.08, 1.79),
    ]

    lonely = _market_total_lambda(_event(outlier))
    informed = _market_total_lambda(_event(full))

    assert lonely is not None and informed is not None
    assert informed < lonely, (
        "the wider market must pull the goals estimate off the single book")


def test_the_1x2_consensus_already_reads_every_book(_betclic_only):
    """This one was always right — the fix made the goals path consistent
    with it, not the other way round."""
    books = [_h2h_book("betclic", "Betclic (FR)", o1=1.50),
             _h2h_book("pinnacle", "Pinnacle", o1=2.40),
             _h2h_book("unibet", "Unibet", o1=2.35)]

    probs = consensus_match_probs(_event(books))

    assert probs is not None
    assert probs.home_win < 0.55, "one aggressive book must not set the price"


def test_the_market_reference_reads_every_book(_betclic_only):
    from betbot.analysis import _novig_fair_prob

    books = [_h2h_book("betclic", "Betclic (FR)"),
             _h2h_book("pinnacle", "Pinnacle"),
             _h2h_book("unibet", "Unibet")]

    fair = _novig_fair_prob(_event(books), "Alpha", "h2h", None,
                            {"Alpha", "Draw", "Beta"})

    assert fair is not None and 0.0 < fair < 1.0


# ---------------------------------------------------------------------------
# The price you strike the bet at must stay inside the whitelist
# ---------------------------------------------------------------------------

def test_the_quoted_price_is_the_one_you_can_actually_get(_betclic_only):
    """A generous price at a book with no account is not an opportunity, it is
    a mirage — and betting the model against it overstates every edge."""
    books = [_h2h_book("betclic", "Betclic (FR)", o1=1.90),
             _h2h_book("pinnacle", "Pinnacle", o1=2.60)]

    best = extract_best_odds(_event(books), "Alpha", market_key="h2h")

    assert best is not None
    assert best.bookmaker == "Betclic (FR)"
    assert best.price == 1.90


def test_derived_prices_inherit_the_whitelist(_betclic_only):
    """A derived Double Chance is only as real as the 1X2 triplet it is built
    from."""
    from betbot.analysis import _derive_dc_dnb_odds

    books = [_h2h_book("betclic", "Betclic (FR)", o1=1.90),
             _h2h_book("pinnacle", "Pinnacle", o1=2.60)]

    prices, _ = _derive_dc_dnb_odds(_event(books), "Alpha", "Beta")

    for best in prices.values():
        assert best.bookmaker == "Betclic (FR)"


# ---------------------------------------------------------------------------
# The rule, stated once
# ---------------------------------------------------------------------------

def test_no_information_path_filters_by_bookmaker():
    """A guard against the conflation coming back: functions that estimate what
    the market thinks must not consult the whitelist."""
    import inspect

    from betbot import models

    for fn in (models._market_total_lambda, models.consensus_match_probs):
        src = inspect.getsource(fn)
        assert "is_allowed" not in src, (
            f"{fn.__name__} estimates information — it must read every book")
