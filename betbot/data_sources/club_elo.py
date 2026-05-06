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
from datetime import date, datetime
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


def get_all_elo_ratings(target_date: date | None = None) -> dict[str, float]:
    """
    Return a {normalized_team_name: elo_rating} mapping for the whole world
    on the given date (default: today). Cached for 24 hours.
    """
    target_date = target_date or date.today()
    if target_date in _CACHE:
        return _CACHE[target_date]

    csv_text = _fetch_csv(target_date)
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
