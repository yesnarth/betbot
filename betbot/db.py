"""
Database access layer (PostgreSQL only).

Built on SQLAlchemy 2.0. The schema is owned by Alembic — `_init_schema` only
runs Base.metadata.create_all when the schema is genuinely empty (greenfield
test envs). In production / Docker, `alembic upgrade head` is the source of
truth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
        reliability: float | None = None,
        enforce_funds: bool = True,  # noqa: ARG002 — kept for caller-API stability
    ) -> bool:
        """
        Save a prediction as PROPOSED. The bankroll is **NOT** debited at this
        stage — that only happens when the user explicitly confirms placement
        via `confirm_prediction_placed()`. This matches the real workflow :
        the bot can't place real bets (the user's bookmakers don't expose
        APIs), so picks live in 'proposed' state until the user does the
        actual click at the bookmaker site and comes back to confirm.

        Returns True if inserted, False if a row with the same
        (event_id, market, selection) already exists.

        The `enforce_funds` argument is **deprecated** — under the advisor
        workflow, funds are checked at confirm time, not save time. Kept in
        the signature so callers (worker, MCP) don't break ; will be removed
        in a future major version.
        """
        del enforce_funds  # explicitly silence the unused-arg warning
        try:
            with session_scope() as s:
                # Idempotency check on (event_id, market, selection)
                exists = s.execute(
                    select(Prediction.id).where(
                        Prediction.event_id == event_id,
                        Prediction.market == market,
                        Prediction.selection == selection,
                    )
                ).first()
                if exists:
                    return False

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
                    reliability=reliability,
                    placement_status="proposed",
                    placement_status_at=_utcnow_iso(),
                )
                s.add(pred)
                return True
        except Exception as exc:
            logger.error("Erreur sauvegarde prédiction : %s", exc)
            return False

    def get_pending_predictions(self) -> list[dict]:
        """Confirmed bets awaiting outcome — what the resolver should fetch
        scores for. Used to be 'all rows with result IS NULL' but that wasted
        quota on proposed/skipped picks the user never bet on. The semantic
        meaning of 'pending' in this codebase is now strictly 'confirmed and
        not resolved'. For the proposed-validation queue, use
        `get_proposed_predictions()`."""
        with session_scope() as s:
            rows = s.execute(
                select(Prediction)
                .where(Prediction.result.is_(None),
                       Prediction.placement_status == "confirmed")
                .order_by(Prediction.created_at)
            ).scalars().all()
            return [_to_dict(r) for r in rows]

    def get_proposed_predictions(self) -> list[dict]:
        """Picks the bot has proposed but the user hasn't acted on yet.
        Newest first so the dashboard shows the most recent recommendations
        at the top of the validation queue."""
        with session_scope() as s:
            rows = s.execute(
                select(Prediction)
                .where(Prediction.placement_status == "proposed",
                       Prediction.result.is_(None))
                .order_by(Prediction.created_at.desc())
            ).scalars().all()
            return [_to_dict(r) for r in rows]

    def get_skipped_predictions(self, limit: int = 20) -> list[dict]:
        """Recently skipped picks — for the 'I clicked skip by mistake'
        recovery UI. Includes both unresolved (where unskip still makes
        sense) and resolved skipped picks (analytics on what we passed on).
        Most-recently skipped first."""
        with session_scope() as s:
            rows = s.execute(
                select(Prediction)
                .where(Prediction.placement_status == "skipped")
                .order_by(Prediction.placement_status_at.desc())
                .limit(limit)
            ).scalars().all()
            return [_to_dict(r) for r in rows]

    def get_confirmed_pending(self) -> list[dict]:
        """Confirmed bets whose match hasn't resolved yet — what the user
        is currently 'on the hook' for."""
        with session_scope() as s:
            rows = s.execute(
                select(Prediction)
                .where(Prediction.placement_status == "confirmed",
                       Prediction.result.is_(None))
                .order_by(Prediction.created_at)
            ).scalars().all()
            return [_to_dict(r) for r in rows]

    def auto_skip_expired_proposed(self, max_age_hours: int = 36) -> int:
        """
        Auto-skip 'proposed' predictions that the user never acted on within
        `max_age_hours` of their creation. We use creation age as the
        expiry signal because we don't always have a reliable kickoff
        timestamp on the prediction row (commence_time is stored at scan
        time but not always on the Prediction).

        Default 36h covers the worst case : a pick proposed at the 20h scan
        on Friday for a Sunday afternoon match (~42h) — but for that case
        the user has Saturday + Sunday morning to react. Most picks are
        for matches within 24h of scan, so 36h is a safe net.

        Returns the count of rows transitioned to 'skipped'.
        """
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()
        with session_scope() as s:
            rows = s.execute(
                select(Prediction).where(
                    Prediction.placement_status == "proposed",
                    Prediction.created_at < cutoff_iso,
                )
            ).scalars().all()
            n = 0
            for row in rows:
                row.placement_status = "skipped"
                row.placement_status_at = _utcnow_iso()
                n += 1
            return n

    def update_result(
        self,
        event_id: str,
        market: str,
        selection: str,
        result: str,
        closing_odds: float | None = None,
    ) -> None:
        """
        Mark a prediction's outcome AND post the matching bankroll movement
        IN A SINGLE TRANSACTION.

        Behavior:
          - result="win"  → bet_won  (credit stake × odds)
          - result="loss" → bet_lost (zero-amount audit entry, stake already debited)
          - result="void" → bet_void (refund the original stake)

        Both writes commit together or roll back together. A Postgres restart
        between the two CANNOT leave a "resolved but uncredited" prediction.

        Idempotent: re-resolving the same prediction is a no-op.
        """
        from betbot.bankroll import _acquire_ledger_lock, _append

        with session_scope() as s:
            # Acquire a row-level lock with SELECT ... FOR UPDATE so we cannot
            # see this prediction's NULL result twice from two concurrent
            # resolver runs (e.g. the daily 04h cron firing while a user
            # clicks "Résoudre les paris terminés" in the dashboard). Without
            # the lock, both transactions would observe result IS NULL,
            # both would INSERT a bet_won ledger entry, double-crediting
            # the bankroll.
            row = s.execute(
                select(Prediction)
                .where(
                    Prediction.event_id == event_id,
                    Prediction.market == market,
                    Prediction.selection == selection,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                logger.debug("update_result: no prediction matching %s/%s/%s",
                             event_id, market, selection)
                return

            # Idempotency: don't double-credit — checked AFTER acquiring the
            # row lock so the test is atomic vs concurrent writers.
            if row.result is not None:
                return

            stake = row.kelly_stake
            odds = row.best_odds
            pred_id = row.id

            row.result = result
            row.closing_odds = closing_odds
            row.resolved_at = _utcnow_iso()

            # Bankroll movement ONLY for predictions the user actually
            # confirmed at the bookmaker. 'proposed' or 'skipped' picks
            # never debited the bankroll, so they shouldn't credit it
            # either when their match resolves. We still record the
            # `result` field for analytics ("would-have ROI" on skipped
            # picks, raw model accuracy on proposed picks).
            is_money_at_stake = (
                stake > 0 and row.placement_status == "confirmed"
            )
            if is_money_at_stake:
                if result == "win":
                    payout = stake * odds
                    _append(s, "bet_won", payout, prediction_id=pred_id,
                            note=f"resolved {row.home_team} vs {row.away_team}")
                elif result == "loss":
                    _append(s, "bet_lost", 0.0, prediction_id=pred_id,
                            note=f"resolved {row.home_team} vs {row.away_team}")
                elif result == "void":
                    _append(s, "bet_void", stake, prediction_id=pred_id,
                            note=f"voided {row.home_team} vs {row.away_team}")

    # ------------------------------------------------------------------
    # Stats / ROI
    # ------------------------------------------------------------------

    def get_roi_stats(self, days: int = 30, only_placed: bool = True) -> dict:
        """
        Return ROI + CLV stats for the last N days.

        only_placed=True (default): include only predictions the user actually
        confirmed (placement_status='confirmed'). This is the honest metric —
        measuring performance on bets the user actually took, not just
        recommendations.

        Set only_placed=False for the analytics counterpart : measures the
        model's accuracy across ALL picks (proposed + confirmed + skipped),
        useful to compare "what the bot would have done if you'd taken
        every pick" vs "what you actually got".

        CLV (Closing Line Value) is the gold standard skill metric.
        """
        from datetime import timedelta
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with session_scope() as s:
            stmt = select(
                Prediction.result,
                Prediction.kelly_stake,
                Prediction.best_odds,
                Prediction.closing_odds,
                Prediction.value_edge,
            ).where(
                Prediction.result.is_not(None),
                Prediction.created_at >= cutoff_iso,
            )
            if only_placed:
                stmt = stmt.where(Prediction.placement_status == "confirmed")
            preds = s.execute(stmt).all()

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

    def confirm_prediction_placed(
        self,
        prediction_id: int,
        bookmaker: str | None = None,
        unconfirm: bool = False,
    ) -> bool:
        """
        Confirm that the user actually placed this bet at their bookmaker.

        This is when the bankroll is debited : we insert the matching
        bet_placed ledger entry and acquire the advisory lock atomically.
        Pre-flight guard checks (stop-loss, daily cap, exposure) run here too.

        Idempotent: re-confirming a 'confirmed' row is a no-op.
        Set unconfirm=True to revert (mistake correction): re-credits the
        stake and flips placement_status back to 'proposed'.

        Returns True on success, False if prediction not found.
        Raises InsufficientFundsError or GuardViolation when applicable.
        """
        from betbot.bankroll import (
            InsufficientFundsError,
            _acquire_ledger_lock,
            _append,
            _state_inside_lock,
        )
        from betbot.guards import GuardViolation, check_can_place_bet

        with session_scope() as s:
            row = s.get(Prediction, prediction_id)
            if row is None:
                return False

            if unconfirm:
                # Mistake correction : refund the stake and revert to 'proposed'
                if row.placement_status != "confirmed":
                    return True  # nothing to revert
                if row.kelly_stake > 0:
                    _acquire_ledger_lock(s)
                    _append(
                        s, "adjustment", row.kelly_stake,
                        prediction_id=row.id,
                        note=f"unconfirm placement (mistake correction)",
                    )
                row.placement_status = "proposed"
                row.placement_status_at = _utcnow_iso()
                row.placed_at = None
                row.placed_bookmaker = None
                return True

            # Already confirmed — idempotent no-op
            if row.placement_status == "confirmed":
                return True

            # Skipped predictions can't be confirmed without unskipping first
            if row.placement_status == "skipped":
                raise ValueError("Cannot confirm a skipped prediction")

            # Pre-flight guards
            if row.kelly_stake > 0:
                try:
                    check_can_place_bet(row.kelly_stake)
                except GuardViolation as exc:
                    logger.warning("Guard blocked confirm (%s) : %s",
                                   prediction_id, exc)
                    raise

                _acquire_ledger_lock(s)
                balance, _ = _state_inside_lock(s)
                if row.kelly_stake > balance:
                    raise InsufficientFundsError(
                        f"Cannot confirm bet of {row.kelly_stake:.2f} — "
                        f"only {balance:.2f} on hand"
                    )
                _append(
                    s, "bet_placed", -row.kelly_stake,
                    prediction_id=row.id,
                    note=f"{row.home_team} vs {row.away_team} [{row.selection}]",
                )

            row.placement_status = "confirmed"
            row.placement_status_at = _utcnow_iso()
            row.placed_at = _utcnow_iso()
            row.placed_bookmaker = bookmaker
            return True

    def skip_prediction(
        self,
        prediction_id: int,
        reason: str = "user_skipped",
    ) -> bool:
        """
        Mark a proposed prediction as skipped — bankroll untouched.

        Used when the user decides not to place a recommended bet, OR when
        the auto-skip cron picks up a 'proposed' row past kickoff. The
        prediction is NOT deleted (we keep it for analytics: which picks
        the user passed on, kept for would-have ROI tracking).

        Idempotent for already-skipped rows. Returns True on success,
        False if prediction not found, raises ValueError for confirmed bets.
        """
        with session_scope() as s:
            row = s.get(Prediction, prediction_id)
            if row is None:
                return False
            if row.placement_status == "skipped":
                return True  # idempotent
            if row.placement_status == "confirmed":
                raise ValueError(
                    "Cannot skip a confirmed prediction — use unconfirm first"
                )
            row.placement_status = "skipped"
            row.placement_status_at = _utcnow_iso()
            return True

    def unskip_prediction(self, prediction_id: int) -> bool:
        """
        Revert a skipped prediction back to 'proposed' — accident recovery.

        If the user clicked skip by mistake and the match hasn't been resolved
        yet, this brings the pick back into the validation queue. Idempotent
        for already-proposed rows. Returns False if not found, raises
        ValueError if the prediction is already confirmed (shouldn't happen
        since confirm and skip are mutually exclusive).
        """
        with session_scope() as s:
            row = s.get(Prediction, prediction_id)
            if row is None:
                return False
            if row.placement_status == "proposed":
                return True  # idempotent
            if row.placement_status == "confirmed":
                raise ValueError(
                    "Cannot unskip a confirmed prediction — it was never skipped"
                )
            row.placement_status = "proposed"
            row.placement_status_at = _utcnow_iso()
            return True

    def list_agent_runs(self, limit: int = 50, offset: int = 0,
                        trigger: str | None = None) -> list[dict]:
        """Return agent runs newest first, optionally filtered by trigger
        ('api' | 'dashboard' | 'scheduled')."""
        with session_scope() as s:
            stmt = select(AgentRun).order_by(AgentRun.id.desc())
            if trigger:
                stmt = stmt.where(AgentRun.trigger == trigger)
            stmt = stmt.limit(limit).offset(offset)
            rows = s.execute(stmt).scalars().all()
            return [_to_dict(r) for r in rows]

    def get_agent_run(self, run_id: int) -> dict | None:
        """Return a single agent run with its full reasoning trace."""
        with session_scope() as s:
            row = s.get(AgentRun, run_id)
            return _to_dict(row) if row else None

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
