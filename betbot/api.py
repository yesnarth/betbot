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
    "soccer_france_ligue1",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_uefa_champs_league",
    "soccer_africa_cup_of_nations",
]


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

    def get_events_with_odds(self, sport: str) -> list[dict]:
        """Fetch H2H odds for all available bookmakers for a given sport."""
        url = f"{BASE_URL}/{sport}/odds"
        params = {
            "apiKey": self._key,
            "regions": "eu",
            "markets": "h2h",
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
        """Fetch odds for all configured sport keys. Returns {sport_key: [events]}."""
        results: dict[str, list[dict]] = {}
        for sport in SPORT_KEYS:
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
