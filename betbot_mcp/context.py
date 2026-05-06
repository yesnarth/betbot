"""
Lazy-loaded singletons used by every MCP tool.

The MCP server starts cold and may serve many tool calls; we cache the heavy
clients (Settings, Database, OddsAPIClient, FootballDataClient) so a single
process keeps a warm connection pool.
"""
from __future__ import annotations

from functools import lru_cache

from betbot.api import OddsAPIClient
from betbot.config import Settings, load_settings
from betbot.db import Database
from betbot.football_api import FootballDataClient


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def db() -> Database:
    s = settings()
    return Database(s.database_url)


@lru_cache(maxsize=1)
def odds_client() -> OddsAPIClient:
    return OddsAPIClient(settings().odds_api_key)


@lru_cache(maxsize=1)
def football_client() -> FootballDataClient:
    return FootballDataClient(settings().football_data_api_key)
