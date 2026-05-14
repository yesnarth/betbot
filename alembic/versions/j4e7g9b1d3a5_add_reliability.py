"""add reliability score to predictions.

Revision ID: j4e7g9b1d3a5
Revises: i3d5f7a9c1b2
Create Date: 2026-05-14

Per-pick reliability score in [0, 1]. Computed by
betbot.reliability.compute_reliability from model_prob / value_edge /
model_type / n_matches at the moment the prediction is detected.
Nullable so legacy rows keep loading; the dashboard renders the column
only when populated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j4e7g9b1d3a5"
down_revision: Union[str, Sequence[str], None] = "i3d5f7a9c1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("reliability", sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("predictions", "reliability")
