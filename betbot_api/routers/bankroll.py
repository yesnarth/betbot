"""Bankroll endpoints — capital state, deposits/withdrawals (idempotent),
ledger, guards, bookmaker accounts, evolution chart data."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from betbot_api.auth import require_auth
from betbot_api.deps import limiter
from betbot_api.schemas import (
    BankrollLedgerRow,
    BankrollMutation,
    BankrollSnapshot,
)

router = APIRouter(prefix="/bankroll", tags=["bankroll"])


@router.get("/state", response_model=BankrollSnapshot)
def bankroll_state(_: str = Depends(require_auth)) -> BankrollSnapshot:
    """Current snapshot: balance, committed (immobilized on pending bets),
    available (free cash), total deposits/withdrawals, P&L."""
    from betbot.bankroll import get_state
    s = get_state()
    return BankrollSnapshot(**s.__dict__)


@router.post("/deposit", response_model=BankrollSnapshot)
@limiter.limit("10/minute")
def bankroll_deposit(
    request: Request,
    body: BankrollMutation,
    _: str = Depends(require_auth),
) -> BankrollSnapshot:
    """Add capital. The amount is always positive.

    Supports `Idempotency-Key` header: same key + same body within the
    retention window returns the cached response without re-debiting.
    """
    from betbot.bankroll import deposit
    from betbot.idempotency import IdempotencyConflict, lookup, record

    idem_key = request.headers.get("Idempotency-Key")
    body_payload = body.model_dump()
    try:
        cached = lookup(idem_key, "bankroll/deposit", body_payload)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if cached is not None:
        return BankrollSnapshot(**cached.response)

    s = deposit(body.amount, note=body.note)
    response = s.__dict__
    record(idem_key, "bankroll/deposit", body_payload, response, status_code=200)
    return BankrollSnapshot(**response)


@router.post("/withdraw", response_model=BankrollSnapshot)
@limiter.limit("10/minute")
def bankroll_withdraw(
    request: Request,
    body: BankrollMutation,
    _: str = Depends(require_auth),
) -> BankrollSnapshot:
    """Remove capital. Refuses if the available balance is insufficient.

    Supports `Idempotency-Key` header (see /bankroll/deposit).
    """
    from betbot.bankroll import InsufficientFundsError, withdraw
    from betbot.idempotency import IdempotencyConflict, lookup, record

    idem_key = request.headers.get("Idempotency-Key")
    body_payload = body.model_dump()
    try:
        cached = lookup(idem_key, "bankroll/withdraw", body_payload)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if cached is not None:
        return BankrollSnapshot(**cached.response)

    try:
        s = withdraw(body.amount, note=body.note)
    except InsufficientFundsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = s.__dict__
    record(idem_key, "bankroll/withdraw", body_payload, response, status_code=200)
    return BankrollSnapshot(**response)


@router.get("/history", response_model=list[BankrollLedgerRow])
def bankroll_history(
    limit: int = Query(default=200, ge=1, le=1000),
    bookmaker_key: str | None = Query(default=None,
                                      description="Filter to one bookmaker account"),
    _: str = Depends(require_auth),
) -> list[BankrollLedgerRow]:
    """Recent ledger entries, newest first. Optionally filtered by bookmaker."""
    from betbot.bankroll import get_history
    return [BankrollLedgerRow(**row)
            for row in get_history(limit=limit, bookmaker_key=bookmaker_key)]


@router.get("/guards")
def bankroll_guards(_: str = Depends(require_auth)) -> dict:
    """Current guard state: stop-loss status, exposure, daily caps used/remaining."""
    from betbot.guards import get_guard_status
    return get_guard_status()


@router.get("/bookmakers")
def bankroll_bookmakers(
    active_only: bool = Query(default=False),
    _: str = Depends(require_auth),
) -> list[dict]:
    """List all bookmaker accounts with their per-account balance."""
    from betbot.bankroll import list_bookmakers
    return list_bookmakers(active_only=active_only)


@router.post("/bookmakers")
def bankroll_add_bookmaker(
    body: dict,
    _: str = Depends(require_auth),
) -> dict:
    """Register a new bookmaker account. Body: {key, display_name, note?}."""
    from betbot.bankroll import add_bookmaker
    return add_bookmaker(
        key=body.get("key", "").strip(),
        display_name=body.get("display_name", "").strip(),
        note=body.get("note"),
    )


@router.get("/evolution")
def bankroll_evolution(
    days: int = Query(default=30, ge=1, le=365),
    bookmaker_key: str | None = Query(default=None,
                                      description="Filter to one bookmaker account"),
    _: str = Depends(require_auth),
) -> list[dict]:
    """Time series for the dashboard chart, optionally filtered by bookmaker."""
    from betbot.bankroll import get_evolution
    return get_evolution(days=days, bookmaker_key=bookmaker_key)
