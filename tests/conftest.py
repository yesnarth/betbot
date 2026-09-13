"""
Test isolation from the developer's `.env`.

`betbot/config.py` calls `load_dotenv()` at import time, so every variable in
the local `.env` leaks into the whole test suite. That is fine for API keys
(tests don't call the network) but NOT for behaviour switches: setting
`BOOKMAKER_WHITELIST=betclic,bet365` in `.env` made 4 previously-green tests
fail, because their synthetic fixtures quote Pinnacle and friends.

A test must describe its own world. These fixtures clear the behaviour
switches by default, so each suite gets the documented default; tests that
exercise a switch set it explicitly with `monkeypatch.setenv`.
"""
from __future__ import annotations

import pytest

# Behaviour switches whose value must never be inherited from the developer's
# local .env. Keep this list in sync when adding a new env-driven behaviour.
_BEHAVIOUR_SWITCHES = (
    "BOOKMAKER_WHITELIST",   # betbot/bookmaker_filter.py — restricts the odds universe
    "AUTO_CONFIRM_PICKS",    # betbot/db.py — placement_status a pick is born with
    "ODDS_REGIONS",          # betbot/api.py — which regions the Odds API returns
    "ODDS_API_KEYS",         # betbot/api.py — key-rotation pool
)


@pytest.fixture(autouse=True)
def _neutralize_env_behaviour_switches(monkeypatch):
    """Every test starts from the documented defaults, not from `.env`."""
    for name in _BEHAVIOUR_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    yield
