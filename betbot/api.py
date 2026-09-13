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

# Markets requested per league. The Odds API bills the CROSS PRODUCT of
# regions x markets, so this string is a price, not just a shopping list —
# it must stay in sync with the pre-flight cost estimate. Single source of
# truth for both the fetch and the gate.
ODDS_MARKETS = "h2h,totals"


def per_league_cost() -> int:
    """Credits one league costs in a scan: regions x markets.

    With ODDS_REGIONS="eu,uk" and h2h+totals that is 4 credits per league —
    so a 46-league scan bills 184, not 46. Callers that show quota to the user
    MUST price the next scan with this, otherwise "100 credits left" reads as
    healthy while the next scan is already unaffordable.
    """
    regions = len([r for r in os.getenv("ODDS_REGIONS", "eu").split(",") if r.strip()])
    markets = len([m for m in ODDS_MARKETS.split(",") if m.strip()])
    return (regions * markets) or 1


SPORT_KEYS = [
    # Football — full Poisson model (xG/ELO/Tavily news/weather all wired)
    "soccer_france_ligue1",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_uefa_champs_league",
    "soccer_africa_cup_of_nations",
    # English Championship — D2 anglaise. football-data.org free tier (ELC).
    # ~24 équipes, ~46 matchs/an chacune. Marchés Pinnacle / Bet365 / Unibet
    # quotent quasi tous les matchs. Bonne profondeur statistique pour le
    # Poisson, légèrement moins liquide que la PL (édges souvent +1-2%
    # plus larges).
    "soccer_efl_champ",
    # Eredivisie 🇳🇱 — D1 néerlandaise. football-data.org free tier (DED).
    # 18 équipes, marchés serrés. Calendrier qui complète bien les fenêtres
    # samedi soir / dimanche après-midi déjà couvertes par PL/Liga/Serie A.
    "soccer_netherlands_eredivisie",
    # Primeira Liga 🇵🇹 — D1 portugaise. football-data.org free tier (PPL).
    # 18 équipes, dont l'axe Porto / Sporting / Benfica qui draine la liquidité.
    # Marchés bookmakers très complets, dont O/U 1.5/3.5 que la phase 10
    # essaie de capturer.
    "soccer_portugal_primeira_liga",
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


def _prefilter_upcoming() -> bool:
    """Skip leagues with no match in the window, using the free listing."""
    return os.getenv("PREFILTER_UPCOMING", "1") == "1"


def _min_before_kickoff() -> int:
    try:
        return int(os.getenv("MIN_BEFORE_KICKOFF", "60"))
    except ValueError:
        return 60


def _scan_all_soccer() -> bool:
    """When SCAN_ALL_SOCCER=1, scan EVERY in-season `soccer_*` league The Odds
    API lists (discovered for FREE via get_active_sports), not just the curated
    wishlist. Leagues without football-data.org coverage fall back to the
    consensus model. Read each call so .env changes take effect at runtime.
    """
    return os.getenv("SCAN_ALL_SOCCER", "0") == "1"


class QuotaExhaustedError(Exception):
    pass


class OddsAPIServerError(Exception):
    """Raised on HTTP 5xx from The Odds API — distinguishes real upstream
    failure from successful-but-empty responses. Callers should treat this
    as a transient outage and let it surface, not silently swallow it."""
    pass


class OddsAPIClient:
    def __init__(self, api_key: str, *, force_key: bool = False):
        # Store the passed key as a fallback. The effective key (`self._key`) is
        # resolved dynamically per request so a dashboard-set runtime override
        # takes effect WITHOUT restarting the client — see runtime_config.
        # `force_key=True` pins this exact key (used to validate a NEW key before
        # saving it, bypassing any existing override).
        self._explicit_key = api_key
        self._force_key = force_key
        # -1 = unknown until the first response with a x-requests-remaining
        # header is observed. A misleading default like 9999 used to surface
        # in the dashboard as "OK" even when the probe had failed.
        self.quota_remaining: int = -1
        self.quota_exhausted: bool = False
        # Key picked by the automatic rotation for the current scan. Reset at
        # the start of every fetch_all_sports so a fresh scan re-evaluates all
        # candidates — a key that was dry yesterday may have reset since.
        self._selected_key: str | None = None
        self._session = requests.Session()

    @property
    def _key(self) -> str:
        """Effective API key.

        Order: forced key → rotation-selected key → dashboard override →
        explicit key. The rotation slot sits above the override because the
        override IS the first rotation candidate — if it was selected, they
        agree; if it was skipped, it had no budget left.
        """
        if self._force_key:
            return self._explicit_key
        if self._selected_key:
            return self._selected_key
        from betbot.runtime_config import get_odds_api_key_override
        return get_odds_api_key_override() or self._explicit_key

    def _candidate_keys(self) -> list[str]:
        """Every key the rotation may use, in priority order, deduplicated.

        1. The dashboard override (explicit user intent, always tried first).
        2. ODDS_API_KEYS — comma-separated pool, the automation the user asked
           for: paste every account's key once, never rotate by hand again.
        3. ODDS_API_KEY — the historical single key.
        """
        keys: list[str] = []
        if not self._force_key:
            from betbot.runtime_config import get_odds_api_key_override
            override = get_odds_api_key_override()
            if override:
                keys.append(override)
        pool = os.getenv("ODDS_API_KEYS", "")
        keys.extend(k.strip() for k in pool.split(",") if k.strip())
        if self._explicit_key:
            keys.append(self._explicit_key)
        seen: set[str] = set()
        ordered: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        return ordered

    def _probe_key(self, key: str) -> int:
        """Remaining quota for one specific key. Free (uses the /sports probe)."""
        return OddsAPIClient(key, force_key=True).probe_quota()

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
            # Normalise at the boundary so the whole codebase only ever sees the
            # internal spelling. Without this, `soccer_france_ligue_one` never
            # matched the `soccer_france_ligue1` rows in team_stats and every
            # Ligue 1 fixture silently fell through to the consensus model.
            from betbot.sport_keys import to_canonical
            return {
                to_canonical(item["key"])
                for item in data
                if isinstance(item, dict) and "key" in item
            }
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

    def get_events_with_odds(self, sport: str, markets: str = ODDS_MARKETS) -> list[dict]:
        """
        Fetch odds for the requested markets across all available bookmakers.

        Default `markets` covers what The Odds API reliably supports for soccer:
          - h2h    : 1/X/2 (match winner)
          - totals : Over/Under (we filter on the 2.5 line in extract_best_odds)

        BTTS (Both Teams To Score) is calculated by the model but NOT requested
        here: this BULK endpoint only serves the featured markets (h2h, totals,
        spreads, outrights) and rejects `btts` with INVALID_MARKET. The old
        conclusion "we'd need a different odds provider" was WRONG — btts,
        double_chance and draw_no_bet ARE served by The Odds API, only through
        the per-event endpoint /events/{id}/odds, and probed live 2026-09-10
        they are quoted on our matches by eu books (pinnacle, williamhill,
        codere_it, matchbook, onexbet). Billing there is per market RETURNED,
        so an uncovered match costs nothing.

        Each additional market costs the same as one h2h-only request in their
        billing model.

        Regions come from `ODDS_REGIONS` (default "eu", the historical value).
        This matters when `BOOKMAKER_WHITELIST` is set: a bookmaker that is not
        in a requested region is simply absent from the response, so the
        whitelist would silently match nothing. "eu,uk" was originally adopted
        for Bet365 — but bet365 has since VANISHED from the feed entirely
        (measured 2026-09-10: absent from a live eu,uk response, absent from
        the official uk bookmaker list, and 0 of 386 all-time picks ever had
        it as best book — the whitelist is de facto Betclic alone). The uk
        region is kept anyway because it feeds the no-vig consensus with 17
        extra books (skybet, paddypower, ladbrokes, smarkets, betfair_ex_uk…):
        information, not price — the owner bets Betclic only, the model reads
        every book.

        COST: The Odds API bills regions × markets. "eu,uk" with "h2h,totals"
        costs 4 credits per sport instead of 2 — halving the number of sports a
        monthly quota can cover.
        """
        # Callers pass the INTERNAL key (that is what predictions and team_stats
        # store); the provider needs its own spelling. See betbot/sport_keys.py.
        from betbot.sport_keys import to_api
        url = f"{BASE_URL}/{to_api(sport)}/odds"
        params = {
            "apiKey": self._key,
            "regions": os.getenv("ODDS_REGIONS", "eu"),
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
        from betbot.sport_keys import to_api
        url = f"{BASE_URL}/{to_api(sport)}/scores"
        params = {
            "apiKey": self._key,
            "daysFrom": min(max(days_from, 1), 3),
            "dateFormat": "iso",
        }
        data = self._get(url, params)
        events = data if isinstance(data, list) else []
        return events

    def get_events(self, sport: str) -> list[dict]:
        """Fixture list for a sport — id, teams, kickoff. NO odds, NO COST.

        The Odds API bills `/odds` per region x market but serves `/events` for
        FREE (verified against the live `x-requests-last` header: 0). The bot
        never used it, so every scan paid to discover which leagues had a match
        at all: measured 2026-08-19, one scan bought 507 fixtures across 45
        leagues to keep the 6 kicking off that day.
        """
        from betbot.sport_keys import to_api
        url = f"{BASE_URL}/{to_api(sport)}/events"
        data = self._get(url, {"apiKey": self._key, "dateFormat": "iso"})
        return data if isinstance(data, list) else []

    def get_participants(self, sport: str) -> list[str]:
        """Canonical team names as The Odds API spells them — 1 credit.

        The provider's own whitelist of participants for a competition. This
        is the ground truth the team-name matcher should be validated AGAINST:
        every false match caught in production (Dundee, Makhachkala, and the
        2026-09-08 Champions-League night: Barcelona SC, Sporting Gijon,
        RED Star FC 93) was a provider spelling meeting our api-football rows
        for the first time inside a live pick. `betbot.participants_audit`
        replays these names through the REAL production matcher offline
        instead, so a wrong resolution costs a report line, not a lost bet.

        The list may include inactive clubs (relegated, historical) — the
        provider documents it as a whitelist, not a current-season roster.
        """
        from betbot.sport_keys import to_api
        url = f"{BASE_URL}/{to_api(sport)}/participants"
        data = self._get(url, {"apiKey": self._key})
        if not isinstance(data, list):
            return []
        return [p.get("full_name") or p.get("name") or "" for p in data
                if isinstance(p, dict)]

    def leagues_with_upcoming(
        self, sports: list[str], min_before_kickoff: int = 60,
    ) -> list[str]:
        """Subset of `sports` that actually has a match in today's window.

        One free call per league, so the paid scan can skip the leagues that
        would have contributed nothing. A league whose free listing FAILS is
        kept, not dropped: a network hiccup must never silently shrink the
        scan — that is the selection bias the pre-flight quota gate exists to
        prevent, arriving through another door.
        """
        from betbot.shared import filter_upcoming_today

        keep: list[str] = []
        for sport in sports:
            try:
                events = self.get_events(sport)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Listing gratuit échoué pour %s (%s) — ligue "
                               "conservée par prudence", sport, exc)
                keep.append(sport)
                continue
            if filter_upcoming_today(events, min_before_kickoff):
                keep.append(sport)
        return keep

    def fetch_all_sports(self) -> dict[str, list[dict]]:
        """Fetch odds for the wishlist, INTERSECTED with currently-active sports.

        Out-of-season leagues (e.g. Premier League in July) are silently
        skipped — saves quota with no degradation in coverage.
        """
        wishlist = _enabled_sport_keys()
        active = self.get_active_sports()  # free probe
        if active:
            to_query = [s for s in wishlist if s in active]
            # SCAN_ALL_SOCCER : add EVERY in-season soccer league the API lists
            # (discovered for free via the probe above), beyond the curated
            # wishlist. New leagues without football-data.org stats degrade
            # gracefully to the consensus model in detect_value_bets.
            if _scan_all_soccer():
                extra = sorted(
                    s for s in active
                    if s.startswith("soccer_") and s not in to_query
                )
                if extra:
                    logger.info("SCAN_ALL_SOCCER : +%d ligue(s) foot en saison", len(extra))
                    to_query = to_query + extra
            skipped = [s for s in wishlist if s not in active]
            if skipped:
                logger.info("Skipping out-of-season sports : %s", ", ".join(skipped))
        else:
            # Probe failed — fall back to wishlist (we'd rather over-query than miss data)
            logger.warning("Active-sports probe returned empty; falling back to full wishlist")
            to_query = wishlist

        # FREE PRE-FILTER — pay only for leagues that actually play today.
        #
        # `get_active_sports` says "in season", which is not "has a match in
        # the next few hours". Measured 2026-08-19 at 15:52: 45 in-season
        # leagues, 507 fixtures bought, 6 usable. The `/events` endpoint lists
        # fixtures for zero credits, so the question "is anyone playing?" no
        # longer has to be answered with money.
        #
        # Runs BEFORE the quota gate on purpose: the gate should price the scan
        # that will actually happen, not a hypothetical one over every league.
        if _prefilter_upcoming() and to_query:
            before = len(to_query)
            narrowed = self.leagues_with_upcoming(
                to_query, min_before_kickoff=_min_before_kickoff())
            if narrowed:
                to_query = narrowed
                if before != len(to_query):
                    logger.info(
                        "Pré-filtre gratuit : %d ligue(s) sur %d ont un match "
                        "dans la fenêtre — %d crédits économisés",
                        len(to_query), before,
                        (before - len(to_query)) * per_league_cost())
            else:
                logger.info("Pré-filtre gratuit : aucune ligue ne joue dans la "
                            "fenêtre — scan annulé, 0 crédit dépensé")
                return {}

        # PRE-FLIGHT — refuse a scan the quota cannot cover END TO END.
        #
        # `to_query` is sorted, so a run that dies halfway always drops the SAME
        # tail of leagues. That is not a coverage gap, it is a selection bias
        # stable over time: the affected leagues would be permanently
        # under-represented in the track record and in the calibrator's training
        # set, and nothing on screen would say so.
        #
        # Cost is regions x markets per league (ODDS_REGIONS defaults to "eu";
        # "eu,uk" doubles it — kept for the 17 uk consensus books, bet365
        # itself no longer exists in the feed). Better to refuse a scan
        # outright and let the user rotate their key than to silently record a
        # skewed one.
        per_league = per_league_cost()
        needed = len(to_query) * per_league
        quota_min = _quota_minimum()

        # AUTOMATIC KEY ROTATION. The user maintains several free Odds API
        # accounts; until now they rotated the key BY HAND whenever a scan was
        # refused — the exact chore this pre-flight kept sending them to do.
        # Here every candidate key is probed (free) in priority order and the
        # first one whose budget covers the WHOLE scan is selected. A key with
        # unknown quota (-1, header never observed) is accepted rather than
        # skipped, same philosophy as before: refusing on ignorance would brick
        # the client on a cold start.
        self._selected_key = None  # a dry key yesterday may have reset today
        candidates = self._candidate_keys()
        probed: list[tuple[str, int]] = []
        for key in candidates:
            remaining = self._probe_key(key) if len(candidates) > 1 else self.probe_quota()
            probed.append((key, remaining))
            if remaining < 0 or remaining - quota_min >= needed:
                if len(candidates) > 1:
                    self._selected_key = key
                    logger.info(
                        "Rotation de clé : clé …%s sélectionnée (%s restantes, "
                        "%d nécessaires)", key[-4:],
                        "?" if remaining < 0 else remaining, needed,
                    )
                break
        else:
            summary = ", ".join(
                f"…{k[-4:]}={'?' if r < 0 else r}" for k, r in probed
            )
            raise QuotaExhaustedError(
                f"Scan refusé : {len(to_query)} ligue(s) × {per_league} = "
                f"{needed} requêtes nécessaires, mais aucune des "
                f"{len(probed)} clé(s) n'a ce budget (réserve {quota_min} ; "
                f"restants par clé : {summary}). Un scan tronqué couperait "
                f"toujours les mêmes ligues et fausserait ton historique. "
                f"Les quotas se réinitialisent chaque mois — ajoute une clé "
                f"dans ODDS_API_KEYS, réduis SCAN_ALL_SOCCER, ou repasse "
                f"ODDS_REGIONS à 'eu' (tu perds alors les 17 books uk du "
                f"consensus, pas un prix jouable)."
            )

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
