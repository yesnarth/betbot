"""
Shared fixtures for FastAPI integration tests.

These tests hit the real `app` object via Starlette's TestClient but:
  - Override `get_db()` with a Mock so we don't touch Postgres
  - Override `require_auth` with a no-op so we don't deal with HTTP Basic
  - Skip endpoints that genuinely require external services (Odds API,
    football-data.org, Anthropic) — covered by manual smoke tests instead

The goal isn't to assert business logic (the unit suite handles that) but
to catch routing, schema, and dependency-injection regressions. Each
refactor of `betbot_api/` should keep this suite green.
"""
from __future__ import annotations

import os
from typing import Iterator
from unittest.mock import MagicMock

import pytest


# Force a non-Postgres DATABASE_URL into the environment BEFORE betbot.config
# loads, so the app boots without trying to talk to a real DB. We override
# get_db() below anyway, but load_settings() validates this upfront.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost:5432/betbot_test_unit")
os.environ.setdefault("ODDS_API_KEY", "test-key")
os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test-pwd")


@pytest.fixture
def mock_db() -> MagicMock:
    """Database mock pre-stubbed with safe defaults for the most common reads."""
    db = MagicMock()
    db.get_all_team_stats_for_league.return_value = []
    db.get_proposed_predictions.return_value = []
    db.get_skipped_predictions.return_value = []
    db.get_confirmed_pending.return_value = []
    db.get_roi_stats.return_value = {
        "n_bets": 0, "n_wins": 0, "hit_rate": 0.0, "roi": 0.0, "avg_edge": 0.0,
        "n_with_clv": 0, "avg_clv_pct": 0.0, "positive_clv_share": 0.0,
    }
    db.list_agent_runs.return_value = []
    db.get_agent_run.return_value = None
    db.save_prediction.return_value = True
    db.confirm_prediction_placed.return_value = True
    db.skip_prediction.return_value = True
    db.unskip_prediction.return_value = True
    return db


@pytest.fixture
def client(mock_db) -> Iterator:
    """FastAPI TestClient with auth + DB dependencies overridden."""
    from starlette.testclient import TestClient

    from betbot_api.main import app
    from betbot_api.auth import require_auth
    from betbot_api.deps import get_db

    app.dependency_overrides[require_auth] = lambda: "test-user"
    app.dependency_overrides[get_db] = lambda: mock_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
