"""
PostgreSQL engine + Session factory.

This is a PostgreSQL-only application. DATABASE_URL is mandatory at startup
and must point to a Postgres instance. Use `docker compose up -d db` for a
zero-install local Postgres on port 5432.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger("betbot.database")


class DatabaseConfigurationError(RuntimeError):
    """Raised when DATABASE_URL is missing or points to an unsupported dialect."""


def _resolve_url(url: str | None) -> str:
    candidate = (url or os.getenv("DATABASE_URL", "")).strip()
    if not candidate:
        raise DatabaseConfigurationError(
            "DATABASE_URL is required. Set it in .env (see .env.example) or run "
            "`docker compose up -d db` and use "
            "postgresql+psycopg2://betbot:betbot_dev_pwd@localhost:5432/betbot"
        )
    if not candidate.startswith(("postgresql://", "postgresql+")):
        raise DatabaseConfigurationError(
            f"Unsupported DATABASE_URL dialect: {candidate.split('://', 1)[0]}. "
            "BetBot is PostgreSQL-only."
        )
    return candidate


def make_engine(url: str | None = None) -> Engine:
    """Build the SQLAlchemy engine. Raises if DATABASE_URL is missing or wrong dialect."""
    url = _resolve_url(url)
    engine = create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,           # heal stale connections after idle / restart
        pool_recycle=1800,            # rotate connections every 30 min (long-running worker)
        future=True,
    )
    logger.debug("Engine SQL initialisé : %s", engine.url.render_as_string(hide_password=True))
    return engine


class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine(url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine — used by tests that swap DATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a Session that commits on success and rolls back on error."""
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
