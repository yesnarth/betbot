"""
SQLAlchemy 2.0 ORM models. Mirrors the previous raw-SQL schema 1:1 to keep
existing data and tests compatible across the SQLite → PostgreSQL migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    # --- Enrichment columns (Phase 8) — nullable so legacy rows still load ---
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
