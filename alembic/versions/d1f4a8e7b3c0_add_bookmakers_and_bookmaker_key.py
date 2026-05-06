"""add_bookmakers_and_bookmaker_key — multi-account bankroll partitioning.

Revision ID: d1f4a8e7b3c0
Revises: c5e8a1b2d4f9
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1f4a8e7b3c0"
down_revision: Union[str, Sequence[str], None] = "c5e8a1b2d4f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bookmakers",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("display_name", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String, nullable=True),
    )

    op.add_column("bankroll_ledger",
                  sa.Column("bookmaker_key", sa.String, nullable=True))
    op.create_foreign_key(
        "fk_bankroll_bookmaker", "bankroll_ledger", "bookmakers",
        ["bookmaker_key"], ["key"], ondelete="SET NULL",
    )
    op.create_index("ix_bankroll_bookmaker", "bankroll_ledger", ["bookmaker_key"])

    # Seed a default account so legacy rows have a destination
    op.execute(
        "INSERT INTO bookmakers (key, display_name, created_at, active) "
        "VALUES ('default', 'Default account', NOW()::text, TRUE)"
    )


def downgrade() -> None:
    op.drop_index("ix_bankroll_bookmaker", table_name="bankroll_ledger")
    op.drop_constraint("fk_bankroll_bookmaker", "bankroll_ledger", type_="foreignkey")
    op.drop_column("bankroll_ledger", "bookmaker_key")
    op.drop_table("bookmakers")
