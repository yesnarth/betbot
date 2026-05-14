"""Basketball pace + ORtg/DRtg endpoints — status, manual refresh, prediction."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from betbot_api.auth import require_auth
from betbot_api.deps import limiter

router = APIRouter(prefix="/basketball", tags=["basketball"])


@router.get("/status")
def basketball_status(_: str = Depends(require_auth)) -> dict:
    """Show the state of the NBA team stats (n_teams, top by net rating)."""
    from betbot.basketball_model import status
    return status()


@router.post("/refresh")
@limiter.limit("2/minute")
def basketball_refresh(
    request: Request,
    season: int | None = Query(default=None),
    _: str = Depends(require_auth),
) -> dict:
    """Force a refresh of NBA team stats from basketball-reference."""
    from betbot.basketball_bootstrap import refresh_stats
    return refresh_stats(season_year=season)


@router.get("/predict")
def basketball_predict_endpoint(
    home: str = Query(...),
    away: str = Query(...),
    league: str = Query(default="nba"),
    _: str = Depends(require_auth),
) -> dict:
    """Quick preview of a pace-based basketball prediction."""
    from betbot.basketball_model import predict
    p = predict(home, away, league=league)
    if p is None:
        return {"error": "team not found"}
    return p.__dict__
