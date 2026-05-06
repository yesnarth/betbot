"""The Odds API client with retry, quota guard, and all-bookmakers fetch."""
import time
import logging

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger("betbot.api")

BASE_URL = "https://api.the-odds-api.com/v4/sports"
QUOTA_MINIMUM = 20  # stop if fewer than this many requests remain

SPORT_KEYS = [
    # Football — full Poisson model (xG/ELO/Tavily news/weather all wired)
    "soccer_france_ligue1",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_uefa_champs_league",
    "soccer_africa_cup_of_nations",
    # Tennis — uses the consensus model only (no per-player Poisson stats yet).
    # Markets are h2h only ("Player 1 vs Player 2"). Set MULTI_SPORT_TENNIS=1
    # in .env to actually scan these (default: off, to save Odds API quota).
]


def _enabled_sport_keys() -> list[str]:
    """Filter SPORT_KEYS by feature flags. Defaults to football-only.

    Tennis is OFF by default because:
      1. We don't yet have per-player Poisson stats (model degrades to consensus)
      2. Each scanned sport costs one Odds API request — protects free quota
    Toggle via .env: MULTI_SPORT_TENNIS=1
    """
    import os
    keys = list(SPORT_KEYS)
    if os.getenv("MULTI_SPORT_TENNIS", "0") == "1":
        keys += ["tennis_atp_french_open", "tennis_atp_us_open",
                 "tennis_atp_wimbledon", "tennis_atp_aus_open"]
    if os.getenv("MULTI_SPORT_BASKETBALL", "0") == "1":
        keys += ["basketball_nba", "basketball_euroleague"]
    return keys


class QuotaExhaustedError(Exception):
    pass


class OddsAPIClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self.quota_remaining: int = 9999
        self._session = requests.Session()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, url: str, params: dict) -> dict:
        resp = self._session.get(url, params=params, timeout=10)
        self._update_quota(resp)
        if resp.status_code == 401:
            raise ValueError("Clé API invalide (401). Vérifie ODDS_API_KEY dans .env")
        if resp.status_code == 422:
            raise ValueError(f"Paramètres invalides pour {url}")
        if resp.status_code != 200:
            logger.warning("HTTP %s pour %s", resp.status_code, url)
            return []
        return resp.json()

    def _update_quota(self, resp: requests.Response) -> None:
        try:
            remaining = int(resp.headers.get("x-requests-remaining", 9999))
            self.quota_remaining = remaining
            if remaining < QUOTA_MINIMUM:
                raise QuotaExhaustedError(
                    f"Quota insuffisant : {remaining} requêtes restantes (min {QUOTA_MINIMUM})"
                )
            if remaining < 50:
                logger.warning("⚠️  Quota faible : %d requêtes restantes", remaining)
        except (ValueError, TypeError):
            pass

    def get_events_with_odds(self, sport: str, markets: str = "h2h,totals") -> list[dict]:
        """
        Fetch odds for the requested markets across all available bookmakers.

        Default `markets` covers what The Odds API reliably supports for soccer:
          - h2h    : 1/X/2 (match winner)
          - totals : Over/Under (we filter on the 2.5 line in extract_best_odds)

        BTTS (Both Teams To Score) is calculated by the model but NOT requested
        from the Odds API — `btts` is not a valid market key for soccer in their
        EU regions. To enable BTTS value bets we'd need a different odds provider.

        Each additional market costs the same as one h2h-only request in their
        billing model.
        """
        url = f"{BASE_URL}/{sport}/odds"
        params = {
            "apiKey": self._key,
            "regions": "eu",
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        data = self._get(url, params)
        events = data if isinstance(data, list) else []
        logger.info("  %s : %d matchs (%d req restantes)", sport, len(events), self.quota_remaining)
        return events

    def get_scores(self, sport: str, days_from: int = 3) -> list[dict]:
        """
        Fetch completed match scores for a sport.
        days_from: how many days back to look (max 3 on free tier).
        Returns list of events with {id, home_team, away_team, completed, scores: [...]}
        """
        url = f"{BASE_URL}/{sport}/scores"
        params = {
            "apiKey": self._key,
            "daysFrom": min(max(days_from, 1), 3),
            "dateFormat": "iso",
        }
        data = self._get(url, params)
        events = data if isinstance(data, list) else []
        return events

    def fetch_all_sports(self) -> dict[str, list[dict]]:
        """Fetch odds for all enabled sport keys (filtered by feature flags).
        Returns {sport_key: [events]}."""
        results: dict[str, list[dict]] = {}
        for sport in _enabled_sport_keys():
            try:
                events = self.get_events_with_odds(sport)
                results[sport] = events
                time.sleep(0.4)
            except QuotaExhaustedError:
                logger.error("Quota épuisé — arrêt du fetch à %s", sport)
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                logger.error("Erreur réseau pour %s : %s", sport, exc)
            except Exception as exc:
                logger.error("Erreur inattendue pour %s : %s", sport, exc)
        return results
