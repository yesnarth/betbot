"""
Alembic environment for BetBot.

Uses DATABASE_URL from the environment (or the .env file via betbot.config import side-effect)
so the same migration scripts apply to local SQLite and Docker PostgreSQL.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the betbot package importable when alembic runs from the project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Loads .env via the side-effect of importing betbot.config
import betbot.config  # noqa: F401

# Import every ORM model so Base.metadata is populated for autogenerate
from betbot.database import Base
import betbot.orm_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL is mandatory — there is no SQLite fallback.
runtime_url = os.getenv("DATABASE_URL", "").strip()
if not runtime_url:
    raise RuntimeError(
        "DATABASE_URL is required. Set it in .env or run "
        "`docker compose up -d db` first."
    )
config.set_main_option("sqlalchemy.url", runtime_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
