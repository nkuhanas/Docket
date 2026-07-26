"""Remove disabled pre-unified reminder compatibility state.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    legacy_rule_ids = (
        "SELECT id FROM reminder_rules WHERE source_kind = 'legacy_explicit'"
    )
    op.execute(
        sa.text(
            "DELETE FROM scheduled_notifications "
            f"WHERE reminder_rule_id IN ({legacy_rule_ids})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE calendar_reminder_plans SET reminder_rule_id = NULL "
            f"WHERE reminder_rule_id IN ({legacy_rule_ids})"
        )
    )
    op.execute(
        sa.text("DELETE FROM reminder_rules WHERE source_kind = 'legacy_explicit'")
    )
    with op.batch_alter_table("reminder_rules") as batch:
        batch.drop_constraint("ck_reminder_rules_source_kind", type_="check")
        batch.drop_column("source_kind")


def downgrade() -> None:
    with op.batch_alter_table("reminder_rules") as batch:
        batch.add_column(
            sa.Column(
                "source_kind",
                sa.String(length=32),
                nullable=False,
                server_default="canonical_plan",
            )
        )
        batch.create_check_constraint(
            "ck_reminder_rules_source_kind",
            "source_kind IN ('legacy_explicit', 'canonical_plan')",
        )
