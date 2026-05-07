"""
Concurrency test for the bankroll ledger.

⚠ DESTRUCTIVE: autouse fixture wipes the ledger and predictions tables
between tests. Refuses to run unless BETBOT_TEST_DATABASE_URL points at
a Postgres DB whose URL contains "test".

Validates that the advisory-lock-protected `_append()` cannot be tricked
by simultaneous threads into reading the same balance and double-spending.
"""
import os
import threading
import pytest


def _is_safe_test_db() -> bool:
    url = os.getenv("BETBOT_TEST_DATABASE_URL", "").strip()
    if not url.startswith(("postgresql://", "postgresql+")):
        return False
    return "test" in url.lower()


pytestmark = pytest.mark.skipif(
    not _is_safe_test_db(),
    reason="bankroll concurrency tests need BETBOT_TEST_DATABASE_URL pointing "
           "at a Postgres DB whose URL contains 'test' (autouse fixture is destructive).",
)


@pytest.fixture(autouse=True)
def _reset_ledger(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.getenv("BETBOT_TEST_DATABASE_URL", ""))
    from betbot.database import session_scope, reset_engine
    from betbot.orm_models import BankrollEntry, Prediction
    reset_engine()
    with session_scope() as s:
        s.query(BankrollEntry).delete()
        s.query(Prediction).delete()
    yield
    reset_engine()


def test_concurrent_deposits_balance_consistent():
    """20 threads each deposit 5$. Final balance must be exactly 100$.

    Without the advisory lock, balance_after snapshots interleave and the
    final cumulative SUM still works (because amount IS additive), but
    individual balance_after values would be wrong. We assert BOTH:
      - cumulative SUM == 100
      - max(balance_after) == 100  (every snapshot consistent with insertion order)
    """
    from betbot.bankroll import deposit, get_state
    from betbot.database import session_scope
    from betbot.orm_models import BankrollEntry
    from sqlalchemy import select, func

    n_threads = 20
    per_deposit = 5.0

    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def _worker():
        try:
            barrier.wait()
            deposit(per_deposit, note="concurrent")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"deposits raised: {errors}"

    state = get_state()
    assert abs(state.balance - n_threads * per_deposit) < 0.01, (
        f"cumulative balance {state.balance} != {n_threads * per_deposit}"
    )

    # Last balance_after row must equal the cumulative balance
    with session_scope() as s:
        max_after = s.execute(select(func.max(BankrollEntry.balance_after))).scalar()
    assert abs(max_after - n_threads * per_deposit) < 0.01


def test_concurrent_bet_placements_respect_available_funds():
    """10 threads try to place a 12$ bet. Available is only 50$ → at most 4 bets succeed."""
    from betbot.bankroll import (
        InsufficientFundsError,
        deposit,
        record_bet_placed,
    )
    from betbot.database import session_scope
    from betbot.orm_models import Prediction

    deposit(50.0)

    # Create 10 distinct predictions to attach the bets to
    pred_ids: list[int] = []
    with session_scope() as s:
        for i in range(10):
            p = Prediction(
                created_at="2026-05-06T00:00:00", event_id=f"evt_{i}",
                sport_key="soccer_epl", home_team="A", away_team="B",
                market="h2h", selection="1",
                model_prob=0.5, best_odds=2.0, best_book="x",
                value_edge=0.1, kelly_stake=12.0, model_type="poisson",
            )
            s.add(p)
        s.flush()
        pred_ids = [p.id for p in s.query(Prediction).all()]

    barrier = threading.Barrier(10)
    successes: list[int] = []
    failures: list[Exception] = []

    def _worker(pid: int):
        try:
            barrier.wait()
            record_bet_placed(pid, 12.0)
            successes.append(pid)
        except InsufficientFundsError as exc:
            failures.append(exc)

    threads = [threading.Thread(target=_worker, args=(pid,)) for pid in pred_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Math: 50$ / 12$ = 4.16 → max 4 bets can fit, the rest must be refused
    assert len(successes) == 4, f"expected 4 successes, got {len(successes)}"
    assert len(failures) == 6, f"expected 6 InsufficientFundsError, got {len(failures)}"
