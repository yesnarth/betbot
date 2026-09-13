"""
AUTO_CONFIRM_PICKS=1 must never move the bankroll.

⚠ DESTRUCTIVE: an autouse fixture wipes the ledger and predictions between
tests. Safety gate is enforced by tests/e2e/conftest.py.

Context (audit 2026-08-01): the user places every scanned pick manually at
Betclic/Bet365, so picks are born 'confirmed' to enter the track record. But
the amount they stake is variable and unknown to the bot, so `save_prediction`
must not write a `bet_placed` debit.

That creates a trap: `update_result` used to credit winnings for any pick with
`placement_status == 'confirmed'`. Under auto-confirm, every winning pick would
be paid out against a stake that was never deducted — the balance would grow
without bound. The guard is now keyed on the presence of a real 'bet_placed'
ledger entry instead.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _reset_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.getenv("BETBOT_TEST_DATABASE_URL", ""))
    from betbot.database import session_scope, reset_engine
    from betbot.orm_models import BankrollEntry, Prediction
    reset_engine()
    with session_scope() as s:
        s.query(BankrollEntry).delete()
        s.query(Prediction).delete()
    yield
    reset_engine()


def _balance() -> float:
    from betbot.database import session_scope
    from betbot.orm_models import BankrollEntry
    with session_scope() as s:
        return sum(e.amount for e in s.query(BankrollEntry).all())


def _seed(db, **overrides) -> None:
    payload = {
        "event_id": "evt-auto-1",
        "sport_key": "soccer_epl",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "market": "h2h",
        "selection": "1",
        "model_prob": 0.55,
        "best_odds": 2.10,
        "best_book": "Bet365",
        "value_edge": 0.155,
        "kelly_stake": 5.0,
        "model_type": "blended",
    }
    payload.update(overrides)
    assert db.save_prediction(**payload) is True


def test_auto_confirmed_pick_is_born_confirmed(monkeypatch):
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "1")
    from betbot.db import Database

    db = Database()
    _seed(db)
    rows = db.get_confirmed_pending()
    assert len(rows) == 1
    assert rows[0]["placement_status"] == "confirmed"


def test_auto_confirmed_pick_does_not_debit_the_bankroll(monkeypatch):
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "1")
    from betbot.db import Database

    db = Database()
    before = _balance()
    _seed(db)
    assert _balance() == pytest.approx(before)


def test_auto_confirmed_win_does_not_credit_phantom_winnings(monkeypatch):
    """The regression this guard exists for: without a 'bet_placed' debit,
    a win must not pay out. Otherwise every auto-confirmed winner inflates
    the balance by stake × odds out of thin air."""
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "1")
    from betbot.db import Database

    db = Database()
    _seed(db)
    before = _balance()

    db.update_result("evt-auto-1", "h2h", "1", "win")

    assert _balance() == pytest.approx(before), (
        "an auto-confirmed pick that never debited the bankroll must not "
        "credit it on a win"
    )


def test_auto_confirmed_win_still_records_the_result_for_statistics(monkeypatch):
    """No money moves, but the pick MUST enter the track record — that is the
    entire point of auto-confirming."""
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "1")
    from betbot.db import Database

    db = Database()
    _seed(db)
    db.update_result("evt-auto-1", "h2h", "1", "win")

    stats = db.get_roi_stats(days=3650, only_placed=True)
    assert stats["n_bets"] >= 1, "auto-confirmed picks must count in ROI stats"
    assert stats["n_wins"] >= 1


def test_explicitly_staked_pick_still_credits_normally(monkeypatch):
    """Backward compatibility: a pick the user confirms through the dashboard
    writes a real 'bet_placed' debit, so the payout path must still fire."""
    monkeypatch.setenv("AUTO_CONFIRM_PICKS", "0")
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database

    db = Database()
    bootstrap_initial_deposit(100.0)
    _seed(db)

    pred = db.get_proposed_predictions()[0]
    assert db.confirm_prediction_placed(pred["id"], bookmaker="Bet365") is True

    after_placement = _balance()
    assert after_placement == pytest.approx(95.0), "stake of 5.0 must be debited"

    db.update_result("evt-auto-1", "h2h", "1", "win")

    # payout = stake × odds = 5.0 × 2.10 = 10.50
    assert _balance() == pytest.approx(105.50)
