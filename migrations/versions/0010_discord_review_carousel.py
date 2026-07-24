"""Add durable Discord aggregate-review presentation state.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("discord_projections") as batch:
        batch.add_column(sa.Column("view_action_revision_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column(
                "view_mode",
                sa.String(length=32),
                nullable=False,
                server_default="summary",
            )
        )
        batch.add_column(sa.Column("view_page", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "reviewed_through_page",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_foreign_key(
            "fk_discord_projections_view_action_revision",
            "action_revisions",
            ["view_action_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_discord_projections_view_mode",
            "view_mode IN ('summary', 'schedule_review', 'decision', "
            "'schedule_failures')",
        )
        batch.create_check_constraint(
            "ck_discord_projections_view_page",
            "((view_mode IN ('schedule_review', 'schedule_failures') "
            "AND view_page BETWEEN 1 AND 5) OR "
            "(view_mode IN ('summary', 'decision') AND view_page IS NULL))",
        )
        batch.create_check_constraint(
            "ck_discord_projections_reviewed_through_page",
            "reviewed_through_page BETWEEN 0 AND 5",
        )


def downgrade() -> None:
    with op.batch_alter_table("discord_projections") as batch:
        batch.drop_constraint(
            "ck_discord_projections_reviewed_through_page",
            type_="check",
        )
        batch.drop_constraint("ck_discord_projections_view_page", type_="check")
        batch.drop_constraint("ck_discord_projections_view_mode", type_="check")
        batch.drop_constraint(
            "fk_discord_projections_view_action_revision",
            type_="foreignkey",
        )
        batch.drop_column("reviewed_through_page")
        batch.drop_column("view_page")
        batch.drop_column("view_mode")
        batch.drop_column("view_action_revision_id")
