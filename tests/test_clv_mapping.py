"""
CLV must price the RIGHT outcome, in the RIGHT market, against the WHOLE market.

Three defects, all live at the moment CLV was enabled:

1. `_outcome_name` returned `away_team` for anything that was not "1" or "X".
   A totals pick therefore asked for the away team's price.
2. `extract_best_odds` was called without `market_key`, so it looked in `h2h`
   even for a totals selection.
3. The closing reference used the bookmaker whitelist, i.e. Betclic/Bet365 —
   which measures how your own book drifted, not closing line value. A bettor
   taking favourites early would show a positive CLV with no skill at all.

Combined, 264 of 297 production picks (88.9%) would have been stamped with an
unrelated price. Not left empty — filled with signed noise that `get_roi_stats`
averages into a CLV figure with no sanity check.

And `update_result` overwrote `closing_odds` unconditionally while all four
resolver call sites omit the argument, so any snapshot was erased at settlement.
"""
from __future__ import annotations

import pytest

from betbot.clv import _closing_lookup
from betbot.models import extract_best_odds


class _Pred:
    """Minimal stand-in for the ORM row — `_closing_lookup` reads 3 fields."""

    def __init__(self, selection, home="Lyon", away="Nice"):
        self.selection = selection
        self.home_team = home
        self.away_team = away


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("1", ("Lyon", "h2h", None)),
    ("X", ("Draw", "h2h", None)),
    ("2", ("Nice", "h2h", None)),
    ("O25", ("Over", "totals", 2.5)),
    ("U25", ("Under", "totals", 2.5)),
    ("O05", ("Over", "totals", 0.5)),
    ("U35", ("Under", "totals", 3.5)),
])
def test_known_selections_map_to_the_right_market(code, expected):
    assert _closing_lookup(_Pred(code)) == expected


@pytest.mark.parametrize("code", ["1X", "X2", "12", "DNB1", "DNB2"])
def test_derived_selections_are_unpriceable(code):
    """They have no quoted outcome of their own — they were computed from the
    1X2. Returning None keeps closing_odds NULL, which is the honest answer.
    Stamping them with a 1X2 price would produce a meaningless CLV."""
    assert _closing_lookup(_Pred(code)) is None


def test_totals_no_longer_resolve_to_a_team_name():
    """The exact regression: O25 used to return the away team."""
    lookup = _closing_lookup(_Pred("O25"))
    assert lookup is not None
    outcome_name, market_key, _ = lookup
    assert outcome_name == "Over"
    assert market_key == "totals"
    assert outcome_name not in ("Lyon", "Nice")


# ---------------------------------------------------------------------------
# The closing reference must span the whole market
# ---------------------------------------------------------------------------

def _event():
    def book(key, title, price):
        return {"key": key, "title": title, "markets": [
            {"key": "h2h", "outcomes": [{"name": "Lyon", "price": price}]},
        ]}
    return {"bookmakers": [
        book("betclic", "Betclic (FR)", 2.10),
        book("pinnacle", "Pinnacle", 2.35),   # sharpest price, outside the whitelist
    ]}


def test_selection_honours_the_whitelist(monkeypatch):
    """Bet selection must stay on books the user can actually reach."""
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "betclic,bet365")
    best = extract_best_odds(_event(), "Lyon")
    assert best.bookmaker == "Betclic (FR)"


def test_clv_reference_ignores_the_whitelist(monkeypatch):
    """CLV compares the entry price to where THE MARKET closed. Measuring
    against your own book would turn 'I took a favourite early' into apparent
    skill."""
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "betclic,bet365")
    best = extract_best_odds(_event(), "Lyon", ignore_whitelist=True)
    assert best.bookmaker == "Pinnacle"
    assert best.price == pytest.approx(2.35)


def test_totals_lookup_reaches_the_totals_market(monkeypatch):
    monkeypatch.delenv("BOOKMAKER_WHITELIST", raising=False)
    event = {"bookmakers": [{"key": "pinnacle", "title": "Pinnacle", "markets": [
        {"key": "h2h", "outcomes": [{"name": "Lyon", "price": 2.10}]},
        {"key": "totals", "outcomes": [
            {"name": "Over", "price": 1.88, "point": 2.5},
            {"name": "Under", "price": 1.92, "point": 2.5},
        ]},
    ]}]}
    outcome_name, market_key, point = _closing_lookup(_Pred("O25"))
    best = extract_best_odds(event, outcome_name, market_key=market_key, point=point)
    assert best is not None
    assert best.price == pytest.approx(1.88), "must read the totals market, not h2h"
