"""Remove obsolete aggregate and single-meeting Calendar proposal paths.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    obsolete_action_ids = (
        "SELECT id FROM actions WHERE action_type IN "
        "('calendar_apply_term_schedule', 'calendar_create_meeting', "
        "'calendar_update_meeting')"
    )
    obsolete_revision_ids = (
        "SELECT id FROM action_revisions "
        f"WHERE action_id IN ({obsolete_action_ids})"
    )
    obsolete_queue_ids = (
        "SELECT queue_item_id FROM actions "
        "WHERE action_type IN ('calendar_apply_term_schedule', "
        "'calendar_create_meeting', 'calendar_update_meeting') "
        "AND queue_item_id IS NOT NULL"
    )

    op.execute(
        sa.text(
            "UPDATE approvals SET status = 'expired', control_projection_id = NULL "
            f"WHERE action_revision_id IN ({obsolete_revision_ids}) AND status = 'pending'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE calendar_reminder_plans SET status = 'cancelled' "
            f"WHERE action_revision_id IN ({obsolete_revision_ids}) "
            "AND status IN ('planned', 'reconciliation_required')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE actions SET status = 'superseded' "
            "WHERE action_type IN ('calendar_apply_term_schedule', "
            "'calendar_create_meeting', 'calendar_update_meeting') "
            "AND status NOT IN ('rejected', 'expired', 'superseded', 'succeeded', "
            "'partial_failed', 'reconciliation_required')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE actions SET status = 'superseded' "
            f"WHERE queue_item_id IN ({obsolete_queue_ids}) "
            "AND action_type IN ('snooze_queue_item', 'ignore_queue_item') "
            "AND status = 'available'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE queue_items SET status = 'completed', "
            "resolved_at = CURRENT_TIMESTAMP, "
            "resolution_code = 'alpha_legacy_removed', "
            "resolution_note = 'Obsolete Calendar proposal workflow removed.', "
            "snoozed_until = NULL, snooze_local_date = NULL, version = version + 1 "
            f"WHERE id IN ({obsolete_queue_ids}) "
            "AND status IN ('pending', 'awaiting_approval', 'executing', 'failed', "
            "'reconciliation_required', 'snoozed')"
        )
    )
    op.drop_table("calendar_schedule_snapshots")


def downgrade() -> None:
    op.create_table(
        "calendar_schedule_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("command_request_id", sa.Uuid(), nullable=False),
        sa.Column("term_record_id", sa.Uuid(), nullable=False),
        sa.Column("term_record_version", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 50",
            name="ck_calendar_schedule_snapshots_item_count",
        ),
        sa.ForeignKeyConstraint(
            ["command_request_id"],
            ["command_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["term_record_id"],
            ["records.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "command_request_id",
            name="uq_calendar_schedule_snapshots_command_request",
        ),
    )
