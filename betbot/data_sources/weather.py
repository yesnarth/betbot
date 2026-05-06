"""
Open-Meteo weather forecasts — https://open-meteo.com

Free, no API key, no rate limit. Returns hourly forecasts; we sample the hour
closest to kick-off. Heavy rain or strong wind dampens scoring meaningfully:
empirically, kickoff with >5mm rain reduces total goals by ~15%.

We use a small built-in lookup of stadium coordinates for the top European
clubs. Unknown stadiums fall back to "no weather adjustment".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TypedDict

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.data_sources.weather")

API_URL = "https://api.open-meteo.com/v1/forecast"

# Approximate stadium coordinates for top clubs. Lookup is on the normalized
# home-team name (same scheme as club_elo._normalize).
_STADIUM_COORDS: dict[str, tuple[float, float]] = {
    # Premier League
    "arsenal":          (51.5549, -0.1084),   # Emirates
    "manchestercity":   (53.4831, -2.2004),   # Etihad
    "manchesterunited": (53.4631, -2.2913),   # Old Trafford
    "liverpool":        (53.4308, -2.9608),   # Anfield
    "chelsea":          (51.4816, -0.1909),   # Stamford Bridge
    "tottenhamhotspur": (51.6043, -0.0664),   # Tottenham Hotspur Stadium
    "tottenham":        (51.6043, -0.0664),
    "newcastleunited":  (54.9756, -1.6217),   # St James' Park
    "astonvilla":       (52.5093, -1.8843),   # Villa Park
    "westhamunited":    (51.5386, -0.0166),   # London Stadium
    # La Liga
    "realmadrid":       (40.4530, -3.6883),   # Bernabéu
    "barcelona":        (41.3809, 2.1228),    # Camp Nou
    "atleticomadrid":   (40.4362, -3.5995),   # Metropolitano
    "athletic":         (43.2643, -2.9495),   # San Mamés
    "sevilla":          (37.3838, -5.9706),   # Sánchez-Pizjuán
    "valencia":         (39.4747, -0.3585),   # Mestalla
    # Serie A
    "internazionale":   (45.4781, 9.1240),    # San Siro
    "milan":            (45.4781, 9.1240),
    "juventus":         (45.1097, 7.6411),    # Allianz Stadium
    "roma":             (41.9341, 12.4549),   # Olimpico
    "lazio":            (41.9341, 12.4549),
    "napoli":           (40.8281, 14.1932),   # Maradona
    "atalanta":         (45.7093, 9.6810),    # Gewiss
    # Bundesliga
    "bayern":           (48.2188, 11.6247),   # Allianz Arena
    "borussia":         (51.4925, 7.4517),    # Signal Iduna Park (Dortmund)
    "leipzig":          (51.3458, 12.3486),   # Red Bull Arena
    "leverkusen":       (51.0381, 7.0023),    # BayArena
    # Ligue 1
    "paris":            (48.8414, 2.2530),    # Parc des Princes
    "marseille":        (43.2697, 5.3956),    # Vélodrome
    "lyon":             (45.7651, 4.9821),    # Groupama
    "lille":            (50.6121, 3.1306),    # Pierre Mauroy
    "monaco":           (43.7269, 7.4153),    # Louis-II
}


class MatchWeather(TypedDict):
    temperature_c: float
    precipitation_mm: float
    wind_kmh: float
    cloud_cover_pct: float
    will_rain_heavy: bool       # > 5 mm/h
    is_windy: bool              # > 35 km/h
    expected_goal_modifier: float  # multiplicative factor to apply on λ (0.85..1.05)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _fetch_forecast(lat: float, lon: float, target_iso: str) -> dict | None:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,wind_speed_10m,cloud_cover",
        "timezone": "UTC",
        "start_date": target_iso[:10],
        "end_date": target_iso[:10],
    }
    resp = requests.get(API_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_match_weather(home_team_normalized: str, kickoff_utc_iso: str) -> MatchWeather | None:
    """
    Return weather conditions at kick-off.

    home_team_normalized: result of betbot.data_sources.club_elo._normalize(home).
                          Fast-paths to None if the stadium isn't in our lookup.
    kickoff_utc_iso: ISO-8601 UTC timestamp ("2026-05-06T19:00:00Z").
    """
    coords = _STADIUM_COORDS.get(home_team_normalized)
    if coords is None:
        return None

    try:
        data = _fetch_forecast(coords[0], coords[1], kickoff_utc_iso)
    except Exception as exc:
        logger.warning("Weather fetch failed for %s : %s", home_team_normalized, exc)
        return None

    if not data or "hourly" not in data:
        return None

    hours = data["hourly"]["time"]
    target_hour_iso = kickoff_utc_iso[:13] + ":00"  # "2026-05-06T19:00"
    if target_hour_iso not in hours:
        return None
    idx = hours.index(target_hour_iso)

    temp = data["hourly"]["temperature_2m"][idx]
    precip = data["hourly"]["precipitation"][idx]
    wind = data["hourly"]["wind_speed_10m"][idx]
    clouds = data["hourly"]["cloud_cover"][idx]

    will_rain_heavy = precip > 5.0
    is_windy = wind > 35.0

    # Empirical multiplier on goal expectancy
    modifier = 1.0
    if will_rain_heavy:
        modifier *= 0.88
    elif precip > 1.0:
        modifier *= 0.95
    if is_windy:
        modifier *= 0.92

    return MatchWeather(
        temperature_c=temp,
        precipitation_mm=precip,
        wind_kmh=wind,
        cloud_cover_pct=clouds,
        will_rain_heavy=will_rain_heavy,
        is_windy=is_windy,
        expected_goal_modifier=round(modifier, 3),
    )
