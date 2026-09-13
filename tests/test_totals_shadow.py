"""
Totals in OBSERVATION: rebuild the evidence without risking a stake.

Overs were cut on a -52% ROI measured while the goals model was broken —
`_prob_to_lambda` invented 4.61 expected goals on a favourite against ~2.9
real, so the model over-bet Overs by construction. That bug is fixed, which
makes the verdict stale. Worse, production produced ZERO totals picks after
2026-08-01: the residual contingent built to keep the cut falsifiable was
itself blocked upstream, so the decision could no longer be tested at all.

The fix is not to re-open the tap. It is to record and grade totals picks
without ever recommending them, until the evidence exists.
"""
from __future__ import annotations

import inspect

import pytest

from betbot.analysis import ValueBet, detect_value_bets
from betbot.models import TeamStats


def _price(p: float, overround: float = 1.06) -> float:
    return round(1.0 / (p * overround), 4)


def _event(over_price=2.10):
    """A match quoting both 1X2 and the 2.5 goals line."""
    return {
        "id": "evt-1", "sport_key": "soccer_epl",
        "commence_time": "2026-08-20T19:00:00Z",
        "home_team": "Alpha", "away_team": "Beta",
        "bookmakers": [
            {"key": k, "title": t, "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Alpha", "price": _price(0.50)},
                    {"name": "Draw", "price": _price(0.28)},
                    {"name": "Beta", "price": _price(0.22)},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": over_price, "point": 2.5},
                    {"name": "Under", "price": _price(0.48), "point": 2.5},
                ]},
            ]}
            for k, t in (("betclic", "Betclic (FR)"), ("bet365", "Bet365"))
        ],
    }


def _stats(name, atk):
    return TeamStats(name=name, attack_home=atk, defense_home=1.0,
                     attack_away=atk, defense_away=1.0, matches_analyzed=30)


def _poisson_stats():
    """Tuned so Over 2.5 lands at ~0.57 — the production average is 0.574.
    That is the whole point: it clears a 0.55 totals floor and fails a 0.70
    one, which is why the 1X2 floor silenced this market instead of guarding
    it."""
    return {"soccer_epl": {
        "teams": {"Alpha": _stats("Alpha", 1.10), "Beta": _stats("Beta", 1.10)},
        "home_avg": 1.50, "away_avg": 1.20, "h2h": {},
    }}


def _run(event=None, prebuilt=None, **kw):
    params = dict(
        events_by_sport={"soccer_epl": [event or _event()]},
        match_history_by_sport={},
        prebuilt_stats_by_sport=prebuilt if prebuilt is not None else _poisson_stats(),
        bankroll=100.0, kelly_fraction=0.25,
        min_value_edge=0.0, min_model_prob=0.70, min_model_prob_totals=0.55,
        min_book_odds=1.0, max_book_odds=0.0, novig_required=False,
        min_edge_vs_novig=0.0, underdog_odds=0.0,
        derive_dc_dnb=False, allow_totals_over=True,
    )
    params.update(kw)
    return detect_value_bets(**params)


# ---------------------------------------------------------------------------
# The floor that silenced the market
# ---------------------------------------------------------------------------

def test_totals_have_their_own_floor():
    assert "min_model_prob_totals" in inspect.signature(detect_value_bets).parameters


def test_the_1x2_floor_would_erase_totals_entirely():
    """Measured on production: Over 2.5 averages 0.574, so a 0.70 floor removes
    the market instead of protecting it — and with it, any chance of ever
    re-testing the cut."""
    with_own_floor = [b for b in _run() if b.market == "totals"]
    with_1x2_floor = [b for b in _run(min_model_prob_totals=0.70)
                      if b.market == "totals"]

    assert with_own_floor, "a lower, market-appropriate floor lets probes through"
    assert all(0.55 <= b.model_prob < 0.70 for b in with_own_floor), (
        "fixture must sit between the two floors — that is the whole point")
    assert not with_1x2_floor, "the 1X2 floor erases the market entirely"


def test_the_totals_floor_never_loosens_the_1x2_discipline():
    """A 0.55 totals probe must not become a 0.55 licence on match winners —
    that floor is what produces the 77% hit rate."""
    for bet in _run():
        if bet.market != "totals":
            assert bet.model_prob >= 0.70


# ---------------------------------------------------------------------------
# Consensus has nothing to say about goals
# ---------------------------------------------------------------------------

def test_consensus_leagues_produce_no_totals_at_all():
    """Since `_prob_to_lambda` was fixed the consensus reads the goals level
    FROM the totals market, so it reproduces the price. 71 of 97 historical
    totals picks came from it, and that is where the losses came from."""
    picks = _run(prebuilt={})   # no team stats -> consensus model

    assert not [b for b in picks if b.market == "totals"]


# ---------------------------------------------------------------------------
# Recorded and graded, never recommended
# ---------------------------------------------------------------------------

def test_totals_picks_are_flagged_as_observation():
    totals = [b for b in _run() if b.market == "totals"]

    assert totals, "fixture should yield at least one totals probe"
    assert all(b.shadow for b in totals)


def test_match_winner_picks_are_never_flagged():
    for bet in _run():
        if bet.market != "totals":
            assert bet.shadow is False


def test_value_bet_defaults_to_not_shadow():
    assert ValueBet.__dataclass_fields__["shadow"].default is False


def test_a_shadow_pick_is_born_proposed_even_under_auto_confirm(monkeypatch):
    """AUTO_CONFIRM_PICKS=1 makes every pick count as a placed bet. A
    measurement must escape that, or the probe would quietly enter the ROI
    and the user's track record."""
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "1")
    from betbot import db as db_mod

    captured = {}

    class _Row:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(db_mod, "Prediction", _Row)
    assert db_mod.default_placement_status() == "confirmed"

    sig = inspect.signature(db_mod.Database.save_prediction)
    assert "shadow" in sig.parameters
    assert sig.parameters["shadow"].default is False


def test_the_email_only_carries_placeable_picks():
    """The email is the "place these" channel. A 0.55-confidence probe sitting
    next to a 0.70 conviction pick invites exactly the stake this design
    exists to prevent."""
    from pathlib import Path

    src = Path("betbot/main.py").read_text(encoding="utf-8")
    assert "_to_recommend = [b for b in ranked_bets if not b.shadow]" in src
    assert "render_html(_to_recommend" in src


def test_sampling_still_bounds_the_probe_volume():
    """Deterministic on (event id, direction): a rescan of the same match must
    reach the same verdict, or repeated scans would accumulate duplicates on
    the lucky draws."""
    from betbot.analysis import _keep_totals_sample

    first = [_keep_totals_sample(f"evt-{i}", "Over") for i in range(50)]
    again = [_keep_totals_sample(f"evt-{i}", "Over") for i in range(50)]

    assert first == again
    assert 0 < sum(first) < 50, "a contingent, neither everything nor nothing"
