"""add commence_time to predictions — kickoff timestamp

Two things were impossible without it:

1. **CLV was economically unusable.** `snapshot_closing_odds` selects every
   unresolved confirmed pick, groups by sport_key and calls the Odds API once
   per league — BEFORE any kickoff-proximity filter, because the row had no
   kickoff to filter on. Measured on production: 25 leagues × 2 regions ×
   2 markets = 100 credits per cycle, every 10 minutes, against a monthly
   quota of 500. Turning CLV on would have burned the month in two cycles.
   With this column the snapshot can narrow to the handful of matches actually
   inside the pre-kickoff window.

2. **The user could not tell which bets were still placeable.** Placing a bet
   needs match + selection + odds + bookmaker + WHEN. The first four were
   shown, the fifth existed nowhere, so the dashboard fell back to guessing
   from `created_at`.

`PredictionRow` already declared the field (with the comment "used by UI
countdown") but it was structurally None — no column backed it.

Stored as an ISO-8601 string for consistency with `created_at`, `resolved_at`
and every other timestamp on this table.

Revision ID: m7b0d4f6a8c3
Revises: l6a9c3e5b8d2
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "m7b0d4f6a8c3"
down_revision = "l6a9c3e5b8d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("commence_time", sa.String(), nullable=True),
    )
    # Partial index: the CLV snapshot and the "still placeable" view both scan
    # unresolved rows ordered by kickoff. Existing rows keep NULL — their
    # kickoff is unknowable after the fact.
    op.create_index(
        "ix_predictions_commence_time",
        "predictions",
        ["commence_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_predictions_commence_time", table_name="predictions")
    op.drop_column("predictions", "commence_time")
