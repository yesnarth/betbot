"""add_elo_xg_columns — Phase 8 enrichment columns on team_stats.

Revision ID: a7c9d2e1f3b8
Revises: b49d255348fe
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c9d2e1f3b8"
down_revision: Union[str, Sequence[str], None] = "b49d255348fe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("team_stats", sa.Column("elo_rating", sa.Float(), nullable=True))
    op.add_column("team_stats", sa.Column("xg_for", sa.Float(), nullable=True))
    op.add_column("team_stats", sa.Column("xg_against", sa.Float(), nullable=True))
    op.add_column("team_stats", sa.Column("npxg_for", sa.Float(), nullable=True))
    op.add_column("team_stats", sa.Column("npxg_against", sa.Float(), nullable=True))
    op.add_column("team_stats", sa.Column("xpts_per_match", sa.Float(), nullable=True))
    op.add_column("team_stats", sa.Column("sources_updated_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("team_stats", "sources_updated_at")
    op.drop_column("team_stats", "xpts_per_match")
    op.drop_column("team_stats", "npxg_against")
    op.drop_column("team_stats", "npxg_for")
    op.drop_column("team_stats", "xg_against")
    op.drop_column("team_stats", "xg_for")
    op.drop_column("team_stats", "elo_rating")
