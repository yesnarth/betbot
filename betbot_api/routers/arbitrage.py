"""Cross-bookmaker arbitrage scanner endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from betbot.api import OddsAPIClient
from betbot.config import load_settings
from betbot_api.auth import require_auth
from betbot_api.deps import limiter

router = APIRouter(tags=["arbitrage"])


@router.get("/arbitrage")
@limiter.limit("10/minute")
def arbitrage_scan(
    request: Request,
    sport_key: str | None = Query(default=None,
                                  description="Filter to one sport, else scan all"),
    today_only: bool = Query(default=True),
    _: str = Depends(require_auth),
) -> dict:
    """
    Detect cross-bookmaker arbitrage opportunities — guaranteed profit if you
    can place all legs at the listed odds. Real arbs are RARE in liquid markets
    (~1 in 200-1000 events) and usually < 1% profit.
    """
    from betbot.arbitrage import scan_arbs, arb_to_dict
    from betbot.shared import filter_upcoming_today
    s = load_settings()
    odds_client = OddsAPIClient(s.odds_api_key)

    if sport_key:
        all_events = {sport_key: odds_client.get_events_with_odds(sport_key)}
    else:
        all_events = odds_client.fetch_all_sports()

    events_by_sport: dict[str, list[dict]] = {}
    for sk, ev in all_events.items():
        kept = filter_upcoming_today(ev, s.min_before_kickoff) if today_only else ev
        if kept:
            events_by_sport[sk] = kept

    arbs = scan_arbs(events_by_sport, market_key="h2h")
    return {
        "n_opportunities": len(arbs),
        "arbs": [arb_to_dict(a) for a in arbs],
        "notes": [
            "Real arbs are usually < 1% profit and require fast execution.",
            "Bookmakers may void your bet or limit your account if you arb regularly.",
            "Always verify odds on the bookmaker site before placing — odds move.",
        ],
    }
