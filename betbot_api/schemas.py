"""Pydantic schemas — request/response shapes for the REST API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    teams_in_db: int
    scan_hours: list[str]
    bankroll: float
    agent_enabled: bool


class EventBrief(BaseModel):
    event_id: str | None
    sport_key: str
    home_team: str | None
    away_team: str | None
    commence_time: str | None
    n_bookmakers: int


class EventsResponse(BaseModel):
    total: int
    by_sport: dict[str, list[EventBrief]]
    today_only: bool


class PredictionRow(BaseModel):
    id: int
    created_at: str
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    market: str
    selection: str
    model_prob: float
    best_odds: float
    best_book: str
    value_edge: float
    kelly_stake: float
    model_type: str
    result: str | None = None


class ROIStats(BaseModel):
    n_bets: int
    n_wins: int
    hit_rate: float
    roi: float
    avg_edge: float
    n_with_clv: int = 0
    avg_clv_pct: float = 0.0
    positive_clv_share: float = 0.0


class AgentFilters(BaseModel):
    """Filters the user can send from the dashboard to the AI agent."""
    sport_key: str | None = Field(
        default=None,
        description="Restrict to a single league (e.g. soccer_epl). Default: all.",
    )
    today_only: bool = Field(default=True, description="Only matches kicking off today.")
    min_edge: float | None = Field(
        default=None, ge=-1.0, le=1.0,
        description="Minimum value edge (0.04 = 4%). Default: settings value.",
    )
    min_prob: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Minimum model probability per leg.",
    )
    min_odds: float | None = Field(
        default=None, ge=1.0,
        description="Minimum book odds per leg.",
    )
    n_legs: int = Field(default=3, ge=1, le=6, description="Number of legs per parlay.")
    n_combos: int = Field(default=3, ge=1, le=10, description="Number of parlays to return.")
    extra_instructions: str | None = Field(
        default=None,
        description="Free-form guidance for the agent (e.g. 'avoid draws', 'prioritize home favorites').",
    )


class AgentResponse(BaseModel):
    picks: list[dict[str, Any]]
    parlays: list[dict[str, Any]]
    rationale: str
    n_tool_calls: int
    duration_ms: int
    cost_usd: float | None = None
    model: str
    agent_run_id: int
    error: str | None = None


class ManualScanFilters(BaseModel):
    """Filters for the no-AI manual scan — same surface as the AI agent's filters
    but only the ones that make sense for the deterministic pipeline."""
    sport_key: str | None = Field(default=None)
    today_only: bool = Field(default=True)
    min_edge: float | None = Field(default=None, ge=-1.0, le=1.0)
    min_prob: float | None = Field(default=None, ge=0.0, le=1.0)
    min_odds: float | None = Field(default=None, ge=1.0)
    n_legs: int = Field(default=3, ge=1, le=6)
    n_combos: int = Field(default=3, ge=1, le=10)


class ManualScanResponse(BaseModel):
    picks: list[dict[str, Any]]
    parlays: list[dict[str, Any]]
    n_picks: int
    n_parlays: int
    filters_used: dict[str, Any]
    n_events_scanned: int
