"""Live external-source health probe — surfaces credential / outage state."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from betbot.config import load_settings
from betbot_api.auth import require_auth

router = APIRouter(tags=["health"])


@router.get("/health/sources")
def health_sources(_: str = Depends(require_auth)) -> dict:
    """
    Probe every external data source and report its status.

    Each source carries:
      - status: "ok" | "ko" | "not_configured"  (distinguishes credential issue from real outage)
      - criticality: "critical" | "important" | "optional"
        * critical  : without it the system stops working (odds_api)
        * important : degrades quality but doesn't break anything (football_data, club_elo)
        * optional  : only powers a specific feature (anthropic = AI agent, tavily = news)
      - latency_ms: how long the probe took
      - reason: error message when status != "ok"
    """
    import os
    import time
    from datetime import datetime, timezone
    from betbot.data_sources import club_elo, api_football
    from betbot.data_sources import xg as xg_source
    s = load_settings()

    # Per-probe timeout : a single slow source (Understat is notoriously
    # flaky) must not block the whole health endpoint. Each probe runs in
    # its own thread with a 4s wall-clock cap. Prevents the dashboard
    # "Erreur : timed out" we used to see when Understat was being slow.
    import concurrent.futures as _cf
    PROBE_TIMEOUT_SEC = 4

    def _probe(name: str, criticality: str, configured: bool, live_check) -> dict:
        if not configured:
            return {
                "name": name, "criticality": criticality,
                "status": "not_configured", "ok": False,
                "latency_ms": 0,
                "reason": "clé/credential absent — voir .env",
            }
        t0 = time.monotonic()
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(live_check)
                ok = bool(future.result(timeout=PROBE_TIMEOUT_SEC))
            return {
                "name": name, "criticality": criticality,
                "status": "ok" if ok else "ko",
                "ok": ok,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": "" if ok else "probe returned falsy result",
            }
        except _cf.TimeoutError:
            return {
                "name": name, "criticality": criticality,
                "status": "ko", "ok": False,
                "latency_ms": PROBE_TIMEOUT_SEC * 1000,
                "reason": f"probe timed out after {PROBE_TIMEOUT_SEC}s",
            }
        except Exception as exc:
            return {
                "name": name, "criticality": criticality,
                "status": "ko", "ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": str(exc)[:200],
            }

    sources = [
        _probe("odds_api",      "critical",  bool(s.odds_api_key),
               lambda: bool(s.odds_api_key)),
        _probe("football_data", "important", bool(s.football_data_api_key),
               lambda: bool(s.football_data_api_key)),
        _probe("club_elo",      "important", True,
               lambda: len(club_elo.get_all_elo_ratings()) > 100),
        _probe("xg",            "optional",  True,
               xg_source.is_available),
        _probe("api_football",  "optional",  bool(os.getenv("API_FOOTBALL_KEY")),
               api_football.is_available),
        _probe("tavily",        "optional",  bool(os.getenv("TAVILY_API_KEY")),
               lambda: bool(os.getenv("TAVILY_API_KEY"))),
        _probe("anthropic",     "optional",  bool(s.anthropic_api_key),
               lambda: bool(s.anthropic_api_key)),
    ]
    return {
        "sources": sources,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
