"""Bookmaker promotions and cash-out tracking endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from betbot_api.auth import require_auth

router = APIRouter(tags=["promotions"])


@router.post("/promotions")
def add_promotion(body: dict, _: str = Depends(require_auth)) -> dict:
    """
    Record a bookmaker promo (freebet, deposit match, refund, etc.).
    Body:
      kind            : "freebet" | "deposit_match" | "refund" | "boost" | "cashback"
      nominal_value   : face value (e.g. 50 for a 50€ freebet)
      cash_equivalent : realistic EV after rollover/odds caps (often < nominal)
      bookmaker_key   : optional account identifier
      rollover_x      : optional wagering requirement
      note            : optional free text
    """
    from betbot.promotions import record_promotion
    promo_id = record_promotion(
        kind=body.get("kind", "").strip(),
        nominal_value=float(body["nominal_value"]),
        cash_equivalent=float(body["cash_equivalent"]),
        bookmaker_key=body.get("bookmaker_key"),
        rollover_x=body.get("rollover_x"),
        expires_at=body.get("expires_at"),
        note=body.get("note"),
    )
    return {"id": promo_id}


@router.get("/promotions")
def list_promos(only_unused: bool = Query(default=False),
                _: str = Depends(require_auth)) -> list[dict]:
    from betbot.promotions import list_promotions
    return list_promotions(only_unused=only_unused)


@router.get("/promotions/summary")
def promo_summary(_: str = Depends(require_auth)) -> dict:
    from betbot.promotions import promotions_summary
    return promotions_summary()


@router.post("/promotions/{promo_id}/used")
def mark_promo_used(promo_id: int, _: str = Depends(require_auth)) -> dict:
    from betbot.promotions import mark_promotion_used
    if not mark_promotion_used(promo_id):
        raise HTTPException(status_code=404, detail=f"Promotion {promo_id} not found")
    return {"id": promo_id, "used": True}


@router.post("/cashouts")
def add_cashout(body: dict, _: str = Depends(require_auth)) -> dict:
    """
    Record a cash-out on a pending prediction. Body:
      prediction_id          : the bet being cashed out
      cash_out_amount        : actual amount received
      bookmaker_offered_price: the cash-out price the bookie showed (optional)
      note                   : optional
    """
    from betbot.promotions import record_cashout
    try:
        cashout_id = record_cashout(
            prediction_id=int(body["prediction_id"]),
            cash_out_amount=float(body["cash_out_amount"]),
            bookmaker_offered_price=body.get("bookmaker_offered_price"),
            note=body.get("note"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": cashout_id}


@router.get("/cashouts")
def list_cashouts_endpoint(
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(require_auth),
) -> list[dict]:
    from betbot.promotions import list_cashouts
    return list_cashouts(limit=limit)
