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

from sqlalchemy import select, func, text

from betbot.database import session_scope
from betbot.orm_models import BankrollEntry, Bookmaker, Prediction


# Postgres advisory lock key — arbitrary signed bigint unique to this bot.
# pg_advisory_xact_lock holds a transaction-scoped lock that prevents two
# concurrent ledger writers from reading the same balance and racing each
# other to insert. Released automatically at COMMIT/ROLLBACK.
_ADVISORY_LOCK_KEY = 0x42BE7B07CAB17A1  # 16 hex digits = signed 60-bit positive

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


def _current_balance(s, bookmaker_key: str | None = None) -> float:
    """Cumulative ledger sum, optionally filtered to a single bookmaker account."""
    stmt = select(func.coalesce(func.sum(BankrollEntry.amount), 0.0))
    if bookmaker_key is not None:
        stmt = stmt.where(BankrollEntry.bookmaker_key == bookmaker_key)
    row = s.execute(stmt).first()
    return float(row[0] or 0.0)


def _committed_amount(s, bookmaker_key: str | None = None) -> float:
    """Sum of stakes (positive values) on predictions that haven't been resolved.
    When bookmaker_key is set, only stakes on that account are counted."""
    stmt = (
        select(func.coalesce(func.sum(-BankrollEntry.amount), 0.0))
        .join(Prediction, BankrollEntry.prediction_id == Prediction.id)
        .where(
            BankrollEntry.kind == "bet_placed",
            Prediction.result.is_(None),
        )
    )
    if bookmaker_key is not None:
        stmt = stmt.where(BankrollEntry.bookmaker_key == bookmaker_key)
    row = s.execute(stmt).first()
    return float(row[0] or 0.0)


def get_state(bookmaker_key: str | None = None) -> BankrollState:
    """Snapshot of the bankroll, globally or filtered to one bookmaker."""
    with session_scope() as s:
        balance = _current_balance(s, bookmaker_key=bookmaker_key)
        committed = _committed_amount(s, bookmaker_key=bookmaker_key)

        def _filter_bk(stmt):
            return stmt.where(BankrollEntry.bookmaker_key == bookmaker_key) if bookmaker_key else stmt

        deposits = s.execute(_filter_bk(
            select(func.coalesce(func.sum(BankrollEntry.amount), 0.0))
            .where(BankrollEntry.kind == "deposit")
        )).scalar() or 0.0
        withdrawals = s.execute(_filter_bk(
            select(func.coalesce(func.sum(-BankrollEntry.amount), 0.0))
            .where(BankrollEntry.kind == "withdrawal")
        )).scalar() or 0.0
        won = s.execute(_filter_bk(
            select(func.coalesce(func.sum(BankrollEntry.amount), 0.0))
            .where(BankrollEntry.kind == "bet_won")
        )).scalar() or 0.0
        lost_stakes = s.execute(_filter_bk(
            select(func.coalesce(func.sum(-BankrollEntry.amount), 0.0))
            .join(Prediction, BankrollEntry.prediction_id == Prediction.id)
            .where(
                BankrollEntry.kind == "bet_placed",
                Prediction.result == "loss",
            )
        )).scalar() or 0.0
        n_entries_stmt = select(func.count(BankrollEntry.id))
        n_entries = s.execute(_filter_bk(n_entries_stmt)).scalar() or 0

    return BankrollState(
        balance=round(balance, 2),
        committed=round(committed, 2),
        # `balance` already reflects bet_placed debits — `available` IS
        # `balance` (cash on hand). `committed` is informational only.
        available=round(balance, 2),
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

def _acquire_ledger_lock(s) -> None:
    """Acquire the advisory lock that serializes ledger writes.

    On PostgreSQL: pg_advisory_xact_lock — released automatically at COMMIT/ROLLBACK.
    On other dialects: no-op (we still rely on `session_scope`'s commit
    semantics, but the application is Postgres-only in production).
    """
    try:
        if s.bind.dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
    except Exception as exc:
        logger.warning("Could not acquire advisory lock (%s) — proceeding without", exc)


def _append(
    s,
    kind: str,
    amount: float,
    prediction_id: int | None = None,
    note: str | None = None,
    bookmaker_key: str | None = None,
) -> float:
    """Append a ledger entry inside an existing session. Returns the new balance.

    Holds the advisory lock for the lifetime of the surrounding transaction
    so that concurrent calls serialize at this step, eliminating the
    "two readers see the same balance" race.

    If `bookmaker_key` is provided, the row is partitioned to that account
    (used by per-account snapshots). Legacy rows / global movements use NULL.
    """
    _acquire_ledger_lock(s)
    new_balance = _current_balance(s, bookmaker_key=bookmaker_key) + amount
    s.add(BankrollEntry(
        ts=datetime.now(timezone.utc).isoformat(),
        kind=kind,
        amount=round(float(amount), 4),
        balance_after=round(new_balance, 4),
        prediction_id=prediction_id,
        bookmaker_key=bookmaker_key,
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


def _state_inside_lock(s) -> tuple[float, float]:
    """Return (balance, committed) read while holding the ledger lock.

    IMPORTANT semantics:
      - `balance` = cumulative sum of ALL ledger movements. Since bet_placed
                    is recorded as -stake, the balance already reflects what
                    has gone OUT of the cash pile. balance == cash on hand.
      - `committed` = the SAME stakes, but for reporting only — "how much is
                       riding on still-unresolved bets". NOT to be subtracted
                       from balance again (that would be double-counting).
      - `available` for spending purposes is therefore simply `balance`.
    """
    balance = _current_balance(s)
    committed = _committed_amount(s)
    return balance, committed


def withdraw(amount: float, note: str | None = None) -> BankrollState:
    if amount <= 0:
        raise ValueError("Withdrawal amount must be > 0")
    with session_scope() as s:
        _acquire_ledger_lock(s)
        balance, _ = _state_inside_lock(s)
        if amount > balance:
            raise InsufficientFundsError(
                f"Cannot withdraw {amount:.2f} — only {balance:.2f} cash on hand"
            )
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
    """Immobilize `stake` atomically. Holds the ledger lock for the entire
    check-then-write so concurrent placements can't double-spend."""
    if stake <= 0:
        raise ValueError("Stake must be > 0")
    with session_scope() as s:
        _acquire_ledger_lock(s)
        if enforce_funds:
            balance, _ = _state_inside_lock(s)
            if stake > balance:
                raise InsufficientFundsError(
                    f"Cannot place bet of {stake:.2f} — only {balance:.2f} on hand"
                )
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
# Bookmaker accounts
# ---------------------------------------------------------------------------

def list_bookmakers(active_only: bool = False) -> list[dict]:
    """Return all bookmaker accounts with their per-account balance."""
    with session_scope() as s:
        stmt = select(Bookmaker).order_by(Bookmaker.created_at.asc())
        if active_only:
            stmt = stmt.where(Bookmaker.active.is_(True))
        rows = s.execute(stmt).scalars().all()
        out = []
        for b in rows:
            balance = _current_balance(s, bookmaker_key=b.key)
            committed = _committed_amount(s, bookmaker_key=b.key)
            out.append({
                "key": b.key,
                "display_name": b.display_name,
                "created_at": b.created_at,
                "active": b.active,
                "note": b.note,
                "balance": round(balance, 2),
                "committed": round(committed, 2),
                "available": round(balance, 2),  # see _state_inside_lock docstring
            })
        return out


def add_bookmaker(key: str, display_name: str, note: str | None = None) -> dict:
    """Register a new bookmaker account. Idempotent on `key`."""
    if not key or not display_name:
        raise ValueError("key and display_name are required")
    with session_scope() as s:
        existing = s.get(Bookmaker, key)
        if existing:
            existing.display_name = display_name
            if note is not None:
                existing.note = note
            existing.active = True
            return {"key": existing.key, "display_name": existing.display_name,
                    "active": existing.active, "note": existing.note}
        b = Bookmaker(
            key=key, display_name=display_name,
            created_at=datetime.now(timezone.utc).isoformat(),
            active=True, note=note,
        )
        s.add(b)
    return {"key": key, "display_name": display_name, "active": True, "note": note}


def deactivate_bookmaker(key: str) -> bool:
    """Soft-disable an account. Existing ledger entries stay attached for audit."""
    with session_scope() as s:
        b = s.get(Bookmaker, key)
        if b is None:
            return False
        b.active = False
        return True


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
