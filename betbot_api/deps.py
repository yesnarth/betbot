"""
Shared FastAPI dependencies for the router modules.

Keeping `limiter`, `get_db`, and any cross-cutting helpers in one place
avoids circular imports between routers and the main app.
"""
from __future__ import annotations

from fastapi import Depends
from slowapi import Limiter
from slowapi.util import get_remote_address

from betbot.config import load_settings
from betbot.db import Database

# Rate limiting (per remote IP). Tighter limits on expensive endpoints are
# applied via @limiter.limit(...) decorators in each router.
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])


def get_db() -> Database:
    """Return a Database handle bound to the configured DATABASE_URL."""
    s = load_settings()
    return Database(s.database_url)
