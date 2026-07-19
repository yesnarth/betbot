"""Runtime settings — lets the dashboard rotate the Odds API key with no restart."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from betbot_api.auth import require_auth
from betbot_api.deps import limiter

router = APIRouter(prefix="/settings", tags=["settings"])


class OddsKeyBody(BaseModel):
    key: str = Field(..., min_length=8, max_length=128)


@router.get("/odds-key")
def get_odds_key(_: str = Depends(require_auth)) -> dict:
    """Masked status of the current Odds key + its live remaining quota."""
    from betbot.api import OddsAPIClient
    from betbot.runtime_config import odds_key_status, get_odds_api_key

    st = odds_key_status()
    remaining = None
    if st["configured"]:
        try:
            remaining = OddsAPIClient(get_odds_api_key()).probe_quota()
        except Exception:
            remaining = None
    return {**st, "remaining": remaining}


@router.post("/odds-key")
@limiter.limit("10/minute")
def set_odds_key(request: Request, body: OddsKeyBody, _: str = Depends(require_auth)) -> dict:
    """Validate a NEW Odds key against the API, then persist it (override).

    The key is probed with force_key=True (bypassing any existing override) so a
    typo is rejected BEFORE it replaces a working key. Once saved, every client
    picks it up on its next request — no restart.
    """
    from betbot.api import OddsAPIClient
    from betbot.runtime_config import set_odds_api_key, odds_key_status

    key = body.key.strip()
    try:
        remaining = OddsAPIClient(key, force_key=True).probe_quota()
    except Exception as exc:
        return {"saved": False, "reason": f"clé injoignable ({exc})"}
    if remaining is None or remaining < 0:
        return {"saved": False, "reason": "clé invalide ou quota indéterminé (probe échoué)"}

    set_odds_api_key(key)
    return {"saved": True, "remaining": remaining, **odds_key_status()}
