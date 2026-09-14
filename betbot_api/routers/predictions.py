"""Predictions endpoints — confirm placement, skip/unskip lifecycle,
proposed/skipped/pending queues, batch resolve. Also `/admin/save-pick-as-proposed`
which writes a pick directly into the validation queue."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from betbot.api import OddsAPIClient
from betbot.config import load_settings
from betbot.db import Database
from betbot.resolver import (
    resolve_pending, resolve_stale_pending, resolve_proposed_picks,
    resolve_proposed_picks_api_football,
)
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
        reliability=pick.reliability,
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
    live = resolve_pending(db, client, days_from=days_from)
    # Fallback: resolve bets too old for the /scores window via football-data.org
    # (so confirmed bets never become permanent 'zombies').
    stale = resolve_stale_pending(db, s.football_data_api_key)
    # Also grade PROPOSED (never-bet) picks whose match has finished — FREE (no
    # Odds quota), NO bankroll effect (update_result only settles confirmed
    # picks). Grades European leagues via football-data + in-season leagues via
    # api-football, so finished picks leave the queue and feed model measurement.
    graded_fd = resolve_proposed_picks(db, s.football_data_api_key, min_age_days=0)
    graded_af = resolve_proposed_picks_api_football(db, min_age_days=0)
    return {
        **live,
        "stale_resolved": stale.get("resolved", 0),
        "proposed_graded": graded_fd.get("resolved", 0) + graded_af.get("resolved", 0),
    }


@router.get("/predictions/blind-parlays")
@limiter.limit("30/minute")
def blind_parlays(
    request: Request,
    target_odds: float = Query(default=100.0, ge=2.0, le=100_000.0),
    n_combos: int = Query(default=3, ge=1, le=10),
    db: Database = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """
    Trois combinés visant ×`target_odds`, assemblés dans le vivier du canal
    aveugle sur les COTES JUSTES DU MODÈLE (1/p). Aucun prix de bookmaker
    n'intervient — ni pour choisir les jambes, ni pour mesurer le multiplicateur.

    Le multiplicateur renvoyé est donc « juste » : celui réellement payé sur le
    ticket sera INFÉRIEUR, le bookmaker prenant sa marge sur chaque jambe.

    `win_prob` vaut exactement 1/`fair_odds` par construction — ce n'est pas une
    coïncidence mais la définition du multiplicateur. Le champ est renvoyé quand
    même : un ×100 présenté sans sa contrepartie (une chance sur cent) serait
    une demi-vérité.
    """
    from betbot.analysis import ValueBet, _sport_key_to_label
    from betbot.blind_parlays import build_blind_parlays

    lignes = [r for r in db.get_proposed_predictions()
              if (r.get("channel") or "") == "modele"]
    picks = [
        ValueBet(
            event_id=r.get("event_id") or "",
            sport_key=r.get("sport_key") or "",
            home_team=r.get("home_team") or "",
            away_team=r.get("away_team") or "",
            league_label=_sport_key_to_label(r.get("sport_key") or ""),
            market=r.get("market") or "",
            selection_code=r.get("selection") or "",
            # Le libellé n'est pas stocké en base (seul le code l'est) : on
            # renvoie le code plutôt que d'inventer un libellé qui pourrait
            # diverger de celui qu'a vu l'utilisateur au moment du pick.
            selection_label=r.get("selection") or "",
            model_prob=float(r.get("model_prob") or 0.0),
            best_odds=0.0, best_book="", value_edge=0.0, kelly_stake=0.0,
            lambda_home=r.get("lambda_home"), lambda_away=r.get("lambda_away"),
            model_type=r.get("model_type") or "blended",
            commence_time=r.get("commence_time") or "",
            channel="modele",
        )
        for r in lignes
    ]
    combos = build_blind_parlays(picks, target_odds=target_odds, n_combos=n_combos)
    return {
        "target_odds": target_odds,
        "n_matches_available": len({p.event_id for p in picks}),
        "parlays": [
            {
                "n_legs": len(c.legs),
                "fair_odds": c.fair_odds,
                "win_prob": c.win_prob,
                "reached_target": c.reached_target,
                "legs": [
                    {
                        "home_team": b.home_team, "away_team": b.away_team,
                        "league": b.league_label, "market": b.market,
                        "selection": b.selection_code,
                        "model_prob": b.model_prob,
                        "fair_odds": round(1.0 / b.model_prob, 2) if b.model_prob else None,
                        "commence_time": b.commence_time,
                    } for b in c.legs
                ],
            } for c in combos
        ],
    }
