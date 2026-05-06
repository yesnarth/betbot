"""
Database access layer (PostgreSQL only).

Built on SQLAlchemy 2.0. The schema is owned by Alembic — `_init_schema` only
runs Base.metadata.create_all when the schema is genuinely empty (greenfield
test envs). In production / Docker, `alembic upgrade head` is the source of
truth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import inspect, select, func
from sqlalchemy.engine import Engine

from betbot.database import Base, get_engine, reset_engine, session_scope
from betbot.orm_models import (
    AgentRun,
    LeagueAverage,
    Prediction,
    TeamStat,
)

logger = logging.getLogger("betbot.db")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """
    Thin façade over the PostgreSQL engine.

    Accepts an optional SQLAlchemy URL; otherwise reads DATABASE_URL from env.
    Raises DatabaseConfigurationError if neither is set or the dialect isn't
    PostgreSQL.
    """

    def __init__(self, url: str | None = None):
        # Reset the cached engine if the URL differs from a previous build
        # (matters for tests that swap DATABASE_URL between cases).
        engine = get_engine(url) if url else get_engine()
        if url and engine.url.render_as_string(hide_password=True) != url:
            reset_engine()
            engine = get_engine(url)
        self._engine: Engine = engine
        self._init_schema()

    def _init_schema(self) -> None:
        """
        Create tables only if the schema is empty. In production we rely on
        Alembic; this is a safety net for fresh test/dev environments where
        no migration has been run yet.
        """
        ins = inspect(self._engine)
        if not ins.has_table("predictions"):
            Base.metadata.create_all(self._engine)
            logger.info("Schéma initialisé via Base.metadata (greenfield) : %s",
                        self._engine.url.render_as_string(hide_password=True))

    # ------------------------------------------------------------------
    # Team stats
    # ------------------------------------------------------------------

    def upsert_team_stats(
        self,
        team_name: str,
        sport_key: str,
        league_code: str,
        attack_home: float,
        defense_home: float,
        attack_away: float,
        defense_away: float,
        matches_analyzed: int,
    ) -> None:
        with session_scope() as s:
            existing = s.get(TeamStat, (team_name, sport_key))
            if existing:
                existing.league_code = league_code
                existing.updated_at = _utcnow_iso()
                existing.attack_home = attack_home
                existing.defense_home = defense_home
                existing.attack_away = attack_away
                existing.defense_away = defense_away
                existing.matches_analyzed = matches_analyzed
            else:
                s.add(TeamStat(
                    team_name=team_name,
                    sport_key=sport_key,
                    league_code=league_code,
                    updated_at=_utcnow_iso(),
                    attack_home=attack_home,
                    defense_home=defense_home,
                    attack_away=attack_away,
                    defense_away=defense_away,
                    matches_analyzed=matches_analyzed,
                ))

    def get_team_stats(self, team_name: str, sport_key: str) -> dict | None:
        with session_scope() as s:
            row = s.get(TeamStat, (team_name, sport_key))
            return _to_dict(row) if row else None

    def get_all_team_stats_for_league(self, sport_key: str) -> list[dict]:
        with session_scope() as s:
            rows = s.execute(
                select(TeamStat).where(TeamStat.sport_key == sport_key)
            ).scalars().all()
            return [_to_dict(r) for r in rows]

    def update_team_enrichment(
        self,
        team_name: str,
        sport_key: str,
        elo_rating: float | None = None,
        xg_for: float | None = None,
        xg_against: float | None = None,
        npxg_for: float | None = None,
        npxg_against: float | None = None,
        xpts_per_match: float | None = None,
        sources_updated_at: str | None = None,
    ) -> bool:
        """
        Patch only the enrichment columns of an existing team_stats row.
        Returns True if updated, False if the row doesn't exist.
        """
        with session_scope() as s:
            row = s.get(TeamStat, (team_name, sport_key))
            if row is None:
                return False
            if elo_rating is not None:
                row.elo_rating = elo_rating
            if xg_for is not None:
                row.xg_for = xg_for
            if xg_against is not None:
                row.xg_against = xg_against
            if npxg_for is not None:
                row.npxg_for = npxg_for
            if npxg_against is not None:
                row.npxg_against = npxg_against
            if xpts_per_match is not None:
                row.xpts_per_match = xpts_per_match
            if sources_updated_at is not None:
                row.sources_updated_at = sources_updated_at
            return True

    # ------------------------------------------------------------------
    # League averages
    # ------------------------------------------------------------------

    def upsert_league_averages(
        self, sport_key: str, home_avg: float, away_avg: float, n_matches: int
    ) -> None:
        with session_scope() as s:
            existing = s.get(LeagueAverage, sport_key)
            if existing:
                existing.home_avg = home_avg
                existing.away_avg = away_avg
                existing.n_matches = n_matches
                existing.updated_at = _utcnow_iso()
            else:
                s.add(LeagueAverage(
                    sport_key=sport_key,
                    home_avg=home_avg,
                    away_avg=away_avg,
                    n_matches=n_matches,
                    updated_at=_utcnow_iso(),
                ))

    def get_league_averages(self, sport_key: str) -> tuple[float, float] | None:
        with session_scope() as s:
            row = s.get(LeagueAverage, sport_key)
            return (row.home_avg, row.away_avg) if row else None

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def save_prediction(
        self,
        event_id: str,
        sport_key: str,
        home_team: str,
        away_team: str,
        market: str,
        selection: str,
        model_prob: float,
        best_odds: float,
        best_book: str,
        value_edge: float,
        kelly_stake: float,
        lambda_home: float | None = None,
        lambda_away: float | None = None,
        model_type: str = "poisson",
        enforce_funds: bool = True,
    ) -> bool:
        """
        Save a prediction AND immobilize its Kelly stake atomically.

        Both the Prediction row and the bankroll_ledger row are inserted in a
        SINGLE transaction. Either both succeed, or neither — no fantôme rows.

        Returns True if inserted, False if duplicate. Raises
        InsufficientFundsError if the bankroll's available balance is below the
        stake (set enforce_funds=False to bypass, useful for backtests).
        """
        from betbot.bankroll import (
            InsufficientFundsError,
            _acquire_ledger_lock,
            _append,
            _state_inside_lock,
        )
        from betbot.guards import GuardViolation, check_can_place_bet

        # Pre-flight responsible-betting guards (stop-loss, daily cap, exposure)
        if kelly_stake > 0 and enforce_funds:
            try:
                check_can_place_bet(kelly_stake)
            except GuardViolation as exc:
                logger.warning("Guard blocked prediction (%s vs %s) : %s",
                               home_team, away_team, exc)
                raise

        try:
            with session_scope() as s:
                # 1. Idempotency check on (event_id, market, selection)
                exists = s.execute(
                    select(Prediction.id).where(
                        Prediction.event_id == event_id,
                        Prediction.market == market,
                        Prediction.selection == selection,
                    )
                ).first()
                if exists:
                    return False

                # 2. Acquire ledger lock — no concurrent placement can race past here
                if kelly_stake > 0:
                    _acquire_ledger_lock(s)
                    if enforce_funds:
                        balance, _ = _state_inside_lock(s)
                        if kelly_stake > balance:
                            raise InsufficientFundsError(
                                f"Cannot place bet of {kelly_stake:.2f} — "
                                f"only {balance:.2f} on hand"
                            )

                # 3. Insert the prediction
                pred = Prediction(
                    created_at=_utcnow_iso(),
                    event_id=event_id,
                    sport_key=sport_key,
                    home_team=home_team,
                    away_team=away_team,
                    market=market,
                    selection=selection,
                    model_prob=model_prob,
                    best_odds=best_odds,
                    best_book=best_book,
                    value_edge=value_edge,
                    kelly_stake=kelly_stake,
                    lambda_home=lambda_home,
                    lambda_away=lambda_away,
                    model_type=model_type,
                )
                s.add(pred)
                s.flush()  # populate pred.id

                # 4. Insert the matching ledger row in the SAME transaction
                if kelly_stake > 0:
                    _append(
                        s, "bet_placed", -kelly_stake,
                        prediction_id=pred.id,
                        note=f"{home_team} vs {away_team} [{selection}]",
                    )
                return True
        except InsufficientFundsError:
            raise  # bubble up; session_scope already rolled back
        except Exception as exc:
            logger.error("Erreur sauvegarde prédiction : %s", exc)
            return False

    def get_pending_predictions(self) -> list[dict]:
        with session_scope() as s:
            rows = s.execute(
                select(Prediction)
                .where(Prediction.result.is_(None))
                .order_by(Prediction.created_at)
            ).scalars().all()
            return [_to_dict(r) for r in rows]

    def update_result(
        self,
        event_id: str,
        market: str,
        selection: str,
        result: str,
        closing_odds: float | None = None,
    ) -> None:
        """Mark a prediction's outcome AND trigger the matching bankroll movement.

        - result="win"  → bet_won  (credit stake × odds)
        - result="loss" → bet_lost (zero-amount audit entry, stake already debited)
        - result="void" → bet_void (refund the original stake)
        """
        from betbot.bankroll import (
            record_bet_lost,
            record_bet_void,
            record_bet_won,
        )
        # 1. Update the prediction row + capture stake/odds for the bankroll hook
        with session_scope() as s:
            row = s.execute(
                select(Prediction).where(
                    Prediction.event_id == event_id,
                    Prediction.market == market,
                    Prediction.selection == selection,
                )
            ).scalar_one_or_none()
            if row is None:
                return
            # Idempotency: don't double-credit if we resolve the same row twice
            already_resolved = row.result is not None
            row.result = result
            row.closing_odds = closing_odds
            row.resolved_at = _utcnow_iso()
            pred_id = row.id
            stake = row.kelly_stake
            odds = row.best_odds

        if already_resolved:
            return

        # 2. Bankroll movement (outside the prediction transaction)
        try:
            if result == "win":
                record_bet_won(pred_id, stake, odds)
            elif result == "loss":
                record_bet_lost(pred_id)
            elif result == "void":
                record_bet_void(pred_id, stake)
        except Exception as exc:
            logger.error("Bankroll hook failed for pred #%s : %s", pred_id, exc)

    # ------------------------------------------------------------------
    # Stats / ROI
    # ------------------------------------------------------------------

    def get_roi_stats(self, days: int = 30) -> dict:
        """
        Return ROI + CLV stats for the last N days.

        CLV (Closing Line Value) is the gold standard skill metric: a
        consistently positive average CLV is a stronger sign of edge than
        short-term ROI, which is noise-dominated for small samples.
        """
        from datetime import timedelta
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with session_scope() as s:
            preds = s.execute(
                select(
                    Prediction.result,
                    Prediction.kelly_stake,
                    Prediction.best_odds,
                    Prediction.closing_odds,
                    Prediction.value_edge,
                ).where(
                    Prediction.result.is_not(None),
                    Prediction.created_at >= cutoff_iso,
                )
            ).all()

        if not preds:
            return {
                "n_bets": 0, "n_wins": 0, "hit_rate": 0.0, "roi": 0.0, "avg_edge": 0.0,
                "n_with_clv": 0, "avg_clv_pct": 0.0, "positive_clv_share": 0.0,
            }

        n = len(preds)
        wins = [p for p in preds if p.result == "win"]
        staked = sum(p.kelly_stake for p in preds)
        returned = sum(p.kelly_stake * p.best_odds for p in wins)
        avg_edge = sum(p.value_edge for p in preds) / n

        # CLV — only on bets where we managed to snapshot the closing odds
        clv_preds = [p for p in preds if p.closing_odds and p.closing_odds > 1.0]
        if clv_preds:
            clvs = [
                (p.best_odds / p.closing_odds - 1.0) * 100
                for p in clv_preds
            ]
            avg_clv = sum(clvs) / len(clvs)
            pos_share = sum(1 for c in clvs if c > 0) / len(clvs) * 100
        else:
            avg_clv = 0.0
            pos_share = 0.0

        return {
            "n_bets": n,
            "n_wins": len(wins),
            "hit_rate": round(len(wins) / n * 100, 1),
            "roi": round((returned - staked) / staked * 100, 1) if staked > 0 else 0.0,
            "avg_edge": round(avg_edge * 100, 1),
            "n_with_clv": len(clv_preds),
            "avg_clv_pct": round(avg_clv, 2),
            "positive_clv_share": round(pos_share, 1),
        }

    # ------------------------------------------------------------------
    # Agent runs (NEW — for AI-agent audit trail)
    # ------------------------------------------------------------------

    def save_agent_run(
        self,
        trigger: str,
        filters: dict,
        model: str,
        reasoning: str | None,
        picks: list,
        n_tool_calls: int,
        duration_ms: int | None,
        cost_usd: float | None,
        status: str = "ok",
        error: str | None = None,
    ) -> int:
        with session_scope() as s:
            run = AgentRun(
                created_at=_utcnow_iso(),
                trigger=trigger,
                filters=filters,
                model=model,
                reasoning=reasoning,
                picks=picks,
                n_tool_calls=n_tool_calls,
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                status=status,
                error=error,
            )
            s.add(run)
            s.flush()
            return run.id


def _to_dict(row) -> dict:
    """ORM instance → plain dict (preserves the raw-SQL API consumers expect)."""
    if row is None:
        return {}
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
