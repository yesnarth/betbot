"""quarantine flag on predictions, and backfill the degenerate-calibration picks

59 production picks were born at model_prob >= 0.99 from a calibrator that
mapped ordinary matches to certainty. They are genuine bets the user placed,
so deleting them is out of the question — the standing rule is that predictions
are never removed. But counting them poisons the ROI, the hit rate, and the
calibrator's own training set, which is how a measurement bug quietly becomes
a modelling bug.

`excluded_reason` keeps them in the table and out of every statistic, with the
reason recorded in plain text. A named column rather than an implicit
`model_prob >= 0.99` filter: the rule is auditable, reversible, and a future
quarantine can state its own reason instead of borrowing this one's
coincidence.

Revision ID: o9d2f6h8c1e5
Revises: n8c1e5g7b9d4
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "o9d2f6h8c1e5"
down_revision = "n8c1e5g7b9d4"
branch_labels = None
depends_on = None

_REASON = "calibration_degeneree_2026_08"


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("excluded_reason", sa.String(), nullable=True),
    )
    # Backfill only the degenerate population, and only rows not already
    # quarantined. The threshold is 0.99 rather than 1.0: the bug drove the
    # winning outcome to 1.0 and its rivals to 0.0, but renormalization left a
    # sliver on some rows.
    op.execute(
        sa.text(
            "UPDATE predictions SET excluded_reason = :reason "
            "WHERE excluded_reason IS NULL AND (model_prob >= 0.99 OR model_prob <= 0.01)"
        ).bindparams(reason=_REASON)
    )


def downgrade() -> None:
    op.drop_column("predictions", "excluded_reason")
