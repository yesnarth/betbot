"""
Bankroll management — single source of truth for capital state.

Every cash movement is appended to `bankroll_ledger`. The current balance is
the cumulative sum of `amount`. Capital "available" subtracts stakes that
are immobilized on unresolved predictions.

Public API (callable from CLI, FastAPI, scheduler, tests):

    deposit(amount, note=None)             → BankrollState
    withdraw(amount, note=None)            → BankrollState (raises if insufficient)
    record_bet_placed(prediction_id, stake)→ BankrollState
    record_bet_won(prediction_id, payout)  → BankrollState
    record_bet_lost(prediction_id)         → no movement (already debited)
    record_bet_void(prediction_id, stake)  → BankrollState (refund)
    adjustment(amount, note)               → BankrollState (manual correction)

    get_state()                            → BankrollState (live snapshot)
    get_history(limit=200)                 → list[dict] of recent entries
    get_evolution(days=30)                 → list[(ts, balance)] for plotting
    bootstrap_initial_deposit(amount)      → idempotent: only if ledger is empty

Concurrency note: all writes go through a single transaction that locks the
final balance computation. Two simultaneous bets on the same race condition
window will see a consistent balance because session_scope() commits atomically.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func

from betbot.database import session_scope
from betbot.orm_models import BankrollEntry, Prediction

logger = logging.getLogger("betbot.bankroll")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class InsufficientFundsError(RuntimeError):
    """Raised when a bet placement or withdrawal would push the available
    balance below zero. Callers catch this and refuse the action."""


# ---------------------------------------------------------------------------
# Live state snapshot
# ---------------------------------------------------------------------------

@dataclass
class BankrollState:
    balance: float          # cumulative sum of all ledger amounts
    committed: float        # stakes locked on predictions that aren't resolved
    available: float        # balance − committed (free cash)
    total_deposits: float   # gross sum of "deposit" entries (inception capital)
    total_withdrawals: float
    total_won: float        # gross "bet_won" payouts
    total_lost_stakes: float  # gross "bet_placed" amount on lost predictions
    pnl: float              # balance − total_deposits + total_withdrawals
    n_entries: int


def _current_balance(s) -> float:
    row = s.execute(select(func.coalesce(func.sum(BankrollEntry.amount), 0.0))).first()
    return float(row[0] or 0.0)


def _committed_amount(s) -> float:
    """Sum of stakes (positive values) on predictions that haven't been resolved."""
    row = s.execute(
        select(func.coalesce(func.sum(-BankrollEntry.amount), 0.0))
        .join(Prediction, BankrollEntry.prediction_id == Prediction.id)
        .where(
            BankrollEntry.kind == "bet_placed",
            Prediction.result.is_(None),
        )
    ).first()
    return float(row[0] or 0.0)


def get_state() -> BankrollState:
    """Snapshot of the bankroll. O(1) over the ledger size thanks to indexes."""
    with session_scope() as s:
        balance = _current_balance(s)
        committed = _committed_amount(s)

        deposits = s.execute(
            select(func.coalesce(func.sum(BankrollEntry.amount), 0.0))
            .where(BankrollEntry.kind == "deposit")
        ).scalar() or 0.0
        withdrawals = s.execute(
            select(func.coalesce(func.sum(-BankrollEntry.amount), 0.0))
            .where(BankrollEntry.kind == "withdrawal")
        ).scalar() or 0.0
        won = s.execute(
            select(func.coalesce(func.sum(BankrollEntry.amount), 0.0))
            .where(BankrollEntry.kind == "bet_won")
        ).scalar() or 0.0
        lost_stakes = s.execute(
            select(func.coalesce(func.sum(-BankrollEntry.amount), 0.0))
            .join(Prediction, BankrollEntry.prediction_id == Prediction.id)
            .where(
                BankrollEntry.kind == "bet_placed",
                Prediction.result == "loss",
            )
        ).scalar() or 0.0
        n_entries = s.execute(select(func.count(BankrollEntry.id))).scalar() or 0

    return BankrollState(
        balance=round(balance, 2),
        committed=round(committed, 2),
        available=round(balance - committed, 2),
        total_deposits=round(float(deposits), 2),
        total_withdrawals=round(float(withdrawals), 2),
        total_won=round(float(won), 2),
        total_lost_stakes=round(float(lost_stakes), 2),
        pnl=round(balance - float(deposits) + float(withdrawals), 2),
        n_entries=int(n_entries),
    )


# ---------------------------------------------------------------------------
# Mutations — every helper appends ONE ledger row inside a transaction
# ---------------------------------------------------------------------------

def _append(
    s,
    kind: str,
    amount: float,
    prediction_id: int | None = None,
    note: str | None = None,
) -> float:
    """Append a ledger entry inside an existing session. Returns the new balance."""
    new_balance = _current_balance(s) + amount
    s.add(BankrollEntry(
        ts=datetime.now(timezone.utc).isoformat(),
        kind=kind,
        amount=round(float(amount), 4),
        balance_after=round(new_balance, 4),
        prediction_id=prediction_id,
        note=note,
    ))
    return new_balance


def deposit(amount: float, note: str | None = None) -> BankrollState:
    if amount <= 0:
        raise ValueError("Deposit amount must be > 0")
    with session_scope() as s:
        _append(s, "deposit", amount, note=note)
    state = get_state()
    logger.info("Bankroll : deposit +%.2f → balance %.2f", amount, state.balance)
    return state


def withdraw(amount: float, note: str | None = None) -> BankrollState:
    if amount <= 0:
        raise ValueError("Withdrawal amount must be > 0")
    state_before = get_state()
    if amount > state_before.available:
        raise InsufficientFundsError(
            f"Cannot withdraw {amount:.2f} — only {state_before.available:.2f} available "
            f"(committed: {state_before.committed:.2f})"
        )
    with session_scope() as s:
        _append(s, "withdrawal", -amount, note=note)
    state = get_state()
    logger.info("Bankroll : withdraw -%.2f → balance %.2f", amount, state.balance)
    return state


def record_bet_placed(
    prediction_id: int,
    stake: float,
    enforce_funds: bool = True,
    note: str | None = None,
) -> BankrollState:
    """Immobilize `stake` on a fresh prediction. Refuses if insufficient funds."""
    if stake <= 0:
        raise ValueError("Stake must be > 0")
    if enforce_funds:
        state = get_state()
        if stake > state.available:
            raise InsufficientFundsError(
                f"Cannot place bet of {stake:.2f} — only {state.available:.2f} available"
            )
    with session_scope() as s:
        _append(s, "bet_placed", -stake, prediction_id=prediction_id, note=note)
    return get_state()


def record_bet_won(
    prediction_id: int,
    stake: float,
    odds: float,
    note: str | None = None,
) -> BankrollState:
    """Credit the FULL return (stake × odds) since the original stake was
    debited at placement. Net profit = stake × (odds - 1)."""
    payout = stake * odds
    with session_scope() as s:
        _append(s, "bet_won", payout, prediction_id=prediction_id, note=note)
    state = get_state()
    logger.info("Bankroll : bet_won +%.2f → balance %.2f", payout, state.balance)
    return state


def record_bet_lost(prediction_id: int, note: str | None = None) -> BankrollState:
    """No cash movement — stake was already debited at placement.
    We append a ZERO-amount entry purely for audit / reporting."""
    with session_scope() as s:
        _append(s, "bet_lost", 0.0, prediction_id=prediction_id, note=note)
    return get_state()


def record_bet_void(
    prediction_id: int,
    stake: float,
    note: str | None = None,
) -> BankrollState:
    """Push / void / cancelled match — refund the stake."""
    with session_scope() as s:
        _append(s, "bet_void", stake, prediction_id=prediction_id, note=note)
    return get_state()


def adjustment(amount: float, note: str) -> BankrollState:
    """Manual correction. Always requires a note."""
    if not note:
        raise ValueError("adjustment() requires a note")
    with session_scope() as s:
        _append(s, "adjustment", amount, note=note)
    return get_state()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_history(limit: int = 200) -> list[dict]:
    """Recent ledger entries, newest first."""
    with session_scope() as s:
        rows = s.execute(
            select(BankrollEntry).order_by(BankrollEntry.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "ts": r.ts,
                "kind": r.kind,
                "amount": r.amount,
                "balance_after": r.balance_after,
                "prediction_id": r.prediction_id,
                "note": r.note,
            }
            for r in rows
        ]


def get_evolution(days: int = 30) -> list[dict]:
    """Time series for the dashboard chart: every ledger row within the period
    with its `balance_after` snapshot. Caller can plot ts → balance."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with session_scope() as s:
        rows = s.execute(
            select(BankrollEntry.ts, BankrollEntry.balance_after, BankrollEntry.kind)
            .where(BankrollEntry.ts >= cutoff)
            .order_by(BankrollEntry.id.asc())
        ).all()
        return [{"ts": r[0], "balance": r[1], "kind": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# Bootstrap helper — creates the inception deposit on a fresh DB
# ---------------------------------------------------------------------------

def bootstrap_initial_deposit(amount: float, note: str = "inception") -> bool:
    """Idempotent: only adds the deposit if the ledger is completely empty.

    Returns True if a deposit was created, False if the ledger already had
    rows (so we don't double-credit). Called at startup with the value of
    `BANKROLL` from .env to keep the legacy behavior compatible.
    """
    with session_scope() as s:
        n = s.execute(select(func.count(BankrollEntry.id))).scalar() or 0
    if n > 0:
        return False
    if amount <= 0:
        return False
    deposit(amount, note=note)
    return True
