"""add head_to_head table — per-pair historical record.

Revision ID: k5f8a1c4e7d2
Revises: j4e7g9b1d3a5
Create Date: 2026-05-14

Stores the recent head-to-head history of every team pair within a
competition (alphabetical team_a / team_b storage so each pair lives in
one row). Populated by the weekly update_team_stats job that already
fetches all match results — H2H is a cheap byproduct.

Used by blended_match_probs to apply a small Bayesian adjustment on H2H
probabilities : when a team has historically dominated their direct
matchups, the prediction is nudged in that direction. Weight kept small
(default 0.10) because H2H is noisy on small samples.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k5f8a1c4e7d2"
down_revision: Union[str, Sequence[str], None] = "j4e7g9b1d3a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "head_to_head",
        sa.Column("sport_key", sa.String(length=64), primary_key=True),
        # Alphabetical ordering — (team_a, team_b) such that team_a < team_b.
        # Avoids storing the same pair under both orientations.
        sa.Column("team_a", sa.String(length=128), primary_key=True),
        sa.Column("team_b", sa.String(length=128), primary_key=True),
        sa.Column("n_matches", sa.Integer, nullable=False, server_default="0"),
        sa.Column("team_a_wins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("draws", sa.Integer, nullable=False, server_default="0"),
        sa.Column("team_b_wins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("team_a_goals_avg", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("team_b_goals_avg", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_index(
        "ix_head_to_head_sport_key", "head_to_head", ["sport_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_head_to_head_sport_key", table_name="head_to_head")
    op.drop_table("head_to_head")
