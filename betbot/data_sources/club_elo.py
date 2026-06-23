"""
Club Elo ratings — http://api.clubelo.com

Free, no API key, no quota. Returns the current Elo rating of every club in
the world. Elo ratings are the most stable single metric of a club's strength
and outperform most ad-hoc models on long-term prediction.

Endpoints:
    /YYYY-MM-DD  → CSV of every club's Elo on that date
    /<ClubName>  → CSV of historical Elos for one club

We fetch the full daily snapshot once a day (cached) since it's tiny and
gives us every team in one request.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.data_sources.club_elo")

API_URL = "http://api.clubelo.com/{date_iso}"
CACHE_TTL_SECONDS = 24 * 3600
_CACHE: dict[date, dict[str, float]] = {}


def _normalize(name: str) -> str:
    """Same normalization scheme used in betbot.analysis to bridge naming."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    for stop in ("fc", "cf", "ac", "rc", "rcd", "as", "ss", "us", "ud", "cd",
                 "afc", "sc", "calcio", "balompie", "hotspur", "albion"):
        s = s.replace(f" {stop} ", " ").replace(f"{stop} ", "").replace(f" {stop}", "")
    return "".join(c for c in s if c.isalnum())


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _fetch_csv(target_date: date) -> str:
    url = API_URL.format(date_iso=target_date.isoformat())
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


def _fetch_csv_latest(target_date: date, max_lookback_days: int = 7):
    """Fetch the ClubElo snapshot for target_date, falling back to the most
    recent EARLIER date if that date isn't published yet (404). ClubElo only has
    data up to its last computed date, so a clock that's a day or two ahead of
    ClubElo's last update (or simply a day it hasn't published) would otherwise
    404 and kill the ELO signal entirely. Returns (csv_text, used_date) or
    (None, None) if no date in the window is available. The retry decorator on
    _fetch_csv still covers transient network errors; 404 is what we walk back."""
    for offset in range(max_lookback_days + 1):
        d = target_date - timedelta(days=offset)
        try:
            return _fetch_csv(d), d
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                continue
            raise
    return None, None


def get_all_elo_ratings(target_date: date | None = None) -> dict[str, float]:
    """
    Return a {normalized_team_name: elo_rating} mapping for the whole world
    on the given date (default: today). Falls back to the most recent published
    date when today's snapshot isn't available yet. Cached for 24 hours; an empty
    result is cached too, so a degraded ELO source never hammers the API during a
    scan (the blended model degrades to Dixon-Coles + H2H without it).
    """
    target_date = target_date or date.today()
    if target_date in _CACHE:
        return _CACHE[target_date]

    csv_text, used = _fetch_csv_latest(target_date)
    if csv_text is None:
        logger.warning(
            "Club Elo : aucune date publiée autour de %s — ELO indisponible "
            "(le modèle dégrade sur Dixon-Coles + H2H).", target_date)
        _CACHE[target_date] = {}
        return {}

    ratings: dict[str, float] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        try:
            name = row.get("Club", "").strip()
            if not name:
                continue
            elo = float(row.get("Elo", "0"))
            ratings[_normalize(name)] = elo
        except (ValueError, KeyError):
            continue

    _CACHE[target_date] = ratings
    if used != target_date:
        logger.info("Club Elo : %d ratings (repli sur %s, %s pas encore publié)",
                    len(ratings), used, target_date)
    else:
        logger.info("Club Elo : %d ratings chargés pour %s", len(ratings), target_date)
    return ratings


def get_team_elo(team_name: str, target_date: date | None = None) -> float | None:
    """Look up the Elo rating of a single team (fuzzy on normalized name)."""
    ratings = get_all_elo_ratings(target_date)
    norm = _normalize(team_name)
    if norm in ratings:
        return ratings[norm]
    # Loose fallback: any normalized name that contains the query
    for k, v in ratings.items():
        if len(norm) >= 5 and (norm in k or k in norm):
            return v
    return None


def elo_win_probability(elo_home: float, elo_away: float, home_advantage: float = 65.0) -> float:
    """
    Standard Elo H2H probability for the home team to NOT lose
    (used as a Bayesian prior alongside Poisson).

    home_advantage: Elo points credited to the home team (≈ 65 in football).
    """
    diff = (elo_home + home_advantage) - elo_away
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))
