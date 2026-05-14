"""drop actually_placed — superseded by placement_status lifecycle.

Revision ID: h2c4e6f8d9b1
Revises: g1a3b7d4e8f2
Create Date: 2026-05-14

The `actually_placed` boolean was a redundant mirror of
`placement_status == 'confirmed'`. Now that the advisor lifecycle (proposed
→ confirmed/skipped) is the single source of truth, the column is dropped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h2c4e6f8d9b1"
down_revision: Union[str, Sequence[str], None] = "g1a3b7d4e8f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("predictions", "actually_placed")


def downgrade() -> None:
    # Restore as nullable=False with server default false, then backfill
    # from placement_status so old code paths reading it still get the
    # right boolean.
    op.add_column(
        "predictions",
        sa.Column("actually_placed", sa.Boolean,
                  nullable=False, server_default=sa.false()),
    )
    op.execute(
        "UPDATE predictions SET actually_placed = TRUE "
        "WHERE placement_status = 'confirmed'"
    )
