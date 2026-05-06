"""
Telegram notifier — push the same scan results that go to email, plus
opportunistic alerts (CLV negative, source down, stop-loss tripped).

Activated when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env.
Without them, every send_* call is a no-op (logs at DEBUG).

Setup:
  1. Talk to @BotFather on Telegram, /newbot, copy the HTTP API token.
  2. Send /start to your new bot, then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=123456:ABC...
       TELEGRAM_CHAT_ID=-100123456789
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("betbot.telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _credentials() -> tuple[str | None, str | None]:
    return (
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
        os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
    )


def is_configured() -> bool:
    token, chat = _credentials()
    return bool(token and chat)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    reraise=True,
)
def _send_raw(text: str, parse_mode: str = "HTML") -> bool:
    token, chat_id = _credentials()
    if not (token and chat_id):
        logger.debug("Telegram not configured — skipping send")
        return False
    url = API_URL.format(token=token)
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text[:4096],   # Telegram cap
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        logger.warning("Telegram send failed (%s) : %s",
                       resp.status_code, resp.text[:200])
        return False
    return True


def notify_scan_summary(
    sport_keys_scanned: Iterable[str],
    n_picks: int,
    n_parlays: int,
    top_picks: list[dict] | None = None,
    label: str = "scan",
) -> bool:
    """Send the same summary the email contains, formatted for Telegram."""
    sports = ", ".join(sport_keys_scanned) or "—"
    text = [
        f"<b>BetBot — {label}</b>",
        f"Ligues : {sports}",
        f"Paris détectés : <b>{n_picks}</b> · Combinés : <b>{n_parlays}</b>",
    ]
    for p in (top_picks or [])[:5]:
        text.append(
            f"  • {p.get('home_team','?')} vs {p.get('away_team','?')} | "
            f"{p.get('selection_label','?')} @ {p.get('best_odds',0):.2f} "
            f"(edge {p.get('value_edge',0)*100:+.1f}%)"
        )
    return _send_raw("\n".join(text))


def notify_alert(title: str, body: str) -> bool:
    """Send a one-off alert (source down, stop-loss tripped, etc.)."""
    return _send_raw(f"<b>⚠️ {title}</b>\n{body}")


def notify_resolved(home: str, away: str, selection: str, result: str,
                    payout: float = 0.0, balance_after: float = 0.0) -> bool:
    """Push a settlement notification when a bet is resolved."""
    icon = {"win": "✅", "loss": "❌", "void": "⏸"}.get(result, "•")
    text = (
        f"{icon} <b>{home} vs {away}</b>\n"
        f"Sélection: {selection} → <b>{result.upper()}</b>\n"
        f"Payout: {payout:+.2f}$ · Solde: {balance_after:.2f}$"
    )
    return _send_raw(text)
