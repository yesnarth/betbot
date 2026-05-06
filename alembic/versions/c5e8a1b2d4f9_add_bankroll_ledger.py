"""add_bankroll_ledger — capital movement journal.

Revision ID: c5e8a1b2d4f9
Revises: a7c9d2e1f3b8
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5e8a1b2d4f9"
down_revision: Union[str, Sequence[str], None] = "a7c9d2e1f3b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bankroll_ledger",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("balance_after", sa.Float, nullable=False),
        sa.Column("prediction_id", sa.Integer,
                  sa.ForeignKey("predictions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.String, nullable=True),
    )
    op.create_index("ix_bankroll_ts", "bankroll_ledger", ["ts"])
    op.create_index("ix_bankroll_kind", "bankroll_ledger", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_bankroll_kind", table_name="bankroll_ledger")
    op.drop_index("ix_bankroll_ts", table_name="bankroll_ledger")
    op.drop_table("bankroll_ledger")
