"""Predictions endpoints — confirm placement, skip/unskip lifecycle,
proposed/skipped/pending queues, batch resolve. Also `/admin/save-pick-as-proposed`
which writes a pick directly into the validation queue."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from betbot.api import OddsAPIClient
from betbot.config import load_settings
from betbot.db import Database
from betbot.resolver import resolve_pending
from betbot_api.auth import require_auth
from betbot_api.deps import get_db, limiter
from betbot_api.schemas import (
    ConfirmPlacedRequest,
    PredictionRow,
    ProposedPickInput,
    SkipRequest,
)

router = APIRouter(tags=["predictions"])


@router.post("/predictions/{prediction_id}/confirm-placed")
@limiter.limit("30/minute")
def confirm_placed(
    request: Request,
    prediction_id: int,
    body: ConfirmPlacedRequest = ConfirmPlacedRequest(),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Confirm the user actually placed this bet at their bookmaker.
    THIS is when the bankroll is debited (atomic with the placement-status
    update). Pre-flight guard checks (stop-loss, daily cap, exposure)
    apply here, NOT at scan time.

    Body fields are validated by ConfirmPlacedRequest — `unconfirm: "false"`
    (string) is rejected with a 422 instead of being silently coerced.
    """
    from betbot.bankroll import InsufficientFundsError
    from betbot.guards import GuardViolation
    try:
        ok = db.confirm_prediction_placed(
            prediction_id, body.bookmaker, unconfirm=body.unconfirm,
        )
    except InsufficientFundsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GuardViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Prediction {prediction_id} not found")
    return {"prediction_id": prediction_id,
            "placement_status": "proposed" if body.unconfirm else "confirmed",
            "bookmaker": body.bookmaker}


@router.post("/admin/save-pick-as-proposed")
@limiter.limit("60/minute")
def save_pick_as_proposed(
    request: Request,
    pick: ProposedPickInput,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Push a single pick (typically from /recommend/manual or /recommend/agent-local)
    into the predictions table as 'proposed'. Same effect as if the worker
    had generated it during a scheduled scan : the row appears in the
    validation queue, awaiting user confirmation, with NO bankroll debit.

    Body is strictly validated via ProposedPickInput — wrong types or missing
    keys return 422 instead of silently corrupting a prediction row.
    """
    ok = db.save_prediction(
        event_id=pick.event_id,
        sport_key=pick.sport_key,
        home_team=pick.home_team,
        away_team=pick.away_team,
        market=pick.market,
        selection=pick.selection_code,
        model_prob=pick.model_prob,
        best_odds=pick.best_odds,
        best_book=pick.best_book,
        value_edge=pick.value_edge,
        kelly_stake=pick.kelly_stake,
        lambda_home=pick.lambda_home,
        lambda_away=pick.lambda_away,
        model_type=pick.model_type,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="duplicate (already in DB)")
    return {"ok": True, "placement_status": "proposed",
            "event": f"{pick.home_team} vs {pick.away_team}"}


@router.post("/predictions/{prediction_id}/skip")
@limiter.limit("30/minute")
def skip_prediction(
    request: Request,
    prediction_id: int,
    body: SkipRequest = SkipRequest(),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Skip a proposed pick — the user passed on the recommendation.
    No bankroll movement. Kept in DB for analytics (would-have ROI).
    """
    try:
        ok = db.skip_prediction(prediction_id, reason=body.reason or "user_skipped")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Prediction {prediction_id} not found")
    return {"prediction_id": prediction_id, "placement_status": "skipped",
            "reason": body.reason}


@router.post("/predictions/{prediction_id}/unskip")
@limiter.limit("30/minute")
def unskip_prediction(
    request: Request,
    prediction_id: int,
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Revert a skipped pick back to 'proposed'. Useful when the user clicked
    skip by mistake. Refused for picks already confirmed.
    """
    try:
        ok = db.unskip_prediction(prediction_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Prediction {prediction_id} not found")
    return {"prediction_id": prediction_id, "placement_status": "proposed"}


@router.get("/predictions/proposed", response_model=list[PredictionRow])
def proposed_predictions(
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[PredictionRow]:
    """Picks the bot has proposed and the user hasn't acted on yet."""
    return [PredictionRow(**r) for r in db.get_proposed_predictions()]


@router.get("/predictions/skipped", response_model=list[PredictionRow])
def skipped_predictions(
    limit: int = Query(default=20, ge=1, le=100),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[PredictionRow]:
    """Recently skipped picks — supports the 'undo skip' recovery UI."""
    return [PredictionRow(**r) for r in db.get_skipped_predictions(limit=limit)]


@router.get("/predictions/pending", response_model=list[PredictionRow])
def pending_predictions(
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> list[PredictionRow]:
    """Confirmed bets awaiting match outcome (the user is on the hook for these)."""
    return [PredictionRow(**r) for r in db.get_confirmed_pending()]


@router.post("/predictions/resolve")
def resolve(
    days_from: int = Query(default=3, ge=1, le=3),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    s = load_settings()
    client = OddsAPIClient(s.odds_api_key)
    return resolve_pending(db, client, days_from=days_from)
