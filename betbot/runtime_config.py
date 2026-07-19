"""Runtime-mutable configuration stored in the data volume.

Lets the user rotate the Odds API key from the dashboard WITHOUT a redeploy or a
container restart: the key is written to data/runtime_config.json, and
OddsAPIClient reads the override before every request (falling back to the
ODDS_API_KEY env var). The file lives in the shared betbot_data volume, so the
worker and the API both see an update instantly.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("betbot.runtime_config")

RUNTIME_CONFIG_PATH = Path(os.getenv("RUNTIME_CONFIG_PATH", "data/runtime_config.json"))


def _read() -> dict:
    try:
        return json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_odds_api_key_override() -> str | None:
    """The dashboard-set Odds key, or None when unset. Takes precedence over env."""
    k = (_read().get("odds_api_key") or "").strip()
    return k or None


def get_odds_api_key() -> str:
    """Effective Odds key: runtime override → ODDS_API_KEY env var."""
    return get_odds_api_key_override() or os.getenv("ODDS_API_KEY", "").strip()


def set_odds_api_key(key: str) -> None:
    """Persist (or clear, if empty) the Odds key override to the data volume."""
    key = (key or "").strip()
    cfg = _read()
    if key:
        cfg["odds_api_key"] = key
    else:
        cfg.pop("odds_api_key", None)
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    logger.info("Odds API key override %s", "défini" if key else "effacé")


def _mask(k: str) -> str:
    k = (k or "").strip()
    if not k:
        return "(aucune)"
    return f"…{k[-4:]}" if len(k) >= 4 else "****"


def odds_key_status() -> dict:
    """Non-secret status for the dashboard: masked key + where it comes from."""
    override = get_odds_api_key_override()
    env = os.getenv("ODDS_API_KEY", "").strip()
    effective = override or env
    return {
        "configured": bool(effective),
        "source": "dashboard" if override else ("env" if env else "none"),
        "masked": _mask(effective),
    }
