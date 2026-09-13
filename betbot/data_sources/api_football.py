"""
API-Football — https://www.api-football.com  (via RapidAPI)

Provides probable line-ups, injuries, suspensions, and head-to-head records.
Free tier: 100 requests / day — enough for 1-2 scans of upcoming fixtures.

Activated only when API_FOOTBALL_KEY is set in .env. Without it, the rest of
the bot still works and the agent can simply skip these tools.

Reference docs: https://www.api-football.com/documentation-v3
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import TypedDict

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.data_sources.api_football")

BASE_URL = "https://v3.football.api-sports.io"


class APIFootballNotConfigured(RuntimeError):
    """Raised when API_FOOTBALL_KEY is missing."""


def _headers() -> dict:
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise APIFootballNotConfigured(
            "API_FOOTBALL_KEY not set. Sign up at https://rapidapi.com/api-sports/api/api-football "
            "and put the key in .env (free tier: 100 req/day)."
        )
    return {"x-apisports-key": key}


# Cumulative api-football call counter. The xG refresh is the only heavy
# consumer (~teams x 7 calls per league, ~45 leagues) and it must be budgeted
# ACROSS the whole run, not per league: a 400-call cap repeated 45 times is an
# 18,000-call cap, three times the daily allowance.
_CALL_COUNT = 0

# Sliding window of call timestamps, for the per-minute ceiling.
_CALL_TIMES: deque = deque()
_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_PER_MIN = 280   # Pro allows 300/min; leave headroom


def _rate_limit_per_min() -> int:
    try:
        return max(1, int(os.getenv("API_FOOTBALL_RATE_LIMIT_PER_MIN",
                                    str(_DEFAULT_RATE_LIMIT_PER_MIN))))
    except ValueError:
        return _DEFAULT_RATE_LIMIT_PER_MIN


def calls_made() -> int:
    """Total api-football HTTP calls issued in this process."""
    return _CALL_COUNT


def _throttle() -> None:
    """Stay under the plan's PER-MINUTE ceiling before issuing a call.

    The daily allowance is generous (7,500 on Pro) but the per-minute one is
    not, and a league-wide xG refresh is a burst of hundreds of calls. Measured
    2026-08-10: an enrichment run got through the leagues from "argentina" to
    "chile", hit the minute ceiling, and every league after that silently
    returned nothing — `_get` logged a warning and handed back `{}`, which
    reads exactly like "this league has no xG". 1,121 of 7,500 daily calls
    used, and the run still came home almost empty.
    """
    limit = _rate_limit_per_min()
    now = time.monotonic()
    while _CALL_TIMES and now - _CALL_TIMES[0] >= 60.0:
        _CALL_TIMES.popleft()
    if len(_CALL_TIMES) >= limit:
        wait = 60.0 - (now - _CALL_TIMES[0]) + 0.25
        if wait > 0:
            logger.info("api-football : plafond/minute atteint, pause %.1fs", wait)
            time.sleep(wait)
            now = time.monotonic()
            while _CALL_TIMES and now - _CALL_TIMES[0] >= 60.0:
                _CALL_TIMES.popleft()
    _CALL_TIMES.append(time.monotonic())


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _get(endpoint: str, params: dict | None = None) -> dict:
    global _CALL_COUNT
    _throttle()
    _CALL_COUNT += 1
    resp = requests.get(
        f"{BASE_URL}/{endpoint}",
        headers=_headers(),
        params=params or {},
        timeout=20,
    )
    if resp.status_code == 429:
        # Back off and retry rather than returning {} — an empty dict is
        # indistinguishable from "no data for this league" to every caller,
        # which is how a throttled run looked like a data-coverage problem.
        for attempt in range(_RATE_LIMIT_RETRIES):
            wait = 5.0 * (attempt + 1)
            logger.warning("API-Football 429 sur %s — pause %.0fs (essai %d/%d)",
                           endpoint, wait, attempt + 1, _RATE_LIMIT_RETRIES)
            time.sleep(wait)
            _CALL_TIMES.clear()   # the ceiling just moved; restart the window
            _CALL_COUNT += 1
            resp = requests.get(
                f"{BASE_URL}/{endpoint}",
                headers=_headers(),
                params=params or {},
                timeout=20,
            )
            if resp.status_code != 429:
                break
    if resp.status_code == 429:
        logger.error("API-Football : plafond toujours actif après %d essais sur %s",
                     _RATE_LIMIT_RETRIES, endpoint)
        return {}
    if resp.status_code != 200:
        logger.warning("API-Football HTTP %s on %s", resp.status_code, endpoint)
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Lineup(TypedDict):
    team_name: str
    formation: str
    starters: list[str]
    coach: str


def get_predicted_lineups(fixture_id: int) -> list[Lineup]:
    """
    Predicted starting XIs for an upcoming fixture (~24h before kick-off).
    fixture_id is the API-Football native ID — use search_fixture_id() to find it.
    """
    data = _get("fixtures/lineups", {"fixture": fixture_id})
    out: list[Lineup] = []
    for raw in data.get("response", []):
        starters = [
            p.get("player", {}).get("name", "")
            for p in raw.get("startXI", [])
        ]
        out.append(Lineup(
            team_name=raw.get("team", {}).get("name", ""),
            formation=raw.get("formation", ""),
            starters=[s for s in starters if s],
            coach=raw.get("coach", {}).get("name", ""),
        ))
    return out


class Injury(TypedDict):
    player: str
    team: str
    type: str       # "Missing Fixture" / "Questionable"
    reason: str


def get_team_injuries(team_id: int, league_id: int, season: int) -> list[Injury]:
    """List players ruled out / doubtful for an upcoming team match."""
    data = _get("injuries", {"team": team_id, "league": league_id, "season": season})
    out: list[Injury] = []
    for raw in data.get("response", []):
        out.append(Injury(
            player=raw.get("player", {}).get("name", ""),
            team=raw.get("team", {}).get("name", ""),
            type=raw.get("player", {}).get("type", ""),
            reason=raw.get("player", {}).get("reason", ""),
        ))
    return out


def get_h2h(team1_id: int, team2_id: int, last: int = 10) -> list[dict]:
    """
    Last N head-to-head matches between two teams (any competition).
    Useful for the agent to spot rivalries or stylistic mismatches.
    """
    data = _get("fixtures/headtohead", {"h2h": f"{team1_id}-{team2_id}", "last": last})
    return [
        {
            "date": f.get("fixture", {}).get("date"),
            "league": f.get("league", {}).get("name"),
            "home": f.get("teams", {}).get("home", {}).get("name"),
            "away": f.get("teams", {}).get("away", {}).get("name"),
            "home_goals": f.get("goals", {}).get("home"),
            "away_goals": f.get("goals", {}).get("away"),
        }
        for f in data.get("response", [])
    ]


def search_team_id(name: str, league_id: int | None = None) -> int | None:
    """Resolve a team name into its API-Football ID. Cached in-process."""
    params = {"search": name}
    if league_id:
        params["league"] = league_id
    data = _get("teams", params)
    rows = data.get("response", [])
    if not rows:
        return None
    return rows[0].get("team", {}).get("id")


# ---------------------------------------------------------------------------
# Expected goals (xG) — reliable, API-sourced. api-football exposes xG only
# PER FIXTURE (fixtures/statistics → {"type":"expected_goals","value":"1.23"}),
# so a team's xG form is aggregated over its recent finished fixtures.
# ---------------------------------------------------------------------------

# sport_key → api-football league id
SPORT_TO_LEAGUE_ID: dict[str, int] = {
    "soccer_epl": 39,
    "soccer_spain_la_liga": 140,
    "soccer_germany_bundesliga": 78,
    "soccer_italy_serie_a": 135,
    "soccer_france_ligue1": 61,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
    "soccer_efl_champ": 40,
}

# The Odds API sport_key → api-football league id, for IN-SEASON leagues that
# football-data.org's free tier does NOT cover (Scandinavian / Asian / South-
# American summer calendars, plus a few smaller European ones). These play
# through the European off-season (June-August), so without them the model
# has NO team data mid-summer and degrades to the market-consensus fallback.
#
# Every id below was verified against api-football /leagues (correct country)
# on 2026-07-26. Leagues whose current season is still thin (just kicked off)
# are included too — the refresh skips them via its min_matches guard and they
# activate automatically once they accumulate enough finished matches.
IN_SEASON_LEAGUE_ID: dict[str, int] = {
    "soccer_norway_eliteserien": 103,
    "soccer_sweden_allsvenskan": 113,
    "soccer_sweden_superettan": 114,
    "soccer_finland_veikkausliiga": 244,
    "soccer_brazil_campeonato": 71,        # Brazil Série A
    "soccer_brazil_serie_b": 72,
    "soccer_usa_mls": 253,
    "soccer_mexico_ligamx": 262,
    "soccer_japan_j_league": 98,           # J1 League
    "soccer_korea_kleague1": 292,
    "soccer_china_superleague": 169,
    "soccer_switzerland_superleague": 207,
    "soccer_russia_premier_league": 235,
    "soccer_poland_ekstraklasa": 106,
    "soccer_denmark_superliga": 119,
    "soccer_austria_bundesliga": 218,
    "soccer_belgium_first_div": 144,
    "soccer_argentina_primera_division": 128,
    "soccer_chile_campeonato": 265,
    "soccer_greece_super_league": 197,
    "soccer_italy_serie_b": 136,
    "soccer_league_of_ireland": 357,
    "soccer_spl": 179,                     # Scottish Premiership
    # Added 2026-08-07. Second tiers and Turkey — in season, quoted by the
    # books, and previously invisible to the model: absent from this map they
    # had no team stats at all and every match ran on market consensus.
    # IDs resolved against the api-football /leagues endpoint and eyeballed
    # against the competition NAME, never guessed: 79 is "2. Bundesliga" and
    # 1034 is the women's league; 141 is "Segunda División" and 875-877 are
    # the RFEF regional groups. A wrong id trains the model on another
    # competition entirely, in silence.
    "soccer_england_league1": 41,          # League One (D3)
    "soccer_england_league2": 42,          # League Two (D4)
    "soccer_france_ligue_two": 62,         # Ligue 2
    "soccer_germany_bundesliga2": 79,      # 2. Bundesliga
    "soccer_germany_liga3": 80,            # 3. Liga
    "soccer_spain_segunda_division": 141,  # Segunda División
    # NB : soccer_turkey_super_league (203) était déjà mappé plus bas — son
    # absence de stats venait de la frontière de saison, pas du mapping.
    # Added 2026-08-01. These competitions were absent from BOTH resolution
    # maps, so their picks could never be graded: 17 stuck on Champions League
    # qualifying alone, 30 unresolved totals picks in total — about +45% of
    # already-acquired sample that was simply never read, concentrated on
    # exactly the segment where evidence was missing (consensus path, heavy
    # favourites, O3.5). IDs verified against the api-football /leagues
    # endpoint, not guessed — a wrong ID grades picks against the wrong
    # competition, which is worse than not grading them.
    "soccer_uefa_champs_league_qualification": 2,   # UEFA CL — qualifying rounds included
    "soccer_england_efl_cup": 48,                   # League Cup (England)
    "soccer_conmebol_copa_sudamericana": 11,        # CONMEBOL Sudamericana
    "soccer_conmebol_copa_libertadores": 13,        # CONMEBOL Libertadores
    "soccer_turkey_super_league": 203,              # Süper Lig
}


def get_finished_matches(
    league_id: int, season: int, max_pages: int = 10,
) -> list[dict]:
    """Finished (FT) fixtures for a league+season, in the SAME shape as
    football_api.parse_match_results: {home_team, away_team, home_goals,
    away_goals, date}, sorted most-recent first.

    Feeds the exact same downstream pipeline as the football-data path
    (compute_league_averages → build_team_stats → compute_elo_ratings), so the
    blended model runs on these leagues with zero model-code duplication.
    """
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        params = {"league": league_id, "season": season, "status": "FT"}
        if page > 1:  # api-football returns 0 results if page=1 is sent explicitly
            params["page"] = page
        data = _get("fixtures", params)
        resp = data.get("response", [])
        for f in resp:
            teams = f.get("teams", {})
            goals = f.get("goals", {})
            home = (teams.get("home") or {}).get("name")
            away = (teams.get("away") or {}).get("name")
            hg, ag = goals.get("home"), goals.get("away")
            if not home or not away or hg is None or ag is None:
                continue
            try:
                out.append({
                    "home_team": home,
                    "away_team": away,
                    "home_goals": int(hg),
                    "away_goals": int(ag),
                    "date": (f.get("fixture") or {}).get("date", ""),
                })
            except (ValueError, TypeError):
                continue
        paging = data.get("paging", {}) or {}
        if page >= int(paging.get("total", 1) or 1):
            break
        page += 1
    out.sort(key=lambda m: m["date"], reverse=True)
    return out

_XG_LEAGUE_CACHE: dict[tuple, list] = {}


def _current_season_year() -> int:
    from datetime import date
    t = date.today()
    return t.year if t.month >= 8 else t.year - 1


def get_fixture_xg(fixture_id: int) -> dict[int, float]:
    """{team_id: expected_goals} for a fixture. Empty when the league has no xG."""
    data = _get("fixtures/statistics", {"fixture": fixture_id})
    out: dict[int, float] = {}
    for raw in data.get("response", []):
        tid = raw.get("team", {}).get("id")
        if tid is None:
            continue
        for stat in raw.get("statistics", []):
            if stat.get("type") == "expected_goals":
                val = stat.get("value")
                try:
                    if val not in (None, ""):
                        out[tid] = float(val)
                except (ValueError, TypeError):
                    pass
    return out


def get_recent_team_xg(
    team_id: int, league_id: int, season: int, last: int = 6,
) -> dict | None:
    """Aggregate a team's xG for/against over its last `last` finished fixtures.
    Returns {matches, xg_per_match, xga_per_match} or None if no xG was found.

    Falls back to the previous season when the current one has not produced
    enough finished fixtures yet. Without this, every autumn-spring league is
    xG-blind for the first two months of its season — the same season-boundary
    hole that left 22 leagues without team stats, one layer down. Measured
    2026-08-10: a first pass over 43 leagues spent 385 calls and filled 67
    teams, because most leagues answered "no finished fixtures" after a single
    request.
    """
    fixtures: list = []
    for candidate_season in (season, season - 1):
        try:
            fx = _get("fixtures", {"team": team_id, "league": league_id,
                                   "season": candidate_season,
                                   "last": last, "status": "FT"})
        except Exception:
            return None
        fixtures = fx.get("response", []) or []
        if fixtures:
            break
    xgf = xga = 0.0
    n = 0
    for f in fixtures:
        fid = f.get("fixture", {}).get("id")
        home_id = f.get("teams", {}).get("home", {}).get("id")
        away_id = f.get("teams", {}).get("away", {}).get("id")
        opp_id = away_id if home_id == team_id else home_id
        if fid is None or opp_id is None:
            continue
        try:  # a single fixture-stats timeout must not drop the whole team
            xg = get_fixture_xg(fid)
        except Exception:
            continue
        if team_id in xg and opp_id in xg:
            xgf += xg[team_id]
            xga += xg[opp_id]
            n += 1
    if n == 0:
        return None
    return {"matches": n, "xg_per_match": round(xgf / n, 3),
            "xga_per_match": round(xga / n, 3)}


_STATUS_CACHE: dict = {"at": 0.0, "value": None}
_STATUS_TTL_S = 3600.0


def account_status(force: bool = False) -> dict:
    """Truthful account state: plan, subscription end, daily consumption.

    Cached for an hour because /status costs a request like any other, and the
    health endpoint is polled every few seconds — reading the quota must not be
    what exhausts it.

    Two traps this function exists to avoid, both met head-on on 2026-08-18:

    * The RESPONSE HEADERS lie about consumption. With the daily allowance
      spent, api-football still advertised `x-ratelimit-requests-remaining:
      7499` out of 7500 while refusing every endpoint. The headers carry the
      plan's nominal limits, not the account's state.
    * The BODY is the truth, and it says so in `errors.requests`. When the
      daily limit is reached the payload has an empty `response`, so anything
      reading `response.subscription` sees None and concludes "unknown" — which
      is how an exhausted quota passes for a configuration problem.
    """
    import time

    now = time.monotonic()
    if not force and _STATUS_CACHE["value"] is not None:
        if now - _STATUS_CACHE["at"] < _STATUS_TTL_S:
            return _STATUS_CACHE["value"]

    out: dict = {"state": "unknown", "plan": None, "active": None,
                 "subscription_end": None, "days_left": None,
                 "requests_used": None, "requests_limit": None}

    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        out["state"] = "unconfigured"
        _STATUS_CACHE.update(at=now, value=out)
        return out

    try:
        resp = requests.get(f"{BASE_URL}/status",
                            headers={"x-apisports-key": key}, timeout=15)
        data = resp.json()
    except Exception:  # noqa: BLE001 — never let a status probe break a caller
        out["state"] = "unreachable"
        _STATUS_CACHE.update(at=now, value=out)
        return out

    errors = data.get("errors") or {}
    if isinstance(errors, dict) and "requests" in errors:
        # The daily allowance is spent. The plan itself is fine.
        out["state"] = "daily_limit_reached"
        _STATUS_CACHE.update(at=now, value=out)
        return out

    body = data.get("response") or {}
    if isinstance(body, dict) and body:
        sub = body.get("subscription") or {}
        req = body.get("requests") or {}
        out["plan"] = sub.get("plan")
        out["active"] = sub.get("active")
        out["subscription_end"] = sub.get("end")
        out["requests_used"] = req.get("current")
        out["requests_limit"] = req.get("limit_day")
        out["days_left"] = _days_until(out["subscription_end"])
        out["state"] = "ok" if sub.get("active") else "inactive"

    _STATUS_CACHE.update(at=now, value=out)
    return out


def _days_until(iso: str | None) -> int | None:
    """Whole days from now to an ISO timestamp; negative once past."""
    from datetime import datetime, timezone

    if not iso:
        return None
    try:
        end = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return int((end - datetime.now(timezone.utc)).total_seconds() // 86400)


def league_id_for(sport_key: str) -> int | None:
    """api-football league id for a sport_key, from EITHER map.

    Two maps grew side by side — `SPORT_TO_LEAGUE_ID` for xG and
    `IN_SEASON_LEAGUE_ID` for team stats — and the xG one was never extended
    past its original 8 leagues. Measured 2026-08-10: 94 of 816 teams carried
    xG (11.5%) while the scan covered 46 leagues, purely because xG resolution
    stopped at the smaller map. One resolver, both maps.
    """
    from betbot.sport_keys import to_canonical
    key = to_canonical(sport_key)
    return SPORT_TO_LEAGUE_ID.get(key) or IN_SEASON_LEAGUE_ID.get(key)


def get_league_xg(sport_key: str, year: int | None = None, last: int = 6,
                  max_calls: int = 400) -> list[dict]:
    """Per-team recent-form xG for a whole league, shaped like the Understat
    source ({title, xg_per_match, xga_per_match, matches}). Drop-in replacement.

    Heavy (≈ teams × (1 + last) calls) — guarded by `max_calls` and a 24h
    in-process cache. Returns [] when the league isn't mapped or has no xG.
    """
    league_id = league_id_for(sport_key)
    if not league_id:
        return []
    season = year or _current_season_year()
    ck = (sport_key, season, last)
    if ck in _XG_LEAGUE_CACHE:
        return _XG_LEAGUE_CACHE[ck]

    try:
        teams = _get("teams", {"league": league_id, "season": season}).get("response", [])
    except Exception as exc:
        logger.warning("api-football xG: teams list failed for %s (%s)", sport_key, exc)
        return []
    out: list[dict] = []
    calls = 1
    for row in teams:
        if calls >= max_calls:
            logger.warning("api-football xG: call budget (%d) reached for %s", max_calls, sport_key)
            break
        team = row.get("team", {})
        tid, name = team.get("id"), team.get("name", "")
        if tid is None:
            continue
        try:  # one team's failure must not abort the whole league
            agg = get_recent_team_xg(tid, league_id, season, last=last)
        except Exception:
            agg = None
        calls += 1 + last
        if agg:
            out.append({"title": name, "matches": agg["matches"],
                        "xg_per_match": agg["xg_per_match"],
                        "xga_per_match": agg["xga_per_match"]})
    if out:
        _XG_LEAGUE_CACHE[ck] = out
        logger.info("api-football xG %s saison %d : %d équipes (%d appels)",
                    sport_key, season, len(out), calls)
    return out


def get_recent_fixture_dates(team_id: int, last: int = 6) -> list:
    """Datetimes of a team's last `last` FINISHED fixtures across ALL competitions
    (league + cups + Europe), sorted ascending. No league filter on purpose — a
    midweek Champions League match must count toward fixture congestion."""
    from datetime import datetime

    fx = _get("fixtures", {"team": team_id, "last": last, "status": "FT"})
    out: list = []
    for f in fx.get("response", []):
        d = (f.get("fixture") or {}).get("date")
        if not d:
            continue
        try:
            out.append(datetime.fromisoformat(str(d).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            continue
    return sorted(out)


def is_available() -> bool:
    """Cheap liveness check for /health — confirms the key works via /status."""
    try:
        data = _get("status")
    except APIFootballNotConfigured:
        return False
    except Exception:
        return False
    return bool(data.get("response"))


def get_standings(league_id: int, season: int) -> dict[str, dict]:
    """
    Classement d'une ligue : {nom d'équipe: {rank, points, played, form}}.

    UN SEUL APPEL couvre toute la ligue — contrairement aux blessures qui
    coûtent un appel par équipe. C'est ce qui rend ce signal finançable sur 46
    ligues : 46 requêtes par rafraîchissement contre 7 500/jour disponibles.

    `form` est la chaîne des derniers résultats vue par le fournisseur, la plus
    récente à DROITE (ex. « WWDLW »). C'est le seul signal réellement nouveau
    ici : le classement lui-même double largement l'ELO et les forces
    attaque/défense, alors que l'élan sur les derniers résultats n'est capturé
    nulle part dans le blend.
    """
    try:
        data = _get("standings", {"league": league_id, "season": season})
    except Exception as exc:                      # jamais bloquant pour un scan
        logger.debug("standings %s/%s indisponible : %s", league_id, season, exc)
        return {}
    out: dict[str, dict] = {}
    for entry in (data.get("response") or []):
        for group in ((entry.get("league") or {}).get("standings") or []):
            for row in group or []:
                name = ((row.get("team") or {}).get("name") or "").strip()
                if not name:
                    continue
                out[name] = {
                    "rank": row.get("rank"),
                    "points": row.get("points"),
                    "played": ((row.get("all") or {}).get("played")),
                    "form": (row.get("form") or ""),
                }
    return out


def get_topscorers(league_id: int, season: int, top: int = 20) -> dict[str, list[str]]:
    """
    Meilleurs buteurs d'une ligue : {nom d'équipe: [noms de joueurs]}.

    Un appel par ligue, comme le classement. Sert à PONDÉRER les absences : le
    modèle comptait les absents sans jamais regarder QUI manquait, si bien que
    la sortie du meilleur buteur et celle d'un troisième gardien pesaient
    identiquement. Les buteurs d'une ligue sont une approximation grossière de
    l'importance offensive, mais c'est la seule qui tienne en un appel.
    """
    try:
        data = _get("players/topscorers", {"league": league_id, "season": season})
    except Exception as exc:
        logger.debug("topscorers %s/%s indisponible : %s", league_id, season, exc)
        return {}
    out: dict[str, list[str]] = {}
    for row in (data.get("response") or [])[:top]:
        player = ((row.get("player") or {}).get("name") or "").strip()
        stats = (row.get("statistics") or [{}])[0]
        team = ((stats.get("team") or {}).get("name") or "").strip()
        if player and team:
            out.setdefault(team, []).append(player)
    return out
