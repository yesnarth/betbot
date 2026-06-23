from dataclasses import dataclass, field
from pathlib import Path
import os
import re

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_HOUR_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass
class Settings:
    odds_api_key: str
    football_data_api_key: str
    gmail_user: str
    gmail_app_password: str
    gmail_recipient: str
    bankroll: float
    kelly_fraction: float
    min_value_edge: float
    min_model_prob: float
    min_book_odds: float
    top_bets: int
    min_combos: int
    top_combos: int
    scan_hours: list[str]          # ex: ["09:00", "15:00", "20:00"]
    min_before_kickoff: int        # ignorer matchs démarrant dans moins de N minutes
    # Garde anti-sélection-adverse : edge minimal exigé vs la ligne CONSENSUS
    # sans-vig (pas seulement la meilleure cote, souvent en retard). 0.0 =
    # désactivé. Consommé par analysis.detect_value_bets.
    min_edge_vs_novig: float = 0.0
    log_path: str = field(default="betbot.log")
    database_url: str = field(default="")
    anthropic_api_key: str = field(default="")
    anthropic_model: str = field(default="claude-sonnet-4-6")
    api_basic_user: str = field(default="betbot")
    api_basic_password: str = field(default="")
    # Shadow-log every scan's picks as 'proposed' so model performance is
    # measurable (and the ML calibrator gets training data). No bankroll impact.
    historize_scans: bool = True
    # CLV closing-odds snapshots run every 10 min and BURN Odds API quota. Off by
    # default (free-tier / often-off PC can't capture CLV reliably anyway) — turn
    # ON once on a paid plan / always-on VPS where CLV is actually capturable.
    clv_snapshot_enabled: bool = False
    # Selection discipline (anti "value-trap" on hard-to-predict longshots).
    # 0 / disabled by default on the dataclass; load_settings sets live values.
    max_book_odds: float = 0.0          # drop singles priced above this (0 = off)
    underdog_odds: float = 0.0          # odds at/above which the prob floor applies
    underdog_min_prob: float = 0.0      # required model_prob when odds ≥ underdog_odds
    novig_required: bool = False        # drop a pick when no-vig consensus is unavailable


def load_settings() -> Settings:
    missing = []

    def require(name: str) -> str:
        val = os.getenv(name, "")
        if not val or "REMPLACE" in val or "ton_adresse" in val:
            missing.append(name)
        return val

    odds_key  = require("ODDS_API_KEY")
    fd_key    = os.getenv("FOOTBALL_DATA_API_KEY", "")
    gmail_user = require("GMAIL_USER")
    gmail_pass = require("GMAIL_APP_PASSWORD")
    gmail_recip = os.getenv("GMAIL_RECIPIENT", gmail_user)

    bankroll          = float(os.getenv("BANKROLL", "100.0"))
    kelly_fraction    = float(os.getenv("KELLY_FRACTION", "0.25"))
    min_value_edge    = float(os.getenv("MIN_VALUE_EDGE", "0.04"))
    # 3% beat of the vig-removed CONSENSUS line (raised from 2% → surer: only
    # bet where the model clearly beats the efficient market). Tune via .env.
    min_edge_vs_novig = float(os.getenv("MIN_EDGE_VS_NOVIG", "0.03"))
    min_model_prob    = float(os.getenv("MIN_MODEL_PROB", "0.40"))
    min_book_odds     = float(os.getenv("MIN_BOOK_ODDS", "1.50"))
    top_bets          = int(os.getenv("TOP_BETS", "10"))
    min_combos        = int(os.getenv("MIN_COMBOS", "3"))
    top_combos        = int(os.getenv("TOP_COMBOS", "3"))
    min_before_kickoff = int(os.getenv("MIN_BEFORE_KICKOFF", "60"))

    # SCAN_HOURS : liste séparée par des virgules, ex "09:00,15:00,20:00".
    # Empty (SCAN_HOURS= or missing entirely) disables the auto-scan job — the
    # worker still runs (CLV snapshots, stats refresh, resolver, ...) but the
    # user triggers scans manually via the dashboard or the /recommend/manual
    # API endpoint. Useful for users who want strict control over Odds API
    # quota consumption.
    raw_hours = os.getenv("SCAN_HOURS", "09:00,15:00,20:00")
    scan_hours = [h.strip() for h in raw_hours.split(",") if h.strip()]
    invalid_hours = [h for h in scan_hours if not _HOUR_RE.match(h)]
    if invalid_hours:
        raise EnvironmentError(
            f"SCAN_HOURS invalide : {', '.join(invalid_hours)} — "
            "format attendu HH:MM 24h (ex: 09:00,15:00,20:00) "
            "ou vide (SCAN_HOURS=) pour désactiver l'auto-scan."
        )
    # Empty scan_hours is now allowed — see comment above.

    if missing:
        raise EnvironmentError(
            f"Variables manquantes dans .env : {', '.join(missing)}\n"
            "Consulte .env.example pour la configuration."
        )

    log_path = os.getenv("LOG_PATH", "betbot.log")  # empty string → stderr-only

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise EnvironmentError(
            "DATABASE_URL is required and must point to PostgreSQL.\n"
            "Quick start (Docker): `docker compose up -d db` then set in .env:\n"
            "  DATABASE_URL=postgresql+psycopg2://betbot:betbot_dev_pwd@localhost:5432/betbot"
        )
    if not database_url.startswith(("postgresql://", "postgresql+")):
        raise EnvironmentError(
            f"DATABASE_URL must be PostgreSQL — got dialect '{database_url.split('://', 1)[0]}'."
        )

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()
    basic_user = os.getenv("API_BASIC_USER", "betbot").strip()
    basic_pass = os.getenv("API_BASIC_PASSWORD", "").strip()

    historize_scans = os.getenv("HISTORIZE_SCANS", "1") == "1"
    clv_snapshot_enabled = os.getenv("CLV_SNAPSHOT_ENABLED", "0") == "1"
    # Selection discipline — live defaults bias AWAY from hard-to-predict
    # longshots/draws (the "value trap"). Tunable via .env.
    max_book_odds     = float(os.getenv("MAX_BOOK_ODDS", "6.0"))
    underdog_odds     = float(os.getenv("UNDERDOG_ODDS", "3.0"))
    underdog_min_prob = float(os.getenv("UNDERDOG_MIN_PROB", "0.42"))
    novig_required    = os.getenv("NOVIG_REQUIRED", "1") == "1"

    return Settings(
        odds_api_key=odds_key,
        football_data_api_key=fd_key,
        gmail_user=gmail_user,
        gmail_app_password=gmail_pass,
        gmail_recipient=gmail_recip,
        bankroll=bankroll,
        kelly_fraction=kelly_fraction,
        min_value_edge=min_value_edge,
        min_edge_vs_novig=min_edge_vs_novig,
        min_model_prob=min_model_prob,
        min_book_odds=min_book_odds,
        top_bets=top_bets,
        min_combos=min_combos,
        top_combos=top_combos,
        scan_hours=scan_hours,
        min_before_kickoff=min_before_kickoff,
        log_path=log_path,
        database_url=database_url,
        anthropic_api_key=anthropic_key,
        anthropic_model=anthropic_model,
        api_basic_user=basic_user,
        api_basic_password=basic_pass,
        historize_scans=historize_scans,
        clv_snapshot_enabled=clv_snapshot_enabled,
        max_book_odds=max_book_odds,
        underdog_odds=underdog_odds,
        underdog_min_prob=underdog_min_prob,
        novig_required=novig_required,
    )
