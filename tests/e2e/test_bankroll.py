"""
Unit tests for the bankroll module.

⚠ DESTRUCTIVE: an autouse fixture wipes the ledger and predictions tables
between tests. Safety gate enforced by tests/e2e/conftest.py.
"""
import os
import pytest

from betbot.bankroll import (
    InsufficientFundsError,
    adjustment,
    bootstrap_initial_deposit,
    deposit,
    get_state,
    record_bet_lost,
    record_bet_placed,
    record_bet_won,
    withdraw,
)


@pytest.fixture(autouse=True)
def _reset_ledger(monkeypatch):
    """Wipe the ledger AND test-only predictions before each test. Only runs
    against the explicitly-opted-in test DB (see pytestmark above)."""
    monkeypatch.setenv("DATABASE_URL", os.getenv("BETBOT_TEST_DATABASE_URL", ""))
    from betbot.database import session_scope, reset_engine
    from betbot.orm_models import BankrollEntry, Prediction
    reset_engine()
    with session_scope() as s:
        s.query(BankrollEntry).delete()
        s.query(Prediction).delete()
    yield
    reset_engine()


def test_empty_state():
    s = get_state()
    assert s.balance == 0.0
    assert s.committed == 0.0
    assert s.available == 0.0
    assert s.n_entries == 0


def test_deposit_increases_balance():
    deposit(100.0, note="seed")
    s = get_state()
    assert s.balance == 100.0
    assert s.available == 100.0
    assert s.total_deposits == 100.0
    assert s.n_entries == 1


def test_withdraw_decreases_balance():
    deposit(100.0)
    withdraw(30.0)
    s = get_state()
    assert s.balance == 70.0
    assert s.total_withdrawals == 30.0


def test_withdraw_insufficient_funds_raises():
    deposit(20.0)
    with pytest.raises(InsufficientFundsError):
        withdraw(50.0)


def test_bet_placed_immobilizes_capital():
    """A placed bet decreases the balance by the stake AND counts as committed."""
    deposit(100.0)
    # We need a real prediction_id — insert one quickly via session
    from betbot.database import session_scope
    from betbot.orm_models import Prediction
    with session_scope() as ss:
        p = Prediction(
            created_at="2026-05-06T00:00:00", event_id="evt", sport_key="soccer_epl",
            home_team="A", away_team="B", market="h2h", selection="1",
            model_prob=0.5, best_odds=2.0, best_book="x",
            value_edge=0.1, kelly_stake=10.0, model_type="poisson",
        )
        ss.add(p)
        ss.flush()
        pid = p.id
    record_bet_placed(pid, 10.0)
    s = get_state()
    assert s.balance == 90.0      # stake already debited from cash
    assert s.committed == 10.0    # tracked separately for reporting
    assert s.available == 90.0    # available == balance (no double-count)


def test_bet_won_credits_full_payout():
    deposit(100.0)
    from betbot.database import session_scope
    from betbot.orm_models import Prediction
    with session_scope() as ss:
        p = Prediction(
            created_at="2026-05-06T00:00:00", event_id="evt2", sport_key="soccer_epl",
            home_team="A", away_team="B", market="h2h", selection="1",
            model_prob=0.5, best_odds=2.0, best_book="x",
            value_edge=0.1, kelly_stake=10.0, model_type="poisson",
        )
        ss.add(p)
        ss.flush()
        pid = p.id
    record_bet_placed(pid, 10.0)   # balance = 90
    record_bet_won(pid, stake=10.0, odds=2.0)   # +20 (full return)
    s = get_state()
    assert s.balance == 110.0      # 100 - 10 + 20 = 110
    assert s.total_won == 20.0


def test_bet_lost_does_not_double_debit():
    deposit(100.0)
    from betbot.database import session_scope
    from betbot.orm_models import Prediction
    with session_scope() as ss:
        p = Prediction(
            created_at="2026-05-06T00:00:00", event_id="evt3", sport_key="soccer_epl",
            home_team="A", away_team="B", market="h2h", selection="1",
            model_prob=0.5, best_odds=2.0, best_book="x",
            value_edge=0.1, kelly_stake=10.0, model_type="poisson",
        )
        ss.add(p)
        ss.flush()
        pid = p.id
    record_bet_placed(pid, 10.0)  # balance = 90
    record_bet_lost(pid)           # balance UNCHANGED (stake already debited)
    s = get_state()
    assert s.balance == 90.0
    assert s.n_entries == 3        # deposit + bet_placed + bet_lost (audit row)


def test_bet_placed_refuses_when_insufficient():
    deposit(5.0)  # only 5$ available
    from betbot.database import session_scope
    from betbot.orm_models import Prediction
    with session_scope() as ss:
        p = Prediction(
            created_at="2026-05-06T00:00:00", event_id="evt4", sport_key="soccer_epl",
            home_team="A", away_team="B", market="h2h", selection="1",
            model_prob=0.5, best_odds=2.0, best_book="x",
            value_edge=0.1, kelly_stake=10.0, model_type="poisson",
        )
        ss.add(p)
        ss.flush()
        pid = p.id
    with pytest.raises(InsufficientFundsError):
        record_bet_placed(pid, 10.0)


def test_bootstrap_idempotent():
    assert bootstrap_initial_deposit(100.0) is True
    # Second call must NOT add a second deposit
    assert bootstrap_initial_deposit(100.0) is False
    s = get_state()
    assert s.balance == 100.0


def test_adjustment_requires_note():
    deposit(100.0)
    with pytest.raises(ValueError):
        adjustment(-5.0, note="")
    adjustment(-5.0, note="penalty for late bet")
    s = get_state()
    assert s.balance == 95.0
