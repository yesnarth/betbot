"""
Cross-bookmaker arbitrage detector.

Concept: when several bookmakers price the same event differently, you can
sometimes back EVERY outcome at the BEST available odds for each, and lock
in a guaranteed profit regardless of the result.

The arbitrage indicator is the sum of inverse best odds across outcomes:
    arb_index = sum(1/best_odds_i)

  - arb_index < 1.0  → arbitrage exists; profit = (1 / arb_index - 1) × stake
  - arb_index ≥ 1.0  → no arb; bookmaker margin (vig) absorbs the spread

Real arbs in liquid soccer markets are RARE (typically 0.1-1% profit when
they exist) and require fast execution before bookies adjust their lines.
This module is for detection — placement is still manual.

Caveats clearly explained in the API response:
  - Limit-stake risk: bookies cap accounts that arb regularly
  - Bookmaker insolvency: rare but real
  - Same-game arbs are sometimes voided as related contingencies
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger("betbot.arbitrage")


@dataclass
class ArbLeg:
    outcome_name: str
    odds: float
    bookmaker: str
    stake_share: float = 0.0   # fraction of total stake to put on this leg


@dataclass
class ArbOpportunity:
    event_id: str
    home_team: str
    away_team: str
    sport_key: str
    market: str
    arb_index: float           # sum(1/odds) — must be < 1.0 for arb
    profit_pct: float          # guaranteed return % if executed correctly
    legs: list[ArbLeg]
    notes: list[str] = field(default_factory=list)


def _best_odds_per_outcome(event: dict, market_key: str = "h2h") -> dict[str, ArbLeg]:
    """Aggregate odds across all bookmakers, keeping the best per outcome.

    Returns {outcome_name: ArbLeg with the best odds + the bookmaker offering it}.
    """
    best: dict[str, ArbLeg] = {}
    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for o in mkt.get("outcomes", []):
                name = o.get("name", "")
                try:
                    price = float(o["price"])
                except (KeyError, ValueError):
                    continue
                if price <= 1.0:
                    continue
                if name not in best or price > best[name].odds:
                    best[name] = ArbLeg(
                        outcome_name=name, odds=price,
                        bookmaker=bm.get("title", bm.get("key", "?")),
                    )
    return best


def find_arb(event: dict, market_key: str = "h2h",
             expected_outcomes: int = 3) -> ArbOpportunity | None:
    """
    Scan one event and report an arbitrage opportunity if the cross-bookie
    best odds yield arb_index < 1.0.

    expected_outcomes:
      - h2h soccer: 3 (home, draw, away)
      - h2h tennis/basket: 2
    Set this to validate that all needed outcomes are present.
    """
    legs_by_outcome = _best_odds_per_outcome(event, market_key)
    if len(legs_by_outcome) < expected_outcomes:
        return None

    arb_index = sum(1.0 / leg.odds for leg in legs_by_outcome.values())
    if arb_index >= 1.0:
        return None  # no arb

    # Compute the stake split that yields equal payout regardless of outcome.
    # If total stake = S, then stake_i = S * (1/odds_i) / arb_index.
    legs: list[ArbLeg] = []
    for leg in legs_by_outcome.values():
        leg.stake_share = round((1.0 / leg.odds) / arb_index, 4)
        legs.append(leg)

    profit_pct = round((1.0 / arb_index - 1.0) * 100, 3)

    notes = []
    if profit_pct < 0.5:
        notes.append("Marge fine (<0.5%) — sensible aux frais et au mouvement de cotes.")
    if len({leg.bookmaker for leg in legs}) < expected_outcomes:
        notes.append("Plusieurs jambes chez le même bookmaker — vérifier la liquidité.")

    return ArbOpportunity(
        event_id=event.get("id", ""),
        home_team=event.get("home_team", ""),
        away_team=event.get("away_team", ""),
        sport_key=event.get("sport_key", ""),
        market=market_key,
        arb_index=round(arb_index, 4),
        profit_pct=profit_pct,
        legs=legs,
        notes=notes,
    )


def scan_arbs(
    events_by_sport: dict[str, list[dict]],
    market_key: str = "h2h",
) -> list[ArbOpportunity]:
    """
    Scan every event across multiple sports and return only those with an
    arbitrage opportunity, sorted by descending profit %.

    Empirical note: in liquid EU soccer h2h, you'll find an arb on roughly
    1 event in 200-1000. Most of these are tiny (<0.5%) and short-lived.
    """
    out: list[ArbOpportunity] = []
    for sport_key, events in events_by_sport.items():
        # Tennis is 2-outcome, soccer 3-outcome
        expected = 2 if sport_key.startswith(("tennis_", "basketball_")) else 3
        for event in events:
            event = {**event, "sport_key": sport_key}
            arb = find_arb(event, market_key=market_key, expected_outcomes=expected)
            if arb is not None:
                out.append(arb)
    out.sort(key=lambda a: a.profit_pct, reverse=True)
    logger.info("Arbitrage scan : %d opportunités trouvées", len(out))
    return out


def arb_to_dict(arb: ArbOpportunity) -> dict:
    """JSON-friendly serialization for the API."""
    return {
        "event_id": arb.event_id,
        "home_team": arb.home_team,
        "away_team": arb.away_team,
        "sport_key": arb.sport_key,
        "market": arb.market,
        "arb_index": arb.arb_index,
        "profit_pct": arb.profit_pct,
        "legs": [
            {
                "outcome_name": leg.outcome_name,
                "odds": leg.odds,
                "bookmaker": leg.bookmaker,
                "stake_share": leg.stake_share,
            }
            for leg in arb.legs
        ],
        "notes": arb.notes,
    }
