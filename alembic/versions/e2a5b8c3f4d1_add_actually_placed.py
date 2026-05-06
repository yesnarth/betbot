"""add_actually_placed — distinguish recommended bets from bets actually played.

Revision ID: e2a5b8c3f4d1
Revises: d1f4a8e7b3c0
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2a5b8c3f4d1"
down_revision: Union[str, Sequence[str], None] = "d1f4a8e7b3c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("predictions",
                  sa.Column("actually_placed", sa.Boolean,
                            nullable=False, server_default=sa.false()))
    op.add_column("predictions",
                  sa.Column("placed_at", sa.String, nullable=True))
    op.add_column("predictions",
                  sa.Column("placed_bookmaker", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("predictions", "placed_bookmaker")
    op.drop_column("predictions", "placed_at")
    op.drop_column("predictions", "actually_placed")
