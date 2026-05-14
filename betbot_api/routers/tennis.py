"""Tennis ELO endpoints — status, manual refresh, on-demand prediction."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from betbot_api.auth import require_auth
from betbot_api.deps import limiter

router = APIRouter(prefix="/tennis", tags=["tennis"])


@router.get("/status")
def tennis_status(_: str = Depends(require_auth)) -> dict:
    """Show the state of the tennis ELO ratings (n_players, top, last match date)."""
    from betbot.tennis_model import status
    return status()


@router.post("/refresh")
@limiter.limit("2/minute")
def tennis_refresh(
    request: Request,
    tour: str = Query(default="atp", description="atp | wta | both"),
    _: str = Depends(require_auth),
) -> dict:
    """Force a refresh of tennis ELO from the latest Sackmann CSV data."""
    from betbot.tennis_bootstrap import refresh_ratings
    return refresh_ratings(tour=tour)


@router.get("/predict")
def tennis_predict_endpoint(
    home: str = Query(...),
    away: str = Query(...),
    surface: str = Query(default="Hard"),
    _: str = Depends(require_auth),
) -> dict:
    """Quick preview of an ELO-based tennis prediction for any player pair."""
    from betbot.tennis_model import predict
    p = predict(home, away, surface)
    if p is None:
        return {"error": "player not found in ratings"}
    return p.__dict__
