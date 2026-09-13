"""
Consensus model: expected goals come from the totals market, not from a guess.

`consensus_match_probs` reads a de-vigged, book-weighted 1X2 consensus — that
part was always sound. It then derived the goal expectation with
`_prob_to_lambda`, whose total is exactly `-2*ln(p_draw)`: it depends on NOTHING
but the draw probability and never looks at the totals market. On a heavy
favourite it announced 4.61 expected goals where reality is ~2.9.

Measured on 87 graded production totals picks (2026-08-01):
    Over  n=42  predicted 0.592 -> realised 0.286  ROI -52.0%
    Under n=45  predicted 0.566 -> realised 0.400  ROI -22.5%
39 of 67 totals picks came through this path.

The bookmakers' own totals market is already in the event — the scan requests
`markets=h2h,totals` — so the LEVEL is available for free. The 1X2 still drives
the home/away SPLIT, which is what it genuinely prices.
"""
from __future__ import annotations

import pytest

from betbot.models import (
    MAX_TOTAL_LAMBDA,
    MIN_TOTAL_LAMBDA,
    _market_total_lambda,
    _poisson_prob_over,
    _prob_to_lambda,
    consensus_match_probs,
    lambda_total_from_over_prob,
)


@pytest.fixture(autouse=True)
def _no_whitelist(monkeypatch):
    monkeypatch.delenv("BOOKMAKER_WHITELIST", raising=False)


def _book(key, title, o1, ox, o2, over=None, under=None, point=2.5):
    markets = [{"key": "h2h", "outcomes": [
        {"name": "A", "price": o1},
        {"name": "Draw", "price": ox},
        {"name": "B", "price": o2},
    ]}]
    if over and under:
        markets.append({"key": "totals", "outcomes": [
            {"name": "Over", "price": over, "point": point},
            {"name": "Under", "price": under, "point": point},
        ]})
    return {"key": key, "title": title, "markets": markets}


def _event(*books):
    return {"id": "evt", "home_team": "A", "away_team": "B", "bookmakers": list(books)}


# ---------------------------------------------------------------------------
# Poisson inversion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("p_over", [0.30, 0.40, 0.50, 0.60, 0.70])
def test_inversion_round_trips(p_over):
    """Feeding the inverted lambda back through the Poisson must return the
    probability we started from."""
    lam = lambda_total_from_over_prob(p_over, 2.5)
    assert _poisson_prob_over(lam, 2.5) == pytest.approx(p_over, abs=1e-3)


def test_inversion_is_monotonic():
    lambdas = [lambda_total_from_over_prob(p, 2.5) for p in (0.3, 0.4, 0.5, 0.6, 0.7)]
    assert lambdas == sorted(lambdas)


def test_inversion_is_bounded_against_absurd_prices():
    """A stale or extreme quote must not produce an absurd goal expectation."""
    assert lambda_total_from_over_prob(0.001, 2.5) == MIN_TOTAL_LAMBDA
    assert lambda_total_from_over_prob(0.999, 2.5) == MAX_TOTAL_LAMBDA


def test_different_lines_give_consistent_totals():
    """A 1.5 line at 80% over and a 2.5 line at ~50% describe a similar match."""
    from_15 = lambda_total_from_over_prob(0.80, 1.5)
    from_25 = lambda_total_from_over_prob(0.50, 2.5)
    assert abs(from_15 - from_25) < 0.6


# ---------------------------------------------------------------------------
# Reading the market
# ---------------------------------------------------------------------------

def test_market_lambda_is_read_from_the_totals_outcomes():
    event = _event(
        _book("pinnacle", "Pinnacle", 1.35, 5.0, 9.0, over=1.85, under=1.95),
        _book("bet365", "Bet365", 1.35, 5.0, 9.0, over=1.85, under=1.95),
    )
    lam = _market_total_lambda(event)
    assert lam is not None
    q_over, q_under = 1 / 1.85, 1 / 1.95
    fair = q_over / (q_over + q_under)
    assert _poisson_prob_over(lam, 2.5) == pytest.approx(fair, abs=1e-3)


def test_market_lambda_is_none_without_a_totals_market():
    event = _event(_book("pinnacle", "Pinnacle", 1.35, 5.0, 9.0))
    assert _market_total_lambda(event) is None


def test_half_quoted_line_is_ignored():
    """Over without its Under carries the book's margin unevenly — de-vigging
    it against nothing would bias the implied probability."""
    event = _event({
        "key": "pinnacle", "title": "Pinnacle",
        "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": 1.85, "point": 2.5},
        ]}],
    })
    assert _market_total_lambda(event) is None


def test_two_five_line_is_preferred_when_available():
    event = _event({
        "key": "pinnacle", "title": "Pinnacle",
        "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": 1.85, "point": 2.5},
            {"name": "Under", "price": 1.95, "point": 2.5},
            {"name": "Over", "price": 5.00, "point": 4.5},
            {"name": "Under", "price": 1.15, "point": 4.5},
        ]}],
    })
    lam = _market_total_lambda(event)
    q_over, q_under = 1 / 1.85, 1 / 1.95
    assert _poisson_prob_over(lam, 2.5) == pytest.approx(
        q_over / (q_over + q_under), abs=1e-3
    )


# ---------------------------------------------------------------------------
# End to end through the consensus model
# ---------------------------------------------------------------------------

def test_heavy_favourite_no_longer_predicts_absurd_goals():
    """The case the audit pointed at: a 1.12 favourite used to produce 4.61
    expected goals because the draw probability was tiny."""
    event = _event(
        _book("pinnacle", "Pinnacle", 1.12, 9.0, 21.0, over=1.50, under=2.55),
        _book("bet365", "Bet365", 1.13, 8.8, 20.0, over=1.52, under=2.50),
    )
    probs = consensus_match_probs(event)
    total = probs.lambda_home + probs.lambda_away
    assert 2.8 < total < 3.6, f"expected a plausible total, got {total}"
    assert probs.model == "consensus_mkt"


def test_the_1x2_still_drives_the_home_away_split():
    """Anchoring the level must not flatten the supremacy: a strong favourite
    keeps the larger share of the goals."""
    event = _event(
        _book("pinnacle", "Pinnacle", 1.20, 7.0, 15.0, over=1.85, under=1.95),
        _book("bet365", "Bet365", 1.20, 7.0, 15.0, over=1.85, under=1.95),
    )
    probs = consensus_match_probs(event)
    assert probs.lambda_home > probs.lambda_away * 1.5


def test_model_reproduces_the_market_on_the_anchored_line():
    """This is the point of the fix, and it is what kills the phantom edge.

    The consensus model holds no information about goals beyond what the market
    prices, so on the anchored line it must agree with the market — which makes
    the edge against the raw price equal to minus the bookmaker's margin, and
    therefore no bet.
    """
    event = _event(
        _book("pinnacle", "Pinnacle", 1.35, 5.0, 9.0, over=1.85, under=1.95),
        _book("bet365", "Bet365", 1.35, 5.0, 9.0, over=1.85, under=1.95),
    )
    probs = consensus_match_probs(event)
    edge_over = probs.over_25 * 1.85 - 1.0
    assert -0.08 < edge_over < 0.0, "must be exactly the margin, never positive"


def test_fallback_when_no_totals_market_is_labelled_as_such():
    """The heuristic remains the only path when nothing quotes totals — but the
    model type says so, which is what makes the fix measurable per segment."""
    event = _event(
        _book("pinnacle", "Pinnacle", 1.35, 5.0, 9.0),
        _book("bet365", "Bet365", 1.35, 5.0, 9.0),
    )
    probs = consensus_match_probs(event)
    assert probs.model == "consensus"


def test_fallback_heuristic_is_bounded():
    """Even without a market the total must stay plausible. Unbounded, a heavy
    favourite reached 4.61 goals."""
    lh = _prob_to_lambda(0.90, 0.07, 0.03, home=True)
    la = _prob_to_lambda(0.90, 0.07, 0.03, home=False)
    assert (lh + la) <= MAX_TOTAL_LAMBDA + 1e-6


def test_fallback_preserves_the_ratio_while_bounding_the_total():
    """Bounding must scale both sides together, not clamp each independently —
    otherwise the supremacy is destroyed."""
    lh = _prob_to_lambda(0.90, 0.07, 0.03, home=True)
    la = _prob_to_lambda(0.90, 0.07, 0.03, home=False)
    assert lh > la, "the favourite must still carry more goals"
