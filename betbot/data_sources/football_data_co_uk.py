"""football-data.co.uk — free historical results + REAL closing odds.

Why: our calibration/backtest previously scored the model against a *synthetic*
base-rate market (a fixed 5% margin on outcome frequencies). That trains the
calibrator on probabilities that never met a real bookmaker. football-data.co.uk
publishes, per league and season, every match's result AND the CLOSING odds from
several books — including Pinnacle (the sharpest). Feeding real closing lines
lets the backtest shrink toward the market exactly like production does, so the
calibrator learns the correction that actually applies at bet time.

Free, static CSVs (no key, no rate limit). We cache them under data/fd_couk/.
Self-contained: team names here differ from The Odds API / football-data.org
("Man United", "Ath Madrid"), but the backtest only needs internal consistency
(same name across a league's own matches), so no cross-source mapping is needed.

CSV columns we use:
  Date (dd/mm/yyyy), HomeTeam, AwayTeam, FTHG, FTAG, FTR,
  closing 1X2 odds — Pinnacle PSCH/PSCD/PSCA, else Avg AvgCH/CD/CA, else Bet365.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger("betbot.fdcouk")

BASE_URL = "https://www.football-data.co.uk/mmz4281"

# sport_key → football-data.co.uk division code
DIV_MAP: dict[str, str] = {
    "soccer_epl": "E0",
    "soccer_england_championship": "E1",
    "soccer_spain_la_liga": "SP1",
    "soccer_germany_bundesliga": "D1",
    "soccer_italy_serie_a": "I1",
    "soccer_france_ligue1": "F1",
    "soccer_netherlands_eredivisie": "N1",
    "soccer_portugal_primeira_liga": "P1",
}

CACHE_DIR = Path(os.getenv("FD_COUK_CACHE", "data/fd_couk"))

# Preferred closing-odds column triples, sharpest first.
_ODDS_TRIPLES = (
    ("PSCH", "PSCD", "PSCA"),   # Pinnacle closing (sharpest)
    ("AvgCH", "AvgCD", "AvgCA"),  # market average closing
    ("B365CH", "B365CD", "B365CA"),  # Bet365 closing
    ("PSH", "PSD", "PSA"),       # Pinnacle opening (last resort)
)


def _season_code(start_year: int) -> str:
    """2025 → '2526' (the football-data.co.uk season file code)."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def recent_completed_seasons(n: int = 2) -> list[int]:
    """Starting years of the most recent COMPLETED seasons, newest first.

    A season starting in year Y ends ~May of Y+1. We skip the current (possibly
    unstarted) season and return the last `n` that have finished.
    """
    now = datetime.now(timezone.utc)
    # From August the season that started (year-1) has completed; before August,
    # step back one more so we never point at an in-progress season.
    latest = now.year - 1 if now.month >= 8 else now.year - 1
    if now.month < 6:  # Jan–May: the year-1 season is still in progress
        latest = now.year - 2
    return [latest - i for i in range(n)]


def _parse_odds(row: dict) -> tuple[float, float, float] | None:
    for cols in _ODDS_TRIPLES:
        try:
            h, d, a = (float(row[cols[0]]), float(row[cols[1]]), float(row[cols[2]]))
        except (KeyError, ValueError, TypeError):
            continue
        if h > 1.0 and d > 1.0 and a > 1.0:
            return h, d, a
    return None


def _parse_date(raw: str) -> str | None:
    """'16/08/2024' or '16/08/24' → ISO 'YYYY-MM-DD' (for chronological sort)."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            continue
    return None


def _parse_csv(text: str) -> list[dict]:
    matches: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        date = _parse_date(row.get("Date", ""))
        home, away = (row.get("HomeTeam") or "").strip(), (row.get("AwayTeam") or "").strip()
        if not (date and home and away):
            continue
        try:
            hg, ag = int(row["FTHG"]), int(row["FTAG"])
        except (KeyError, ValueError, TypeError):
            continue  # not played / missing score
        odds = _parse_odds(row)
        if odds is None:
            continue  # no usable closing line → useless for market-shrink calibration
        matches.append({
            "date": date,
            "home_team": home,
            "away_team": away,
            "home_goals": hg,
            "away_goals": ag,
            "close_home": odds[0],
            "close_draw": odds[1],
            "close_away": odds[2],
        })
    return matches


def _fetch_csv(div: str, season_code: str, max_age_days: int = 7) -> str | None:
    """Download (or read cached) a season CSV. Past seasons are immutable, so a
    cached file is reused; the in-progress season refreshes after max_age_days."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{div}_{season_code}.csv"
    if cache.exists():
        age = (datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime) / 86400.0
        if age < max_age_days:
            return cache.read_text(encoding="latin-1", errors="replace")
    url = f"{BASE_URL}/{season_code}/{div}.csv"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200 or not resp.text.strip():
            logger.debug("fd.co.uk %s %s → HTTP %s", div, season_code, resp.status_code)
            return cache.read_text(encoding="latin-1", errors="replace") if cache.exists() else None
        cache.write_text(resp.text, encoding="latin-1", errors="replace")
        return resp.text
    except Exception as exc:
        logger.debug("fd.co.uk fetch %s %s: %s", div, season_code, exc)
        return cache.read_text(encoding="latin-1", errors="replace") if cache.exists() else None


def get_matches_with_odds(sport_key: str, n_seasons: int = 2) -> list[dict]:
    """Results + real closing 1X2 odds for a league across the last `n_seasons`
    completed seasons, oldest → newest. Empty list when the league isn't mapped
    or nothing could be fetched (caller falls back)."""
    div = DIV_MAP.get(sport_key)
    if not div:
        return []
    out: list[dict] = []
    # Pull a couple extra candidate seasons so we still get n_seasons of data even
    # if the most recent file is briefly unavailable.
    for start_year in recent_completed_seasons(n_seasons + 1):
        text = _fetch_csv(div, _season_code(start_year))
        if not text:
            continue
        season_matches = _parse_csv(text)
        if season_matches:
            out.extend(season_matches)
            logger.info("  fd.co.uk %s %s : %d matchs avec cotes de clôture",
                        div, _season_code(start_year), len(season_matches))
        if sum(1 for _ in out) and len({m["date"][:4] for m in out}) >= n_seasons:
            break
    out.sort(key=lambda m: m["date"])
    return out
