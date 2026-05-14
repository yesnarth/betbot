"""
End-to-end lifecycle test — bet placement → resolution → bankroll updated.

⚠ DESTRUCTIVE: an autouse fixture wipes BankrollEntry and Prediction tables
between tests for clean isolation. Safety gate enforced by tests/e2e/conftest.py.

Validates the full sequence the user actually goes through:
  1. deposit initial bankroll
  2. save_prediction (proposed status, no debit)
  3. confirm_prediction_placed (debits the stake atomically)
  4. update_result(win) → bankroll credited atomically
  5. ROI stats reflect ONLY the confirmed bets, not proposed/skipped ones
"""
import os
import pytest


@pytest.fixture(autouse=True)
def _reset_db(monkeypatch):
    """Wipe ledger + predictions for clean test isolation. Only runs against
    the explicitly-opted-in test DB (see pytestmark above)."""
    # Force the application code to use the test DB even if shells have
    # DATABASE_URL set to prod.
    monkeypatch.setenv("DATABASE_URL", os.getenv("BETBOT_TEST_DATABASE_URL", ""))
    from betbot.database import session_scope, reset_engine
    from betbot.orm_models import BankrollEntry, Prediction
    reset_engine()
    with session_scope() as s:
        s.query(BankrollEntry).delete()
        s.query(Prediction).delete()
    yield
    reset_engine()


# ---------------------------------------------------------------------------
# Happy path: full bet lifecycle
# ---------------------------------------------------------------------------

def test_winning_bet_flows_through_lifecycle():
    """Deposit → save_prediction → confirm_placed → update_result(win) →
    balance credited correctly, ROI reflects the placed bet."""
    from betbot.bankroll import deposit, get_state
    from betbot.db import Database

    deposit(100.0, note="seed")
    db = Database()

    # Place a bet of 10$ at 2.0 odds
    ok = db.save_prediction(
        event_id="e2e_evt_1", sport_key="soccer_epl",
        home_team="Arsenal", away_team="Chelsea",
        market="h2h", selection="1",
        model_prob=0.55, best_odds=2.0, best_book="Pinnacle",
        value_edge=0.10, kelly_stake=10.0, model_type="poisson",
    )
    assert ok is True
    state_after_place = get_state()
    assert state_after_place.balance == 90.0
    assert state_after_place.committed == 10.0

    # User confirms they played the bet at Pinnacle
    pred = db.get_pending_predictions()[0]
    assert db.confirm_prediction_placed(pred["id"], bookmaker="pinnacle") is True

    # Match settles as a win
    db.update_result("e2e_evt_1", "h2h", "1", "win")

    state_after_win = get_state()
    # Won 10 × 2.0 = 20 credit, on top of the already-debited 90 → 110
    assert state_after_win.balance == 110.0
    assert state_after_win.committed == 0.0

    # ROI counts the placed winning bet (1 bet, 100% hit rate)
    roi = db.get_roi_stats(days=30, only_placed=True)
    assert roi["n_bets"] == 1
    assert roi["n_wins"] == 1
    assert roi["hit_rate"] == 100.0


def test_losing_bet_flows_through_lifecycle():
    from betbot.bankroll import deposit, get_state
    from betbot.db import Database

    deposit(100.0)
    db = Database()
    db.save_prediction(
        event_id="e2e_evt_2", sport_key="soccer_epl",
        home_team="Burnley", away_team="ManCity",
        market="h2h", selection="1",
        model_prob=0.30, best_odds=4.0, best_book="Bet365",
        value_edge=0.20, kelly_stake=5.0, model_type="poisson",
    )
    pred = db.get_pending_predictions()[0]
    db.confirm_prediction_placed(pred["id"], bookmaker="bet365")

    db.update_result("e2e_evt_2", "h2h", "1", "loss")

    state = get_state()
    # 100 - 5 (placed) - 0 (loss) = 95
    assert state.balance == 95.0
    assert state.committed == 0.0

    roi = db.get_roi_stats(days=30, only_placed=True)
    assert roi["n_bets"] == 1
    assert roi["n_wins"] == 0
    assert roi["hit_rate"] == 0.0


def test_void_refunds_stake():
    from betbot.bankroll import deposit, get_state
    from betbot.db import Database

    deposit(100.0)
    db = Database()
    db.save_prediction(
        event_id="e2e_evt_3", sport_key="soccer_epl",
        home_team="A", away_team="B",
        market="h2h", selection="1",
        model_prob=0.55, best_odds=2.0, best_book="x",
        value_edge=0.10, kelly_stake=8.0, model_type="poisson",
    )
    db.update_result("e2e_evt_3", "h2h", "1", "void")

    state = get_state()
    # 100 - 8 (placed) + 8 (void refund) = 100
    assert state.balance == 100.0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_resolving_twice_does_not_double_credit():
    from betbot.bankroll import deposit, get_state
    from betbot.db import Database

    deposit(100.0)
    db = Database()
    db.save_prediction(
        event_id="e2e_evt_4", sport_key="soccer_epl",
        home_team="A", away_team="B", market="h2h", selection="1",
        model_prob=0.55, best_odds=2.0, best_book="x",
        value_edge=0.10, kelly_stake=10.0, model_type="poisson",
    )
    db.update_result("e2e_evt_4", "h2h", "1", "win")
    balance_after_first = get_state().balance
    db.update_result("e2e_evt_4", "h2h", "1", "win")   # second call
    balance_after_second = get_state().balance
    assert balance_after_first == balance_after_second


# ---------------------------------------------------------------------------
# Unplaced bets: ROI MUST exclude them
# ---------------------------------------------------------------------------

def test_unplaced_bets_excluded_from_roi_by_default():
    """The bot recommends a bet the user does NOT play. After resolution,
    ROI must NOT count it (only_placed=True is the default)."""
    from betbot.bankroll import deposit, get_state
    from betbot.db import Database

    deposit(100.0)
    db = Database()
    db.save_prediction(
        event_id="e2e_evt_5", sport_key="soccer_epl",
        home_team="A", away_team="B", market="h2h", selection="1",
        model_prob=0.55, best_odds=2.0, best_book="x",
        value_edge=0.10, kelly_stake=10.0, model_type="poisson",
    )
    # Note: NO confirm_prediction_placed() — user didn't play it
    db.update_result("e2e_evt_5", "h2h", "1", "win")

    roi_placed = db.get_roi_stats(days=30, only_placed=True)
    roi_all = db.get_roi_stats(days=30, only_placed=False)
    assert roi_placed["n_bets"] == 0      # nothing actually played
    assert roi_all["n_bets"] == 1         # but the prediction is still recorded


# ---------------------------------------------------------------------------
# Guard: cool-off after consecutive losses
# ---------------------------------------------------------------------------

def test_cool_off_blocks_after_consecutive_losses(monkeypatch):
    """Three consecutive losses → next bet refused by guard."""
    from betbot.bankroll import deposit
    from betbot.db import Database
    from betbot.guards import GuardViolation, check_can_place_bet

    monkeypatch.setenv("COOL_OFF_LOSSES", "3")
    monkeypatch.setenv("COOL_OFF_HOURS", "12")

    deposit(100.0)
    db = Database()
    for i in range(3):
        db.save_prediction(
            event_id=f"e2e_loss_{i}", sport_key="soccer_epl",
            home_team="A", away_team="B", market="h2h", selection="1",
            model_prob=0.55, best_odds=2.0, best_book="x",
            value_edge=0.10, kelly_stake=5.0, model_type="poisson",
        )
        pid = db.get_pending_predictions()[0]["id"]
        db.confirm_prediction_placed(pid)
        db.update_result(f"e2e_loss_{i}", "h2h", "1", "loss")

    # Next bet must be refused by the cool-off guard
    with pytest.raises(GuardViolation):
        check_can_place_bet(5.0)
