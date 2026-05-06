"""
Periodic source health check — pings every external provider and pushes an
alert (email + Telegram) when one transitions from OK to KO.

Designed to run from the worker scheduler once a day (06:30 UTC, after the
weekly stats refresh). Stores last-known status in `data/source_health.json`
on disk so we only alert on TRANSITIONS, not every probe.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("betbot.source_health")

STATE_PATH = Path(os.getenv("SOURCE_HEALTH_STATE", "data/source_health.json"))


@dataclass
class SourceProbe:
    name: str
    ok: bool
    reason: str = ""


def _probe_all() -> list[SourceProbe]:
    """Probe every external provider. Returns one entry per source."""
    from betbot.config import load_settings
    s = load_settings()

    probes: list[SourceProbe] = []

    # Odds API
    try:
        from betbot.api import OddsAPIClient
        c = OddsAPIClient(s.odds_api_key)
        events = c.get_events_with_odds("soccer_epl")
        probes.append(SourceProbe("odds_api", len(events) >= 0))
    except Exception as exc:  # noqa: BLE001
        probes.append(SourceProbe("odds_api", False, str(exc)[:200]))

    # football-data.org
    try:
        from betbot.football_api import FootballDataClient
        c = FootballDataClient(s.football_data_api_key)
        m = c.get_recent_matches("PL", limit=1)
        probes.append(SourceProbe("football_data", len(m) > 0))
    except Exception as exc:
        probes.append(SourceProbe("football_data", False, str(exc)[:200]))

    # Club Elo
    try:
        from betbot.data_sources import club_elo
        n = len(club_elo.get_all_elo_ratings())
        probes.append(SourceProbe("club_elo", n > 100))
    except Exception as exc:
        probes.append(SourceProbe("club_elo", False, str(exc)[:200]))

    # Understat (the chronically-broken one)
    try:
        from betbot.data_sources import understat
        probes.append(SourceProbe("understat", understat.is_available()))
    except Exception as exc:
        probes.append(SourceProbe("understat", False, str(exc)[:200]))

    return probes


def _load_last_state() -> dict[str, bool]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open() as f:
            return {k: bool(v) for k, v in json.load(f).items()}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, bool]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def check_and_alert() -> dict:
    """
    Run all probes, compare to last-known state, alert on transitions.
    Returns a summary dict suitable for logging.
    """
    probes = _probe_all()
    last = _load_last_state()
    new_state = {p.name: p.ok for p in probes}

    transitioned_to_ko: list[SourceProbe] = []
    transitioned_to_ok: list[SourceProbe] = []

    for p in probes:
        was_ok = last.get(p.name, True)   # treat missing as OK to avoid spam on first run
        if was_ok and not p.ok:
            transitioned_to_ko.append(p)
        elif not was_ok and p.ok:
            transitioned_to_ok.append(p)

    # Save the new state regardless of whether we alerted
    _save_state(new_state)

    # Build alert messages
    if transitioned_to_ko or transitioned_to_ok:
        lines = ["BetBot — source health change"]
        for p in transitioned_to_ko:
            lines.append(f"  🔴 {p.name} : DOWN — {p.reason or 'no reason'}")
        for p in transitioned_to_ok:
            lines.append(f"  ✅ {p.name} : back UP")
        message = "\n".join(lines)
        logger.warning(message)

        # Email
        try:
            from betbot.config import load_settings
            from betbot.notifier import EmailNotifier
            s = load_settings()
            EmailNotifier(s.gmail_user, s.gmail_app_password, s.gmail_recipient).send(
                subject="[BetBot] Source health change",
                html=f"<pre>{message}</pre>",
            )
        except Exception as exc:
            logger.error("Could not send email alert: %s", exc)

        # Telegram
        try:
            from betbot.telegram_notifier import notify_alert
            notify_alert("Source health change", message)
        except Exception as exc:
            logger.error("Could not send Telegram alert: %s", exc)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_ko": sum(1 for p in probes if not p.ok),
        "n_ok": sum(1 for p in probes if p.ok),
        "transitioned_to_ko": [p.name for p in transitioned_to_ko],
        "transitioned_to_ok": [p.name for p in transitioned_to_ok],
    }
