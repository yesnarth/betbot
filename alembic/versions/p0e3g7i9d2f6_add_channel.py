"""add channel to predictions

Two channels, two promises. 'valeur' claims an edge over the price. 'favoris'
claims only that model and de-vigged market AGREE at high confidence — no edge,
long-run expectation of minus the bookmaker's margin, which is the owner's
explicit and informed trade for hit rate (his stated goal since day one).

A column rather than a convention, because the two records must never blur:
mixing a flood of favourites into the value channel's ROI would bury the one
number that decides whether the model earns more freedom.

Every existing row predates the channel and was made under the value promise,
hence the backfilling server default.

Revision ID: p0e3g7i9d2f6
Revises: o9d2f6h8c1e5
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0e3g7i9d2f6"
down_revision = "o9d2f6h8c1e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("channel", sa.String(), nullable=False,
                  server_default="valeur"),
    )
    op.create_index("ix_predictions_channel", "predictions", ["channel"])


def downgrade() -> None:
    op.drop_index("ix_predictions_channel", table_name="predictions")
    op.drop_column("predictions", "channel")
