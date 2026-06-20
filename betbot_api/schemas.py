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


class ConfirmPlacedRequest(BaseModel):
    """POST body for /predictions/{id}/confirm-placed.

    Validates types up-front so callers can't send `unconfirm: "false"` (string)
    and have it silently coerced to True by Python truthiness.
    """
    bookmaker: str | None = Field(default=None, max_length=64)
    unconfirm: bool = False


class SkipRequest(BaseModel):
    """POST body for /predictions/{id}/skip."""
    reason: str | None = Field(default="user_skipped", max_length=120)


class ProposedPickInput(BaseModel):
    """POST body for /admin/save-pick-as-proposed — same shape as the picks
    returned by /recommend/manual, validated strictly so a malformed pick
    can't slip through and corrupt the prediction table."""
    event_id: str = Field(..., min_length=1, max_length=128)
    sport_key: str = Field(..., min_length=1, max_length=64)
    home_team: str = Field(..., min_length=1, max_length=128)
    away_team: str = Field(..., min_length=1, max_length=128)
    market: str = Field(..., min_length=1, max_length=32)
    selection_code: str = Field(..., min_length=1, max_length=8)
    model_prob: float = Field(..., ge=0.0, le=1.0)
    best_odds: float = Field(..., ge=1.0, le=1000.0)
    best_book: str = Field(..., min_length=1, max_length=64)
    value_edge: float = Field(..., ge=-1.0, le=10.0)
    kelly_stake: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    lambda_home: float | None = Field(default=None, ge=0.0, le=10.0)
    lambda_away: float | None = Field(default=None, ge=0.0, le=10.0)
    model_type: str = Field(default="poisson", max_length=32)
    reliability: float | None = Field(default=None, ge=0.0, le=1.0,
                                      description="0..1, qualifies value_edge")


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
    reliability: float | None = None       # 0..1, qualifies value_edge
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


class TargetParlayFilters(BaseModel):
    """Filters for the ×1000 high-variance parlay mode — stacks many legs toward
    a combined-odds CEILING. `target_odds` is a max-not-to-exceed, not a floor:
    the builder returns the requested number of combos, each as big as it can be
    WITHOUT exceeding the cap (so you still get combos below ×1000 on thin days).
    Same protections as the safe-picks engine (no-vig gate, positive-stake legs):
    the ceiling is approached by adding MORE disciplined favorites, not by padding
    with longshots likely to fail. Still a low-probability lottery on variance,
    but every leg is +edge and the ticket is +EV."""
    sport_key: str | None = Field(default=None)
    today_only: bool = Field(default=True)
    target_odds: float = Field(default=100.0, ge=2.0, le=100_000.0,
                               description="Cote combinée MAX / plafond à ne pas dépasser (défaut 100 ; monte vers 1000 pour viser plus gros) — pas un objectif obligatoire")
    max_legs: int = Field(default=14, ge=2, le=20,
                          description="Nombre maximum de jambes par combiné")
    n_combos: int = Field(default=3, ge=1, le=10)
    min_leg_odds: float = Field(default=1.2, ge=1.01, le=50.0,
                                description="Cote minimale acceptée par jambe")
    max_leg_odds: float | None = Field(default=2.5, ge=1.1, le=50.0,
                                       description="Cote MAX par jambe — plafonne pour atteindre la cible en empilant des favoris, pas des longshots")
    min_prob: float = Field(default=0.50, ge=0.0, le=1.0,
                            description="Proba modèle min/jambe — favoris (plus de chances de gagner que de perdre)")
    min_edge: float = Field(default=0.0, ge=-1.0, le=1.0,
                            description="Edge min vs meilleure cote (0 = EV non-négative ; la garde no-vig fait le tri fin)")


class TargetParlayResponse(BaseModel):
    parlays: list[dict[str, Any]]
    n_candidates: int            # nb de jambes candidates dans le pool
    best_achievable_odds: float  # meilleure cote atteignable (info si cible non atteinte)
    target_odds: float
    n_events_scanned: int
    odds_quota_remaining: int = -1
    odds_quota_exhausted: bool = False


class LiveScanFilters(BaseModel):
    """Filters for the live (in-play) scanner. Single bets only — no parlays."""
    sport_key: str | None = Field(default=None, description="None = tous les sports en cours")
    min_edge: float = Field(default=0.04, ge=-1.0, le=1.0)
    min_odds: float = Field(default=1.30, ge=1.0)


class LiveScanResponse(BaseModel):
    picks: list[dict[str, Any]]   # value bets live (avec live_score, minute estimée)
    n_live_events: int
    checked_at: str               # ISO — données à ~30 s, à rafraîchir souvent
    odds_quota_remaining: int = -1
    odds_quota_exhausted: bool = False


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


class BacktestRequest(BaseModel):
    """POST body for /backtest/run.

    Walk-forward backtest of the model on historical matches. Currently only
    football leagues mapped in `LEAGUE_MAP` are supported.
    """
    sport_key: str = Field(..., min_length=1, max_length=64)
    n_holdout: int = Field(default=100, ge=20, le=500,
                           description="Number of most-recent matches to score (walk-forward).")
    use_enrichment: bool = Field(
        default=False,
        description="Snapshot ELO/xG (look-ahead bias — gives optimistic bound).",
    )


class BacktestCalibrationBucket(BaseModel):
    range: str
    n_samples: int
    predicted_avg: float
    actual_avg: float
    abs_error: float


class BacktestResponse(BaseModel):
    sport_key: str
    n_matches: int
    brier_score: float          # 0 = perfect, ~0.667 = baseline 1/3
    log_loss: float
    calibration: list[BacktestCalibrationBucket]
    notes: str
    duration_seconds: float
    # Odds-free value backtest vs a synthetic base-rate market (proxy, not real odds).
    roi_pct: float = 0.0
    n_value_bets: int = 0
    avg_ev_pct: float = 0.0


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
