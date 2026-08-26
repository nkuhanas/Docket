"""Allow a durable morning-brief decision carousel.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("discord_projections") as batch:
        batch.drop_constraint("ck_discord_projections_view_mode", type_="check")
        batch.drop_constraint("ck_discord_projections_view_page", type_="check")
        batch.create_check_constraint(
            "ck_discord_projections_view_mode",
            "view_mode IN ('summary', 'schedule_review', 'decision', "
            "'schedule_failures', 'brief_review')",
        )
        batch.create_check_constraint(
            "ck_discord_projections_view_page",
            "((view_mode IN ('schedule_review', 'schedule_failures') "
            "AND view_page BETWEEN 1 AND 5) OR "
            "(view_mode = 'brief_review' AND view_page BETWEEN 1 AND 65535) OR "
            "(view_mode IN ('summary', 'decision') AND view_page IS NULL))",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE discord_projections SET view_mode = 'summary', view_page = NULL "
        "WHERE view_mode = 'brief_review'"
    )
    with op.batch_alter_table("discord_projections") as batch:
        batch.drop_constraint("ck_discord_projections_view_mode", type_="check")
        batch.drop_constraint("ck_discord_projections_view_page", type_="check")
        batch.create_check_constraint(
            "ck_discord_projections_view_mode",
            "view_mode IN ('summary', 'schedule_review', 'decision', 'schedule_failures')",
        )
        batch.create_check_constraint(
            "ck_discord_projections_view_page",
            "((view_mode IN ('schedule_review', 'schedule_failures') "
            "AND view_page BETWEEN 1 AND 5) OR "
            "(view_mode IN ('summary', 'decision') AND view_page IS NULL))",
        )
