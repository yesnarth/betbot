"""
SQLAlchemy 2.0 ORM models. Mirrors the previous raw-SQL schema 1:1 to keep
existing data and tests compatible across the SQLite → PostgreSQL migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from betbot.database import Base


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp string — matches the previous schema."""
    return datetime.now(timezone.utc).isoformat()


class TeamStat(Base):
    __tablename__ = "team_stats"

    team_name: Mapped[str] = mapped_column(String, primary_key=True)
    sport_key: Mapped[str] = mapped_column(String, primary_key=True)
    league_code: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    attack_home: Mapped[float] = mapped_column(Float, nullable=False)
    defense_home: Mapped[float] = mapped_column(Float, nullable=False)
    attack_away: Mapped[float] = mapped_column(Float, nullable=False)
    defense_away: Mapped[float] = mapped_column(Float, nullable=False)
    matches_analyzed: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Enrichment columns — nullable so legacy rows still load ---
    elo_rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    xg_for: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # xG per match
    xg_against: Mapped[Optional[float]] = mapped_column(Float, nullable=True)    # xGA per match
    npxg_for: Mapped[Optional[float]] = mapped_column(Float, nullable=True)      # non-penalty xG
    npxg_against: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    xpts_per_match: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sources_updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class LeagueAverage(Base):
    __tablename__ = "league_averages"

    sport_key: Mapped[str] = mapped_column(String, primary_key=True)
    home_avg: Mapped[float] = mapped_column(Float, nullable=False)
    away_avg: Mapped[float] = mapped_column(Float, nullable=False)
    n_matches: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("event_id", "market", "selection", name="uq_prediction_selection"),
        Index("ix_predictions_created_at", "created_at"),
        Index("ix_predictions_sport_key", "sport_key"),
        Index("ix_predictions_result", "result"),
        # Created by migration g1a3b7d4e8f2 ; declared here so `alembic check`
        # doesn't see drift between the ORM and the live schema.
        Index("ix_predictions_placement_status", "placement_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    sport_key: Mapped[str] = mapped_column(String, nullable=False)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    selection: Mapped[str] = mapped_column(String, nullable=False)
    model_prob: Mapped[float] = mapped_column(Float, nullable=False)
    best_odds: Mapped[float] = mapped_column(Float, nullable=False)
    best_book: Mapped[str] = mapped_column(String, nullable=False)
    value_edge: Mapped[float] = mapped_column(Float, nullable=False)
    kelly_stake: Mapped[float] = mapped_column(Float, nullable=False)
    lambda_home: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lambda_away: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    model_type: Mapped[str] = mapped_column(String, nullable=False, default="poisson")
    result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    closing_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    resolved_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Lifecycle state from the user's perspective :
    #   "proposed"  : bot recommended it, awaiting user action (bankroll NOT debited)
    #   "confirmed" : user placed the bet at their bookmaker (bankroll debited)
    #   "skipped"   : user passed on it, or auto-expired past kickoff (no debit)
    # Defaults to "proposed". Worker save_prediction does NOT debit the
    # bankroll any more — the user must explicitly confirm via the dashboard
    # or API endpoint /predictions/{id}/confirm-placed.
    placement_status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    placement_status_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    placed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    placed_bookmaker: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Promotion(Base):
    """
    Bookmaker promo / freebet / refund / cashback tracker.

    Each row is a promo offered by a bookmaker on a specific date, with its
    terms and the value-cash-equivalent we got from it. This lets us
    distinguish "real edge" (statistical model) from "promo edge" (free money
    that the bookmaker uses to acquire/retain users).

    Why it matters: a bot that loses 5% on the model but gains 8% on promos
    looks profitable, but it's a different game. Tracking promos separately
    keeps the ROI of the *strategy* honest.
    """
    __tablename__ = "promotions"
    __table_args__ = (
        Index("ix_promotions_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    received_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    bookmaker_key: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("bookmakers.key", ondelete="SET NULL"), nullable=True,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Common kinds:
    #   "freebet"       — token usable on selected events
    #   "deposit_match" — 100% match on a deposit up to X$
    #   "refund"        — refund if the bet loses by 1 specific outcome
    #   "boost"         — odds enhancement
    #   "cashback"      — % back of weekly losses
    nominal_value: Mapped[float] = mapped_column(Float, nullable=False)
    cash_equivalent: Mapped[float] = mapped_column(Float, nullable=False)
    # cash_equivalent < nominal_value because rollover requirements / odds caps
    # eat real value. E.g. a 50€ freebet at 2.0 odds gives ~25-35€ EV.
    rollover_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    used: Mapped[bool] = mapped_column(default=False)
    used_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class CashOut(Base):
    """
    Tracks cash-outs taken before a match settles.

    A cash-out closes a pending bet at a price the bookmaker offers (always
    below true EV — that's how they monetize). Recording each cash-out lets
    us:
      - separate "skill ROI" (would the bet have won?) from "cash-out ROI"
        (was the cash-out a good decision relative to the held EV?)
      - measure how often cash-outs cost us long-term
    """
    __tablename__ = "cash_outs"
    __table_args__ = (
        Index("ix_cash_outs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("predictions.id", ondelete="CASCADE"), nullable=False,
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    cash_out_amount: Mapped[float] = mapped_column(Float, nullable=False)
    # The bookmaker's offered price; we record it but don't trust it for
    # accounting — the user's bankroll moves by the actual amount received.
    bookmaker_offered_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AgentRun(Base):
    """
    Persists each AI-agent invocation: the user filters that triggered it,
    the reasoning trace, the picks, and the metrics. Critical for audit and
    backtest replays.
    """
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    trigger: Mapped[str] = mapped_column(String, nullable=False)  # "scheduled" | "dashboard" | "api"
    filters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model: Mapped[str] = mapped_column(String, nullable=False)  # "claude-sonnet-4-6", etc.
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    picks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    n_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")  # ok | error | timeout
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Bookmaker(Base):
    """
    A betting account at a specific bookmaker. Each row tracks one user-owned
    account so the bot can split capital allocation realistically — having
    50€ at Bet365 and 30€ at Pinnacle is NOT the same as 80€ in one place.

    `key` is a stable slug used in cross-references (e.g. "pinnacle", "bet365");
    `display_name` is the human-readable label.
    """
    __tablename__ = "bookmakers"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    active: Mapped[bool] = mapped_column(default=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class BankrollEntry(Base):
    """
    Single source of truth for bankroll evolution. Every cash movement —
    deposits, withdrawals, bet placements, settlements — produces ONE row.
    The current balance is the running sum of `amount` over all rows.

    `kind` taxonomy (signed `amount` always tells the direction):
      - "deposit"     :  +N    cash in
      - "withdrawal"  :  -N    cash out
      - "bet_placed"  :  -N    stake immobilized when a prediction is saved
      - "bet_won"     :  +N    payout = stake × odds (full return, not net)
      - "bet_lost"    :   0    stake already debited at placement (tracking only)
      - "bet_void"    :  +N    refund of the original stake (push)
      - "adjustment"  :  ±N    manual correction by the user (with note)

    Storing `balance_after` is redundant with the running sum but lets us
    detect ledger corruption (sum mismatch) and renders charts without an
    O(n) scan per row.
    """
    __tablename__ = "bankroll_ledger"
    __table_args__ = (
        Index("ix_bankroll_ts", "ts"),
        Index("ix_bankroll_kind", "kind"),
        # Explicit name matches the migration d1f4a8e7b3c0 that originally
        # created this index. Using `index=True` on the column would cause
        # SQLAlchemy to autogenerate `ix_bankroll_ledger_bookmaker_key`,
        # producing a perpetual divergence flagged by `alembic check`.
        Index("ix_bankroll_bookmaker", "bookmaker_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance_after: Mapped[float] = mapped_column(Float, nullable=False)
    prediction_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("predictions.id", ondelete="SET NULL"), nullable=True,
    )
    # Per-bookmaker partitioning of the bankroll. Nullable so legacy rows
    # (before Phase A5) keep loading; new rows always specify the account.
    bookmaker_key: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("bookmakers.key", ondelete="SET NULL"),
        nullable=True,  # index defined explicitly in __table_args__ above
    )
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class IdempotencyKey(Base):
    """
    Client-provided idempotency keys for mutating endpoints.

    The key is supplied by the caller via the `Idempotency-Key` header. On
    first request we record the response body + status code; subsequent
    requests with the same key replay the cached response without re-running
    the underlying mutation. Two different request bodies under the same key
    are rejected (409 Conflict) — protects against accidental key reuse.

    Used for bankroll deposit / withdraw where a network glitch + retry
    could otherwise produce a double mutation.
    """
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("ix_idempotency_created_at", "created_at"),
    )

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
