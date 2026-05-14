"""
Tests for the placement_status lifecycle (proposed → confirmed → skipped).

⚠ DESTRUCTIVE: an autouse fixture wipes the ledger and predictions between
tests. Safety gate is enforced by tests/e2e/conftest.py.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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


def _seed_proposed_pick(db, **overrides):
    """Helper: insert a proposed prediction and return its id."""
    payload = {
        "event_id": "evt-001",
        "sport_key": "soccer_epl",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "market": "h2h",
        "selection": "1",
        "model_prob": 0.55,
        "best_odds": 2.10,
        "best_book": "pinnacle",
        "value_edge": 0.155,
        "kelly_stake": 5.0,
        "model_type": "blended",
    }
    payload.update(overrides)
    ok = db.save_prediction(**payload)
    assert ok is True
    # Find the row to get its id
    from betbot.database import session_scope
    from betbot.orm_models import Prediction
    from sqlalchemy import select
    with session_scope() as s:
        row = s.execute(
            select(Prediction).where(Prediction.event_id == payload["event_id"])
        ).scalar_one()
        return row.id


# ---------------------------------------------------------------------------
# save_prediction lands as 'proposed', no bankroll debit
# ---------------------------------------------------------------------------

def test_save_prediction_does_not_debit_bankroll():
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    s = get_state()
    assert s.balance == 100.0

    db = Database(load_settings().database_url)
    _seed_proposed_pick(db, kelly_stake=10.0)

    # Bankroll untouched
    s2 = get_state()
    assert s2.balance == 100.0
    assert s2.committed == 0.0


# ---------------------------------------------------------------------------
# confirm_prediction_placed debits + flips to 'confirmed'
# ---------------------------------------------------------------------------

def test_confirm_debits_bankroll_atomically():
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, kelly_stake=10.0)

    ok = db.confirm_prediction_placed(pid, bookmaker="pinnacle")
    assert ok is True

    s = get_state()
    assert s.balance == 90.0   # 100 - 10 stake
    assert s.committed == 10.0


def test_confirm_is_idempotent():
    """Re-confirming a confirmed pick must not double-debit."""
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, kelly_stake=10.0)

    db.confirm_prediction_placed(pid, bookmaker="pinnacle")
    db.confirm_prediction_placed(pid, bookmaker="pinnacle")  # second call

    assert get_state().balance == 90.0  # single debit


def test_unconfirm_refunds_and_reverts_status():
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, kelly_stake=10.0)

    db.confirm_prediction_placed(pid, bookmaker="pinnacle")
    assert get_state().balance == 90.0

    db.confirm_prediction_placed(pid, unconfirm=True)
    # Adjustment refunds the stake
    assert get_state().balance == 100.0


# ---------------------------------------------------------------------------
# skip / unskip
# ---------------------------------------------------------------------------

def test_skip_does_not_touch_bankroll():
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, kelly_stake=10.0)

    ok = db.skip_prediction(pid)
    assert ok is True
    assert get_state().balance == 100.0


def test_skip_is_idempotent():
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db)

    db.skip_prediction(pid)
    db.skip_prediction(pid)  # second call should not raise


def test_cannot_skip_confirmed():
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, kelly_stake=5.0)
    db.confirm_prediction_placed(pid)

    with pytest.raises(ValueError, match="confirmed"):
        db.skip_prediction(pid)


def test_unskip_reverts_to_proposed():
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db)

    db.skip_prediction(pid)
    proposed_before = db.get_proposed_predictions()
    assert len(proposed_before) == 0  # skipped, no longer proposed

    ok = db.unskip_prediction(pid)
    assert ok is True

    proposed_after = db.get_proposed_predictions()
    assert len(proposed_after) == 1
    assert proposed_after[0]["id"] == pid


def test_cannot_unskip_confirmed():
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, kelly_stake=5.0)
    db.confirm_prediction_placed(pid)

    with pytest.raises(ValueError):
        db.unskip_prediction(pid)


# ---------------------------------------------------------------------------
# Queries (proposed / skipped)
# ---------------------------------------------------------------------------

def test_get_proposed_predictions_only_returns_proposed():
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid_a = _seed_proposed_pick(db, event_id="A", home_team="A")
    pid_b = _seed_proposed_pick(db, event_id="B", home_team="B", kelly_stake=5.0)
    pid_c = _seed_proposed_pick(db, event_id="C", home_team="C")

    db.confirm_prediction_placed(pid_b)  # B becomes confirmed
    db.skip_prediction(pid_c)             # C becomes skipped

    proposed = db.get_proposed_predictions()
    assert len(proposed) == 1
    assert proposed[0]["id"] == pid_a


def test_get_skipped_predictions_returns_recent_skipped():
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid_a = _seed_proposed_pick(db, event_id="A", home_team="A")
    pid_b = _seed_proposed_pick(db, event_id="B", home_team="B")

    db.skip_prediction(pid_a)
    db.skip_prediction(pid_b)

    skipped = db.get_skipped_predictions(limit=5)
    assert len(skipped) == 2
    # Most-recently-skipped first
    assert skipped[0]["id"] == pid_b


# ---------------------------------------------------------------------------
# auto_skip_expired_proposed
# ---------------------------------------------------------------------------

def test_auto_skip_marks_old_proposed_as_skipped():
    """Proposed picks older than max_age_hours should be auto-skipped."""
    from betbot.bankroll import bootstrap_initial_deposit
    from betbot.database import session_scope
    from betbot.db import Database
    from betbot.orm_models import Prediction
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid_old = _seed_proposed_pick(db, event_id="OLD", home_team="Old")
    pid_new = _seed_proposed_pick(db, event_id="NEW", home_team="New")

    # Manually back-date the OLD pick beyond 36h
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with session_scope() as s:
        old_row = s.get(Prediction, pid_old)
        old_row.created_at = long_ago

    n = db.auto_skip_expired_proposed(max_age_hours=36)
    assert n == 1

    proposed_now = db.get_proposed_predictions()
    assert len(proposed_now) == 1
    assert proposed_now[0]["id"] == pid_new


# ---------------------------------------------------------------------------
# Resolver only affects confirmed
# ---------------------------------------------------------------------------

def test_resolver_does_not_credit_skipped_pick():
    """A skipped pick whose match resolves as WIN should NOT credit the bankroll."""
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, event_id="SKP", kelly_stake=10.0)
    db.skip_prediction(pid)

    balance_before = get_state().balance
    db.update_result(event_id="SKP", market="h2h", selection="1", result="win")
    balance_after = get_state().balance

    # No bankroll movement on skipped resolution
    assert balance_after == balance_before


def test_resolver_credits_confirmed_win():
    from betbot.bankroll import bootstrap_initial_deposit, get_state
    from betbot.db import Database
    from betbot.config import load_settings

    bootstrap_initial_deposit(100.0)
    db = Database(load_settings().database_url)
    pid = _seed_proposed_pick(db, event_id="WIN", kelly_stake=10.0, best_odds=2.0)
    db.confirm_prediction_placed(pid)
    assert get_state().balance == 90.0  # debited

    db.update_result(event_id="WIN", market="h2h", selection="1", result="win")
    # Win pays stake × odds = 10 × 2.0 = 20 back, so balance = 90 + 20 = 110
    assert get_state().balance == 110.0
