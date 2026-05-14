"""add idempotency_keys — replay protection for bankroll mutations.

Revision ID: i3d5f7a9c1b2
Revises: h2c4e6f8d9b1
Create Date: 2026-05-14

Clients (dashboard, mobile, scripts) can send an `Idempotency-Key` header
on deposit / withdraw. First call processes normally and caches the
response; retries with the same key return the cached response without
re-running the mutation. Different request body + same key → 409.

24h is enough for transactional retries — the table can be GC'd by a
periodic job, but stays cheap as-is (a few rows/day in normal use).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i3d5f7a9c1b2"
down_revision: Union[str, Sequence[str], None] = "h2c4e6f8d9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.Text, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_index(
        "ix_idempotency_created_at", "idempotency_keys", ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
