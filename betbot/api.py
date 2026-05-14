"""The Odds API client with retry, quota guard, and all-bookmakers fetch."""
import os
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


def _quota_minimum() -> int:
    """Read the safety threshold each call so .env changes take effect at runtime."""
    try:
        return max(0, int(os.getenv("ODDS_QUOTA_MINIMUM", "20")))
    except ValueError:
        return 20

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


class OddsAPIServerError(Exception):
    """Raised on HTTP 5xx from The Odds API — distinguishes real upstream
    failure from successful-but-empty responses. Callers should treat this
    as a transient outage and let it surface, not silently swallow it."""
    pass


class OddsAPIClient:
    def __init__(self, api_key: str):
        self._key = api_key
        # -1 = unknown until the first response with a x-requests-remaining
        # header is observed. A misleading default like 9999 used to surface
        # in the dashboard as "OK" even when the probe had failed.
        self.quota_remaining: int = -1
        self.quota_exhausted: bool = False
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
        if resp.status_code == 404:
            # 404 typically means a sport key that doesn't exist any more on
            # The Odds API (e.g. discontinued tournament). Skip silently —
            # the wishlist filter via get_active_sports should prevent this
            # but we are defensive against stale config.
            logger.warning("HTTP 404 pour %s", url)
            return []
        if 500 <= resp.status_code < 600:
            # Upstream outage : raise loudly so the caller (worker scan,
            # resolver, CLV snapshot) treats it as a real failure rather
            # than silently believing "no events". Returning [] used to
            # leave resolver stuck for hours and burn quota on retries.
            raise OddsAPIServerError(
                f"Odds API returned HTTP {resp.status_code} for {url}"
            )
        if resp.status_code != 200:
            # Other 4xx (rate-limit etc.) — log and bail, but don't pretend success.
            logger.warning("HTTP %s pour %s (treating as empty response)",
                           resp.status_code, url)
            return []
        return resp.json()

    def _update_quota(self, resp: requests.Response) -> None:
        try:
            remaining = int(resp.headers.get("x-requests-remaining", 9999))
            self.quota_remaining = remaining
            threshold = _quota_minimum()
            if remaining < threshold:
                self.quota_exhausted = True
                raise QuotaExhaustedError(
                    f"Quota insuffisant : {remaining} requêtes restantes (min {threshold})"
                )
            if remaining < 50:
                logger.warning("⚠️  Quota faible : %d requêtes restantes", remaining)
        except (ValueError, TypeError):
            pass

    def get_active_sports(self) -> set[str]:
        """Return the set of sport keys currently in-season.

        Hits `/v4/sports?all=false` which The Odds API serves for FREE
        (no `x-requests-used` increment). Used to skip out-of-season leagues
        before burning quota on `/v4/sports/{key}/odds` requests.
        """
        try:
            resp = self._session.get(BASE_URL, params={"apiKey": self._key, "all": "false"}, timeout=5)
            try:
                self.quota_remaining = int(resp.headers.get("x-requests-remaining", self.quota_remaining))
            except (ValueError, TypeError):
                pass
            data = resp.json() if resp.status_code == 200 else []
            return {item["key"] for item in data if isinstance(item, dict) and "key" in item}
        except Exception as exc:
            logger.debug("get_active_sports: %s", exc)
            return set()

    def probe_quota(self) -> int:
        """Refresh quota_remaining without consuming a billed request.

        Hits the `/v4/sports` listing endpoint, which is free on The Odds API
        and still returns the `x-requests-remaining` header. Used by /health
        to display live quota in the dashboard.
        """
        try:
            resp = self._session.get(BASE_URL, params={"apiKey": self._key}, timeout=5)
            try:
                self.quota_remaining = int(resp.headers.get("x-requests-remaining", self.quota_remaining))
            except (ValueError, TypeError):
                pass
            self.quota_exhausted = self.quota_remaining < _quota_minimum()
        except Exception as exc:
            logger.debug("probe_quota: %s", exc)
        return self.quota_remaining

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
        """Fetch odds for the wishlist, INTERSECTED with currently-active sports.

        Out-of-season leagues (e.g. Premier League in July) are silently
        skipped — saves quota with no degradation in coverage.
        """
        wishlist = _enabled_sport_keys()
        active = self.get_active_sports()  # free probe
        if active:
            to_query = [s for s in wishlist if s in active]
            skipped = [s for s in wishlist if s not in active]
            if skipped:
                logger.info("Skipping out-of-season sports : %s", ", ".join(skipped))
        else:
            # Probe failed — fall back to wishlist (we'd rather over-query than miss data)
            logger.warning("Active-sports probe returned empty; falling back to full wishlist")
            to_query = wishlist

        results: dict[str, list[dict]] = {}
        for sport in to_query:
            try:
                events = self.get_events_with_odds(sport)
                results[sport] = events
                time.sleep(0.4)
            except QuotaExhaustedError:
                # Stop the loop entirely — every subsequent call would fail
                # the same way and just burn the buffer below QUOTA_MINIMUM.
                logger.error("Quota épuisé — arrêt du fetch à %s", sport)
                break
            except OddsAPIServerError as exc:
                # Upstream 5xx for THIS sport. Log it visibly but keep
                # fetching the other sports — the worker shouldn't lose
                # an entire scan because one league is having issues.
                logger.error("Odds API 5xx pour %s : %s — autre(s) sport(s) suivent",
                             sport, exc)
            except (requests.Timeout, requests.ConnectionError) as exc:
                logger.error("Erreur réseau pour %s : %s", sport, exc)
            except Exception as exc:
                logger.error("Erreur inattendue pour %s : %s", sport, exc)
        return results
