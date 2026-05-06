"""add_promotions_and_cashouts — track bookmaker promos and cash-outs.

Revision ID: f9d8c7a3b2e6
Revises: e2a5b8c3f4d1
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9d8c7a3b2e6"
down_revision: Union[str, Sequence[str], None] = "e2a5b8c3f4d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("received_at", sa.String, nullable=False),
        sa.Column("bookmaker_key", sa.String,
                  sa.ForeignKey("bookmakers.key", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("nominal_value", sa.Float, nullable=False),
        sa.Column("cash_equivalent", sa.Float, nullable=False),
        sa.Column("rollover_x", sa.Float, nullable=True),
        sa.Column("expires_at", sa.String, nullable=True),
        sa.Column("used", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("used_at", sa.String, nullable=True),
        sa.Column("note", sa.String, nullable=True),
    )
    op.create_index("ix_promotions_received_at", "promotions", ["received_at"])

    op.create_table(
        "cash_outs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("prediction_id", sa.Integer,
                  sa.ForeignKey("predictions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("cash_out_amount", sa.Float, nullable=False),
        sa.Column("bookmaker_offered_price", sa.Float, nullable=True),
        sa.Column("note", sa.String, nullable=True),
    )
    op.create_index("ix_cash_outs_created_at", "cash_outs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_cash_outs_created_at", table_name="cash_outs")
    op.drop_table("cash_outs")
    op.drop_index("ix_promotions_received_at", table_name="promotions")
    op.drop_table("promotions")
