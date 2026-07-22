"""Milestone 2 Calendar action and execution state.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[sa.DateTime], sa.Column[sa.DateTime]]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def upgrade() -> None:
    op.create_table(
        "queue_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("primary_source_item_id", sa.Uuid()),
        sa.Column("deduplication_key", sa.String(length=512), nullable=False),
        sa.Column("material_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.String(length=2000), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("snoozed_until", sa.DateTime(timezone=True)),
        sa.Column("snooze_local_date", sa.Date()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_code", sa.String(length=128)),
        sa.Column("resolution_note", sa.String(length=1000)),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'awaiting_approval', 'executing', 'completed', "
            "'failed', 'reconciliation_required', 'snoozed', 'ignored')",
            name="ck_queue_items_status",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_queue_items_priority",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("queue_item_id", sa.Uuid()),
        sa.Column("record_id", sa.Uuid()),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "queue_item_id IS NOT NULL OR record_id IS NOT NULL",
            name="ck_actions_has_entity",
        ),
        sa.CheckConstraint(
            "status IN ('available', 'approval_pending', 'ready', 'executing', "
            "'succeeded', 'rejected', 'expired', 'superseded', 'failed', "
            "'reconciliation_required')",
            name="ck_actions_status",
        ),
        sa.ForeignKeyConstraint(["queue_item_id"], ["queue_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["record_id"], ["records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "action_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.Uuid()),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("preview_sha256", sa.String(length=64), nullable=False),
        sa.Column("risk_class", sa.String(length=32), nullable=False),
        sa.Column("target_versions", sa.JSON(), nullable=False),
        sa.Column("created_by_actor_type", sa.String(length=32), nullable=False),
        sa.Column("created_by_actor_id", sa.String(length=255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_id", "revision", name="uq_action_revisions_action_revision"
        ),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_revision_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("interaction_token_version", sa.Integer(), nullable=False),
        sa.Column("short_code_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorized_user_id", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column("response_user_id", sa.String(length=64)),
        sa.Column("response_guild_id", sa.String(length=64)),
        sa.Column("response_channel_id", sa.String(length=64)),
        sa.Column("response_message_id", sa.String(length=64)),
        sa.Column("discord_interaction_id", sa.String(length=255)),
        sa.Column("response_note", sa.String(length=1000)),
        sa.Column("consumed_operation_id", sa.Uuid()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'consumed', 'rejected', 'expired', "
            "'superseded')",
            name="ck_approvals_status",
        ),
        sa.ForeignKeyConstraint(
            ["action_revision_id"], ["action_revisions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discord_interaction_id"),
        sa.UniqueConstraint("short_code_sha256"),
    )
    op.create_index(
        "uq_approvals_pending_revision",
        "approvals",
        ["action_revision_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_revision_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid()),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("operation_type", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("provider_correlation", sa.String(length=255), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("last_error_message", sa.String(length=1000)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'reconciliation_required')",
            name="ck_operations_status",
        ),
        sa.ForeignKeyConstraint(
            ["action_revision_id"], ["action_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("provider_correlation"),
    )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.create_foreign_key(
            "fk_approvals_consumed_operation",
            "operations",
            ["consumed_operation_id"],
            ["id"],
        )
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("request_summary", sa.JSON(), nullable=False),
        sa.Column("response_summary", sa.JSON()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255)),
        sa.Column("error_code", sa.String(length=128)),
        sa.Column("error_message", sa.String(length=1000)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('execute', 'reconcile')", name="ck_attempts_kind"),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'unknown')",
            name="ck_attempts_status",
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "attempt_number", name="uq_attempts_operation_number"
        ),
    )
    op.create_table(
        "calendar_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_id", sa.String(length=1024), nullable=False),
        sa.Column("external_event_id", sa.String(length=1024), nullable=False),
        sa.Column("provider_etag", sa.String(length=1024)),
        sa.Column("provider_correlation", sa.String(length=255), nullable=False),
        sa.Column("last_synced_version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["record_id"], ["records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_correlation"),
        sa.UniqueConstraint(
            "account_id",
            "calendar_id",
            "external_event_id",
            name="uq_calendar_links_external_event",
        ),
        sa.UniqueConstraint(
            "record_id",
            "meeting_id",
            "account_id",
            "calendar_id",
            name="uq_calendar_links_target",
        ),
    )


def downgrade() -> None:
    op.drop_table("calendar_links")
    op.drop_table("execution_attempts")
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint("fk_approvals_consumed_operation", type_="foreignkey")
    op.drop_table("operations")
    op.drop_index("uq_approvals_pending_revision", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("action_revisions")
    op.drop_table("actions")
    op.drop_table("queue_items")
