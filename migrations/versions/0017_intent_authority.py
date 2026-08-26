"""Add formulation authority and explicit queue presentation.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("queue_items") as batch:
        batch.add_column(
            sa.Column(
                "presentation",
                sa.String(length=32),
                nullable=False,
                server_default="proposal",
            )
        )
        batch.create_check_constraint(
            "ck_queue_items_presentation",
            "presentation IN ('proposal', 'conflict_resolution', 'clarification', "
            "'action_required', 'awareness', 'terminal_outcome', 'system_alert', "
            "'suppressed')",
        )
    op.execute(
        sa.text(
            "UPDATE queue_items SET presentation = 'awareness' "
            "WHERE resolution_code = 'gmail_notification'"
        )
    )

    with op.batch_alter_table("action_revisions") as batch:
        batch.add_column(
            sa.Column(
                "authority",
                sa.String(length=32),
                nullable=False,
                server_default="inferred",
            )
        )
        batch.create_check_constraint(
            "ck_action_revisions_authority",
            "authority IN ('explicit_user', 'canonical', 'inferred')",
        )
    op.execute(
        sa.text(
            "UPDATE action_revisions SET authority = 'explicit_user' "
            "WHERE action_type LIKE 'calendar_%'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("action_revisions") as batch:
        batch.drop_constraint("ck_action_revisions_authority", type_="check")
        batch.drop_column("authority")
    with op.batch_alter_table("queue_items") as batch:
        batch.drop_constraint("ck_queue_items_presentation", type_="check")
        batch.drop_column("presentation")
