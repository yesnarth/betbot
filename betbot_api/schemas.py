"""Pydantic schemas — request/response shapes for the REST API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    teams_in_db: int
    scan_hours: list[str]
    bankroll_initial: float           # value of BANKROLL in .env — Kelly reference
    balance: float                    # actual current bankroll balance
    available: float                  # balance minus committed stakes
    agent_enabled: bool
    odds_quota_remaining: int = -1    # The Odds API monthly quota left; -1 = unknown
    odds_quota_minimum: int = 20      # safety threshold below which scans are blocked
    odds_quota_exhausted: bool = False  # true when quota_remaining < odds_quota_minimum
    active_sports: list[str] = []     # currently in-season sports from our wishlist
    db_latency_ms: int = -1           # SELECT 1 round-trip latency; -1 = unknown


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
    placement_status: str = "proposed"     # proposed | confirmed | skipped
    placement_status_at: str | None = None
    placed_bookmaker: str | None = None
    commence_time: str | None = None       # match kickoff (used by UI countdown)


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
    odds_quota_remaining: int = -1
    odds_quota_exhausted: bool = False


class LocalAgentFilters(ManualScanFilters):
    """Same filters as the manual scan, plus runtime toggles for the rule engine."""
    fetch_news: bool = Field(default=True, description="Use Tavily web search to fetch live news")
    fetch_weather: bool = Field(default=True, description="Fetch Open-Meteo forecast for the home stadium")
    min_final_edge: float = Field(default=0.02, ge=-0.10, le=0.50,
                                  description="Reject picks whose calibrated edge falls below this")


class BankrollSnapshot(BaseModel):
    balance: float
    committed: float
    available: float
    total_deposits: float
    total_withdrawals: float
    total_won: float
    total_lost_stakes: float
    pnl: float
    n_entries: int


class BankrollMutation(BaseModel):
    # Upper bound prevents accidents (typing 1000000 instead of 100) AND a
    # whole class of authenticated-attacker abuse vectors (huge deposit
    # corrupting Kelly stake math, integer overflows downstream, etc.).
    amount: float = Field(
        ..., gt=0, le=1_000_000,
        description="Amount in account currency (positive, max 1 000 000)",
    )
    note: str | None = Field(default=None, max_length=500)


class BankrollLedgerRow(BaseModel):
    id: int
    ts: str
    kind: str
    amount: float
    balance_after: float
    prediction_id: int | None = None
    note: str | None = None


class LocalAgentResponse(BaseModel):
    picks: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    parlays: list[dict[str, Any]]
    n_picks_in: int
    n_accepted: int
    n_rejected: int
    n_parlays: int
    n_news_calls: int
    n_weather_calls: int
    tavily_available: bool
    odds_quota_remaining: int = -1
    odds_quota_exhausted: bool = False
