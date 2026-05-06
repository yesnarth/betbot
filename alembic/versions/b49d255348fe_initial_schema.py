"""initial_schema — full BetBot baseline (PostgreSQL).

Revision ID: b49d255348fe
Revises:
Create Date: 2026-05-06

Creates the full schema from scratch. PostgreSQL only — there is no
SQLite path. Future migrations will use `alembic revision --autogenerate`
on top of this baseline.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b49d255348fe"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_stats",
        sa.Column("team_name", sa.String, nullable=False),
        sa.Column("sport_key", sa.String, nullable=False),
        sa.Column("league_code", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
        sa.Column("attack_home", sa.Float, nullable=False),
        sa.Column("defense_home", sa.Float, nullable=False),
        sa.Column("attack_away", sa.Float, nullable=False),
        sa.Column("defense_away", sa.Float, nullable=False),
        sa.Column("matches_analyzed", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("team_name", "sport_key"),
    )

    op.create_table(
        "league_averages",
        sa.Column("sport_key", sa.String, primary_key=True),
        sa.Column("home_avg", sa.Float, nullable=False),
        sa.Column("away_avg", sa.Float, nullable=False),
        sa.Column("n_matches", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )

    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("event_id", sa.String, nullable=False),
        sa.Column("sport_key", sa.String, nullable=False),
        sa.Column("home_team", sa.String, nullable=False),
        sa.Column("away_team", sa.String, nullable=False),
        sa.Column("market", sa.String, nullable=False),
        sa.Column("selection", sa.String, nullable=False),
        sa.Column("model_prob", sa.Float, nullable=False),
        sa.Column("best_odds", sa.Float, nullable=False),
        sa.Column("best_book", sa.String, nullable=False),
        sa.Column("value_edge", sa.Float, nullable=False),
        sa.Column("kelly_stake", sa.Float, nullable=False),
        sa.Column("lambda_home", sa.Float, nullable=True),
        sa.Column("lambda_away", sa.Float, nullable=True),
        sa.Column("model_type", sa.String, nullable=False),
        sa.Column("result", sa.String, nullable=True),
        sa.Column("closing_odds", sa.Float, nullable=True),
        sa.Column("resolved_at", sa.String, nullable=True),
        sa.UniqueConstraint("event_id", "market", "selection", name="uq_prediction_selection"),
    )
    op.create_index("ix_predictions_created_at", "predictions", ["created_at"])
    op.create_index("ix_predictions_sport_key", "predictions", ["sport_key"])
    op.create_index("ix_predictions_result", "predictions", ["result"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("trigger", sa.String, nullable=False),
        sa.Column("filters", sa.JSON, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("picks", sa.JSON, nullable=False),
        sa.Column("n_tool_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="ok"),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_predictions_result", table_name="predictions")
    op.drop_index("ix_predictions_sport_key", table_name="predictions")
    op.drop_index("ix_predictions_created_at", table_name="predictions")
    op.drop_table("predictions")
    op.drop_table("league_averages")
    op.drop_table("team_stats")
