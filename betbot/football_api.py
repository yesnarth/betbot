"""football-data.org client for fetching team match history."""
import logging
import time

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger("betbot.football_api")

BASE_URL = "https://api.football-data.org/v4"

# Mapping The Odds API sport keys → football-data.org competition codes
LEAGUE_MAP: dict[str, str | None] = {
    "soccer_epl": "PL",
    "soccer_spain_la_liga": "PD",
    "soccer_germany_bundesliga": "BL1",
    "soccer_italy_serie_a": "SA",
    "soccer_france_ligue1": "FL1",
    "soccer_uefa_champs_league": "CL",
    "soccer_africa_cup_of_nations": None,  # non disponible sur free tier
    # Extended coverage — all three live in the football-data.org free tier
    # so the Poisson model gets full team-level stats without a paid plan.
    "soccer_efl_champ": "ELC",                  # English Championship (D2 anglaise)
    "soccer_netherlands_eredivisie": "DED",     # Eredivisie (D1 néerlandaise)
    "soccer_portugal_primeira_liga": "PPL",     # Primeira Liga (D1 portugaise)
}


class FootballDataClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._session = requests.Session()
        self._session.headers.update({"X-Auth-Token": api_key})

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=5, max=30),
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        resp = self._session.get(f"{BASE_URL}/{endpoint}", params=params or {}, timeout=15)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            logger.warning("Rate limit football-data.org — attente %ds", retry_after)
            time.sleep(retry_after)
            raise requests.ConnectionError("Rate limited")
        if resp.status_code == 403:
            raise ValueError(
                "Accès refusé football-data.org (403). "
                "Vérifie FOOTBALL_DATA_API_KEY dans .env\n"
                "Clé gratuite sur https://www.football-data.org"
            )
        if resp.status_code != 200:
            logger.warning("HTTP %s pour %s", resp.status_code, endpoint)
            return {}
        return resp.json()

    def get_recent_matches(
        self, competition_code: str, limit: int = 60, season: int | None = None
    ) -> list[dict]:
        """
        Fetch the most recent finished matches for a competition.
        Returns a list of match dicts with home/away team names and scores.

        `season` pins a specific season (its STARTING year, e.g. 2025 = 2025-26).
        When omitted, football-data returns the CURRENT season — which in the
        summer off-season has 0 finished matches. So when the current season is
        empty (and the caller didn't pin one), we fall back to the most recent
        COMPLETED season, so the model / backtest / calibration cold-start still
        get data year-round instead of going blind between June and August.
        """
        def _fetch(params: dict) -> list[dict]:
            data = self._get(f"competitions/{competition_code}/matches", params=params)
            return data.get("matches", [])

        params = {"status": "FINISHED", "limit": limit}
        if season is not None:
            params["season"] = season
        matches = _fetch(params)

        if not matches and season is None:
            from datetime import datetime, timezone
            yr = datetime.now(timezone.utc).year
            for cand in (yr - 1, yr - 2):   # last completed season, then the one before
                try:
                    fallback = _fetch({"status": "FINISHED", "limit": limit, "season": cand})
                except Exception as exc:
                    logger.debug("season-fallback %s %d: %s", competition_code, cand, exc)
                    fallback = []
                if fallback:
                    logger.info("  %s : saison courante vide → repli saison %d",
                                competition_code, cand)
                    matches = fallback
                    break

        logger.info(
            "  %s : %d matchs terminés récupérés", competition_code, len(matches)
        )
        return matches

    def get_all_leagues(self, limit: int = 60) -> dict[str, list[dict]]:
        """
        Fetch recent match data for all supported leagues.
        Returns {sport_key: [raw_match_dicts]}.
        """
        results: dict[str, list[dict]] = {}
        for sport_key, comp_code in LEAGUE_MAP.items():
            if comp_code is None:
                logger.info("  %s : pas de données football-data.org (CAN)", sport_key)
                continue
            if not self._key or "REMPLACE" in self._key:
                logger.warning(
                    "FOOTBALL_DATA_API_KEY non configurée — "
                    "le modèle Poisson sera désactivé pour %s",
                    sport_key,
                )
                continue
            try:
                matches = self.get_recent_matches(comp_code, limit=limit)
                results[sport_key] = matches
                time.sleep(6)  # free tier: 10 req/min → 6s entre requêtes
            except ValueError as exc:
                logger.error("%s", exc)
                break
            except Exception as exc:
                logger.error("Erreur pour %s : %s", sport_key, exc)
        return results


def parse_match_results(raw_matches: list[dict]) -> list[dict]:
    """
    Convert raw football-data.org match dicts into simplified records:
    {home_team, away_team, home_goals, away_goals, date}
    """
    parsed = []
    for m in raw_matches:
        try:
            score = m.get("score", {})
            full = score.get("fullTime", {})
            home_goals = full.get("home")
            away_goals = full.get("away")
            if home_goals is None or away_goals is None:
                continue
            parsed.append(
                {
                    "home_team": m["homeTeam"]["name"],
                    "away_team": m["awayTeam"]["name"],
                    "home_goals": int(home_goals),
                    "away_goals": int(away_goals),
                    "date": m.get("utcDate", ""),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return parsed
