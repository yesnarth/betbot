"""
Promotions and cash-outs — domain logic for bookmaker bonuses.

Both are recorded as ledger movements so the bankroll stays the single source
of truth for cash position:

  - record_promotion()  : creates a Promotion row + an "adjustment" ledger
                          entry of `cash_equivalent` (the user's *real* extra
                          balance), with a note tagging the promo id.
  - record_cashout()    : closes a pending prediction at the cash-out amount
                          (creates `bet_void` + `bet_won` pair), records a
                          CashOut row for audit.

Why not just adjust the balance directly? Because we want to query:
  - "how much of my P&L came from promos vs strategy?"
  - "what's my cash-out hit rate vs holding to settlement?"
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from betbot.bankroll import _acquire_ledger_lock, _append, get_state
from betbot.database import session_scope
from betbot.orm_models import CashOut, Prediction, Promotion

logger = logging.getLogger("betbot.promotions")


# ---------------------------------------------------------------------------
# Promotions
# ---------------------------------------------------------------------------

def record_promotion(
    kind: str,
    nominal_value: float,
    cash_equivalent: float,
    bookmaker_key: str | None = None,
    rollover_x: float | None = None,
    expires_at: str | None = None,
    note: str | None = None,
) -> int:
    """
    Register a promo and credit its cash equivalent to the bankroll.

    Args:
        kind:             freebet | deposit_match | refund | boost | cashback
        nominal_value:    face value (e.g. 50 for a 50€ freebet)
        cash_equivalent:  realistic EV of the promo (typically < nominal due
                          to rollover or odds caps). User decides the value.
        bookmaker_key:    which account received the promo
        rollover_x:       wagering requirement multiplier (e.g. 5x)
        expires_at:       ISO timestamp when the promo lapses
        note:             free-form description
    """
    if cash_equivalent < 0:
        raise ValueError("cash_equivalent must be ≥ 0")

    with session_scope() as s:
        promo = Promotion(
            received_at=datetime.now(timezone.utc).isoformat(),
            bookmaker_key=bookmaker_key,
            kind=kind,
            nominal_value=nominal_value,
            cash_equivalent=cash_equivalent,
            rollover_x=rollover_x,
            expires_at=expires_at,
            used=False,
            note=note,
        )
        s.add(promo)
        s.flush()
        promo_id = promo.id

        # Bankroll credit (counts as adjustment so it doesn't pollute deposits)
        if cash_equivalent > 0:
            _append(
                s, "adjustment", cash_equivalent,
                bookmaker_key=bookmaker_key,
                note=f"promo #{promo_id} {kind} nominal={nominal_value}",
            )
    logger.info("Promotion enregistrée #%s : %s +%.2f$ EV (nominal %.2f$)",
                promo_id, kind, cash_equivalent, nominal_value)
    return promo_id


def mark_promotion_used(promo_id: int) -> bool:
    """Mark a promo as consumed."""
    with session_scope() as s:
        p = s.get(Promotion, promo_id)
        if p is None:
            return False
        p.used = True
        p.used_at = datetime.now(timezone.utc).isoformat()
    return True


def list_promotions(only_unused: bool = False) -> list[dict]:
    """List promotions, newest first."""
    with session_scope() as s:
        stmt = select(Promotion).order_by(Promotion.id.desc())
        if only_unused:
            stmt = stmt.where(Promotion.used.is_(False))
        rows = s.execute(stmt).scalars().all()
        return [
            {
                "id": p.id,
                "received_at": p.received_at,
                "bookmaker_key": p.bookmaker_key,
                "kind": p.kind,
                "nominal_value": p.nominal_value,
                "cash_equivalent": p.cash_equivalent,
                "rollover_x": p.rollover_x,
                "expires_at": p.expires_at,
                "used": p.used,
                "used_at": p.used_at,
                "note": p.note,
            }
            for p in rows
        ]


def promotions_summary() -> dict:
    """Aggregate stats: how much of the bankroll P&L comes from promos."""
    with session_scope() as s:
        rows = s.execute(
            select(Promotion.cash_equivalent, Promotion.nominal_value, Promotion.used)
        ).all()
    n = len(rows)
    total_cash = sum(r[0] for r in rows)
    total_nominal = sum(r[1] for r in rows)
    n_used = sum(1 for r in rows if r[2])
    return {
        "n_promotions": n,
        "n_used": n_used,
        "total_nominal_value": round(total_nominal, 2),
        "total_cash_equivalent": round(total_cash, 2),
        "promo_haircut_pct": round(
            (1 - total_cash / total_nominal) * 100, 1
        ) if total_nominal > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Cash-outs
# ---------------------------------------------------------------------------

def record_cashout(
    prediction_id: int,
    cash_out_amount: float,
    bookmaker_offered_price: float | None = None,
    note: str | None = None,
) -> int:
    """
    Close a pending prediction at the bookmaker's cash-out price.

    Effect on the bankroll: the original stake was already debited at
    placement. We credit `cash_out_amount` (could be > or < stake depending
    on how the match has gone). The prediction is marked resolved with
    result='void' (since it's neither a clean win nor a clean loss).
    """
    if cash_out_amount < 0:
        raise ValueError("cash_out_amount must be ≥ 0")

    with session_scope() as s:
        pred = s.get(Prediction, prediction_id)
        if pred is None:
            raise ValueError(f"Prediction #{prediction_id} not found")
        if pred.result is not None:
            raise ValueError(f"Prediction #{prediction_id} already resolved as {pred.result}")

        pred.result = "void"
        pred.resolved_at = datetime.now(timezone.utc).isoformat()

        cashout = CashOut(
            prediction_id=prediction_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            cash_out_amount=cash_out_amount,
            bookmaker_offered_price=bookmaker_offered_price,
            note=note,
        )
        s.add(cashout)
        s.flush()
        cashout_id = cashout.id

        # Credit the bankroll. Tag clearly so /bankroll/history shows the link.
        _acquire_ledger_lock(s)
        _append(
            s, "bet_void", cash_out_amount,
            prediction_id=prediction_id,
            note=f"cashout #{cashout_id} for pred #{prediction_id}",
        )
    logger.info("Cash-out enregistré #%s : pred=%s amount=%.2f$",
                cashout_id, prediction_id, cash_out_amount)
    return cashout_id


def list_cashouts(limit: int = 100) -> list[dict]:
    """Recent cash-outs, newest first, with the original prediction context."""
    with session_scope() as s:
        rows = s.execute(
            select(CashOut, Prediction)
            .join(Prediction, CashOut.prediction_id == Prediction.id)
            .order_by(CashOut.id.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": co.id,
                "created_at": co.created_at,
                "prediction_id": co.prediction_id,
                "home_team": pred.home_team,
                "away_team": pred.away_team,
                "stake": pred.kelly_stake,
                "cash_out_amount": co.cash_out_amount,
                "net_pnl": round(co.cash_out_amount - pred.kelly_stake, 2),
                "bookmaker_offered_price": co.bookmaker_offered_price,
                "note": co.note,
            }
            for co, pred in rows
        ]
