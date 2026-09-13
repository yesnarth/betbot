"""
Bookmaker whitelist + auto-confirm placement status.

Context (audit 2026-08-01): the bot compared prices across 23 bookmakers and
kept the maximum, but the user only bets on Betclic and Bet365. On 310
production picks, 99.4% were priced on an operator they have no account with
— so `value_edge` was denominated in prices that could never be obtained.
"""
from __future__ import annotations

import pytest

from betbot.bookmaker_filter import allowed_bookmakers, is_allowed, whitelist_tokens
from betbot.models import extract_best_odds


def _event(*books: tuple[str, str, float]) -> dict:
    """Build a minimal Odds API event: (key, title, h2h price for 'Lyon')."""
    return {
        "bookmakers": [
            {
                "key": key,
                "title": title,
                "markets": [{"key": "h2h", "outcomes": [{"name": "Lyon", "price": price}]}],
            }
            for key, title, price in books
        ]
    }


@pytest.fixture
def whitelisted(monkeypatch):
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "betclic,bet365")
    return None


@pytest.fixture
def unfiltered(monkeypatch):
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "")
    return None


@pytest.mark.parametrize(
    "book,expected",
    [
        ({"key": "betclic", "title": "Betclic (FR)"}, True),
        ({"key": "bet365", "title": "Bet365"}, True),
        # 'Betfair' shares the 'bet' prefix with both tokens but must NOT match
        # — this is the operator that priced 26% of production picks.
        ({"key": "betfair_ex_eu", "title": "Betfair"}, False),
        ({"key": "matchbook", "title": "Matchbook"}, False),
        ({"key": "onexbet", "title": "1xBet"}, False),
        ({"key": "winamax_fr", "title": "Winamax (FR)"}, False),
        ({"key": "unibet_fr", "title": "Unibet (FR)"}, False),
        ({"key": "pinnacle", "title": "Pinnacle"}, False),
    ],
)
def test_is_allowed_matches_only_the_users_operators(whitelisted, book, expected):
    assert is_allowed(book) is expected


def test_empty_whitelist_allows_everything(unfiltered):
    assert whitelist_tokens() == ()
    assert is_allowed({"key": "betfair_ex_eu", "title": "Betfair"}) is True


def test_whitelist_is_reread_when_env_changes(monkeypatch):
    """The cache keys on the raw env string — a mid-process change must apply,
    otherwise tests and the dashboard would see a stale universe."""
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "betclic")
    assert is_allowed({"key": "bet365", "title": "Bet365"}) is False
    monkeypatch.setenv("BOOKMAKER_WHITELIST", "betclic,bet365")
    assert is_allowed({"key": "bet365", "title": "Bet365"}) is True


def test_best_odds_ignores_a_better_price_on_an_unreachable_book(whitelisted):
    """The core regression: Betfair's 2.60 is the best price on the market but
    the user cannot take it. The bot must quote Bet365's 2.35."""
    event = _event(
        ("betfair_ex_eu", "Betfair", 2.60),
        ("betclic", "Betclic (FR)", 2.30),
        ("bet365", "Bet365", 2.35),
    )
    best = extract_best_odds(event, "Lyon")
    assert best is not None
    assert best.price == pytest.approx(2.35)
    assert best.bookmaker == "Bet365"


def test_best_odds_without_whitelist_keeps_legacy_behaviour(unfiltered):
    event = _event(
        ("betfair_ex_eu", "Betfair", 2.60),
        ("bet365", "Bet365", 2.35),
    )
    best = extract_best_odds(event, "Lyon")
    assert best.price == pytest.approx(2.60)
    assert best.bookmaker == "Betfair"


def test_best_odds_returns_none_when_no_reachable_book_prices_the_outcome(whitelisted):
    """No pick at all is the correct outcome — better than a pick priced on a
    book the user cannot reach."""
    event = _event(("matchbook", "Matchbook", 3.00))
    assert extract_best_odds(event, "Lyon") is None


def test_phantom_edge_is_the_difference_between_the_two_books(whitelisted):
    """Quantifies why this matters: the same model probability yields a wildly
    different edge depending on which book is quoted. The gap here (+11.2 pts)
    is the same order as the +12.26% average edge the bot claimed in
    production while being 12.56 points overconfident."""
    model_prob = 0.45
    unreachable_edge = model_prob * 2.60 - 1.0
    reachable_edge = model_prob * 2.35 - 1.0
    assert unreachable_edge == pytest.approx(0.17, abs=1e-9)
    assert reachable_edge == pytest.approx(0.0575, abs=1e-9)
    assert unreachable_edge - reachable_edge > 0.10


def test_allowed_bookmakers_filters_the_event(whitelisted):
    event = _event(
        ("betfair_ex_eu", "Betfair", 2.60),
        ("bet365", "Bet365", 2.35),
        ("matchbook", "Matchbook", 2.55),
    )
    assert [b["title"] for b in allowed_bookmakers(event)] == ["Bet365"]


# ---------------------------------------------------------------------------
# Auto-confirm
# ---------------------------------------------------------------------------


def test_default_placement_status_is_proposed_without_the_flag(monkeypatch):
    monkeypatch.delenv("AUTO_CONFIRM_PICKS", raising=False)
    from betbot.db import default_placement_status

    assert default_placement_status() == "proposed"


def test_auto_confirm_makes_every_scanned_pick_count(monkeypatch):
    """The user places every scanned pick at their bookmaker, so a pick must
    enter the track record immediately instead of being archived to 'skipped'
    36h later and excluded from every statistic."""
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "1")
    from betbot.db import default_placement_status

    assert default_placement_status() == "confirmed"


def test_auto_confirm_off_by_explicit_zero(monkeypatch):
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "0")
    from betbot.db import default_placement_status

    assert default_placement_status() == "proposed"
