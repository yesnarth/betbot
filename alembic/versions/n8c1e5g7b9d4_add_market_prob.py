"""add market_prob to predictions

The de-vigged consensus probability of the selected outcome, as it stood when
the pick was made. It was already computed on every pick — it is the
adverse-selection gate — and then discarded.

Keeping it is what makes the decisive question answerable: on the same graded
picks, Brier(model_prob) against Brier(market_prob) says whether the model adds
anything over the price. Measured on the old data the model lost that contest
(0.2356 vs 0.2242), but that measurement predates the fixes of 2026-08-07..10
(team-name matching, Poisson coverage 24 -> 45 leagues, xG 11.5% -> 67.8%), so
it has to be re-run on picks produced by the repaired pipeline.

It cannot be reconstructed afterwards: de-vigging needs the complete outcome
group and only the selected side's price survives the scan. Hence a column,
recorded from the first pick onward.

Revision ID: n8c1e5g7b9d4
Revises: m7b0d4f6a8c3
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "n8c1e5g7b9d4"
down_revision = "m7b0d4f6a8c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable on purpose: legacy rows have no market reference and never will,
    # and thin markets (no book pricing the whole outcome group) legitimately
    # leave it NULL. Any analysis must filter on IS NOT NULL rather than assume
    # a default, which would silently score the model against a fabricated
    # reference.
    op.add_column(
        "predictions",
        sa.Column("market_prob", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("predictions", "market_prob")
