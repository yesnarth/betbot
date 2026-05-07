"""add placement_status — separates 'proposed by bot' from 'confirmed by user'

Revision ID: g1a3b7d4e8f2
Revises: f9d8c7a3b2e6
Create Date: 2026-05-07

The bot now operates as an ADVISOR : it proposes picks, the user reviews
them on the dashboard, places the bet manually at their bookmaker (which
has no API), then confirms. Bankroll is only debited upon confirmation.

States:
  - proposed  : bot suggested it, waiting for user decision (bankroll NOT debited)
  - confirmed : user confirmed they placed the bet at the bookmaker (bankroll debited)
  - skipped   : user explicitly skipped, OR auto-expired past kickoff (no debit)

Existing rows are marked 'confirmed' on upgrade because the legacy code
debited bankroll at save_prediction time — those debits are real.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g1a3b7d4e8f2"
down_revision: Union[str, Sequence[str], None] = "f9d8c7a3b2e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("placement_status", sa.String(length=16),
                  nullable=False, server_default="proposed"),
    )
    op.add_column(
        "predictions",
        sa.Column("placement_status_at", sa.String, nullable=True),
    )

    # Existing rows already had their bankroll debited under the legacy
    # save_prediction behaviour — we preserve that by marking them 'confirmed'.
    # New rows from the worker post-upgrade default to 'proposed'.
    op.execute(
        "UPDATE predictions SET placement_status = 'confirmed', "
        "placement_status_at = COALESCE(placed_at, created_at) "
        "WHERE placement_status = 'proposed'"
    )

    op.create_index("ix_predictions_placement_status",
                    "predictions", ["placement_status"])


def downgrade() -> None:
    op.drop_index("ix_predictions_placement_status", table_name="predictions")
    op.drop_column("predictions", "placement_status_at")
    op.drop_column("predictions", "placement_status")
