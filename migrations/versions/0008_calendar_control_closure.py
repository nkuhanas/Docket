"""Add Calendar control closure state for Milestone 3.6.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
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


def _backfill_calendar_classification() -> None:
    connection = op.get_bind()
    links = sa.table(
        "calendar_links",
        sa.column("id", sa.Uuid()),
        sa.column("record_id", sa.Uuid()),
        sa.column("meeting_id", sa.String()),
        sa.column("logical_key", sa.String()),
        sa.column("system_tags", sa.JSON()),
    )
    for row in connection.execute(
        sa.select(links.c.id, links.c.record_id, links.c.meeting_id)
    ).mappings():
        connection.execute(
            links.update()
            .where(links.c.id == row["id"])
            .values(
                logical_key=f"course:{row['record_id']}:{row['meeting_id']}",
                system_tags=["recurring", "timed", "course_meeting"],
            )
        )

    cache = sa.table(
        "calendar_event_cache",
        sa.column("id", sa.Uuid()),
        sa.column("provider_event_id", sa.String()),
        sa.column("recurring_event_id", sa.String()),
        sa.column("is_all_day", sa.Boolean()),
        sa.column("recurrence_kind", sa.String()),
        sa.column("system_tags", sa.JSON()),
    )
    linked_event_ids = set(
        connection.execute(
            sa.select(sa.column("external_event_id")).select_from(sa.table("calendar_links"))
        ).scalars()
    )
    for row in connection.execute(
        sa.select(
            cache.c.id,
            cache.c.provider_event_id,
            cache.c.recurring_event_id,
            cache.c.is_all_day,
        )
    ).mappings():
        recurring = (
            row["recurring_event_id"] is not None
            or row["provider_event_id"] in linked_event_ids
        )
        recurrence_kind = "recurring" if recurring else "one_time"
        timing_kind = "all_day" if row["is_all_day"] else "timed"
        connection.execute(
            cache.update()
            .where(cache.c.id == row["id"])
            .values(
                recurrence_kind=recurrence_kind,
                system_tags=[recurrence_kind, timing_kind],
            )
        )


def upgrade() -> None:
    with op.batch_alter_table("calendar_links") as batch:
        batch.add_column(
            sa.Column(
                "origin_kind",
                sa.String(length=32),
                nullable=False,
                server_default="course_meeting",
            )
        )
        batch.add_column(sa.Column("logical_key", sa.String(length=512), nullable=True))
        batch.add_column(
            sa.Column(
                "recurrence_kind",
                sa.String(length=16),
                nullable=False,
                server_default="recurring",
            )
        )
        batch.add_column(
            sa.Column("system_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("operator_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal")
        )
        batch.add_column(
            sa.Column(
                "priority_basis",
                sa.String(length=32),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(sa.Column("reminder_plan_sha256", sa.String(length=64)))

    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.add_column(
            sa.Column(
                "has_attendees", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("organizer_is_self", sa.Boolean()))
        batch.add_column(
            sa.Column(
                "recurrence_kind",
                sa.String(length=16),
                nullable=False,
                server_default="one_time",
            )
        )
        batch.add_column(
            sa.Column("system_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("operator_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal")
        )
        batch.add_column(
            sa.Column(
                "priority_basis",
                sa.String(length=32),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(
            sa.Column(
                "provider_reminders",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    with op.batch_alter_table("reminder_rules") as batch:
        batch.add_column(
            sa.Column(
                "source_kind",
                sa.String(length=32),
                nullable=False,
                server_default="legacy_explicit",
            )
        )
        batch.create_check_constraint(
            "ck_reminder_rules_source_kind",
            "source_kind IN ('legacy_explicit', 'canonical_plan')",
        )

    _backfill_calendar_classification()

    with op.batch_alter_table("calendar_links") as batch:
        batch.drop_constraint("uq_calendar_links_target", type_="unique")
        batch.alter_column("record_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("meeting_id", existing_type=sa.String(length=128), nullable=True)
        batch.alter_column(
            "logical_key", existing_type=sa.String(length=512), nullable=False
        )
        batch.create_check_constraint(
            "ck_calendar_links_origin_kind",
            "origin_kind IN ('course_meeting', 'standalone', 'adopted_provider_event')",
        )
        batch.create_check_constraint(
            "ck_calendar_links_recurrence_kind",
            "recurrence_kind IN ('one_time', 'recurring')",
        )
        batch.create_check_constraint(
            "ck_calendar_links_priority",
            "priority IN ('low', 'normal', 'high', 'urgent')",
        )
        batch.create_check_constraint(
            "ck_calendar_links_priority_basis",
            "priority_basis IN ('default', 'explicit_operator')",
        )
        batch.create_check_constraint(
            "ck_calendar_links_origin_target",
            "(origin_kind = 'course_meeting' AND record_id IS NOT NULL "
            "AND meeting_id IS NOT NULL) OR "
            "(origin_kind IN ('standalone', 'adopted_provider_event') "
            "AND meeting_id IS NULL)",
        )
        batch.create_unique_constraint(
            "uq_calendar_links_logical_target",
            ["account_id", "calendar_id", "logical_key"],
        )

    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.create_check_constraint(
            "ck_calendar_event_cache_recurrence_kind",
            "recurrence_kind IN ('one_time', 'recurring')",
        )
        batch.create_check_constraint(
            "ck_calendar_event_cache_priority",
            "priority IN ('low', 'normal', 'high', 'urgent')",
        )
        batch.create_check_constraint(
            "ck_calendar_event_cache_priority_basis",
            "priority_basis IN ('default', 'explicit_operator')",
        )

    op.create_table(
        "calendar_schedule_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("command_request_id", sa.Uuid(), nullable=False),
        sa.Column("term_record_id", sa.Uuid(), nullable=False),
        sa.Column("term_record_version", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 50",
            name="ck_calendar_schedule_snapshots_item_count",
        ),
        sa.ForeignKeyConstraint(
            ["command_request_id"], ["command_requests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["term_record_id"], ["records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "command_request_id",
            name="uq_calendar_schedule_snapshots_command_request",
        ),
    )
    op.create_table(
        "operation_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("item_key", sa.String(length=512), nullable=False),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.Column("last_error_code", sa.String(length=128)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'reconciliation_required')",
            name="ck_operation_items_status",
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "operation_id", "item_key", name="uq_operation_items_operation_key"
        ),
    )
    op.create_table(
        "calendar_reminder_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_revision_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_item_key", sa.String(length=512)),
        sa.Column("lead_seconds", sa.Integer(), nullable=False),
        sa.Column("delivery_channels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reminder_rule_id", sa.Uuid()),
        sa.Column("provider_applied_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "lead_seconds BETWEEN 0 AND 2419200 AND lead_seconds % 60 = 0",
            name="ck_calendar_reminder_plans_lead_seconds",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'activated', 'cancelled', 'reconciliation_required')",
            name="ck_calendar_reminder_plans_status",
        ),
        sa.ForeignKeyConstraint(
            ["action_revision_id"], ["action_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reminder_rule_id"], ["reminder_rules.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_revision_id",
            "manifest_item_key",
            "lead_seconds",
            name="uq_calendar_reminder_plans_revision_item_lead",
        ),
    )
    op.create_table(
        "calendar_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operator_user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "proposal_mode",
            sa.String(length=32),
            nullable=False,
            server_default="suggest",
        ),
        sa.Column(
            "default_reminder_lead_seconds",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[600]'"),
        ),
        sa.Column(
            "default_reminder_delivery_channels",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[\"google_popup\", \"docket_queue\"]'"),
        ),
        sa.Column(
            "conflict_policy",
            sa.String(length=16),
            nullable=False,
            server_default="warn",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "proposal_mode IN ('explicit_only', 'suggest', 'off')",
            name="ck_calendar_profiles_proposal_mode",
        ),
        sa.CheckConstraint(
            "conflict_policy IN ('warn', 'block')",
            name="ck_calendar_profiles_conflict_policy",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operator_user_id"),
    )

    with op.batch_alter_table("operations") as batch:
        batch.drop_constraint("ck_operations_status", type_="check")
        batch.create_check_constraint(
            "ck_operations_status",
            "status IN ('pending', 'running', 'succeeded', 'partial_failed', "
            "'failed', 'reconciliation_required')",
        )

    with op.batch_alter_table("execution_attempts") as batch:
        batch.drop_constraint("uq_attempts_operation_number", type_="unique")
        batch.add_column(sa.Column("operation_item_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_execution_attempts_operation_item",
            "operation_items",
            ["operation_item_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "uq_attempts_parent_number",
        "execution_attempts",
        ["operation_id", "attempt_number"],
        unique=True,
        postgresql_where=sa.text("operation_item_id IS NULL"),
        sqlite_where=sa.text("operation_item_id IS NULL"),
    )
    op.create_index(
        "uq_attempts_item_number",
        "execution_attempts",
        ["operation_item_id", "attempt_number"],
        unique=True,
        postgresql_where=sa.text("operation_item_id IS NOT NULL"),
        sqlite_where=sa.text("operation_item_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_attempts_item_number", table_name="execution_attempts")
    op.drop_index("uq_attempts_parent_number", table_name="execution_attempts")
    with op.batch_alter_table("execution_attempts") as batch:
        batch.drop_constraint("fk_execution_attempts_operation_item", type_="foreignkey")
        batch.drop_column("operation_item_id")
        batch.create_unique_constraint(
            "uq_attempts_operation_number", ["operation_id", "attempt_number"]
        )
    with op.batch_alter_table("operations") as batch:
        batch.drop_constraint("ck_operations_status", type_="check")
        batch.create_check_constraint(
            "ck_operations_status",
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'reconciliation_required')",
        )

    op.drop_table("calendar_profiles")
    op.drop_table("calendar_reminder_plans")
    op.drop_table("operation_items")
    op.drop_table("calendar_schedule_snapshots")

    with op.batch_alter_table("reminder_rules") as batch:
        batch.drop_constraint("ck_reminder_rules_source_kind", type_="check")
        batch.drop_column("source_kind")

    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.drop_constraint("ck_calendar_event_cache_priority_basis", type_="check")
        batch.drop_constraint("ck_calendar_event_cache_priority", type_="check")
        batch.drop_constraint("ck_calendar_event_cache_recurrence_kind", type_="check")
        batch.drop_column("provider_reminders")
        batch.drop_column("priority_basis")
        batch.drop_column("priority")
        batch.drop_column("operator_tags")
        batch.drop_column("system_tags")
        batch.drop_column("recurrence_kind")
        batch.drop_column("organizer_is_self")
        batch.drop_column("has_attendees")

    with op.batch_alter_table("calendar_links") as batch:
        batch.drop_constraint("uq_calendar_links_logical_target", type_="unique")
        batch.drop_constraint("ck_calendar_links_origin_target", type_="check")
        batch.drop_constraint("ck_calendar_links_priority_basis", type_="check")
        batch.drop_constraint("ck_calendar_links_priority", type_="check")
        batch.drop_constraint("ck_calendar_links_recurrence_kind", type_="check")
        batch.drop_constraint("ck_calendar_links_origin_kind", type_="check")
        batch.alter_column("meeting_id", existing_type=sa.String(length=128), nullable=False)
        batch.alter_column("record_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_unique_constraint(
            "uq_calendar_links_target",
            ["record_id", "meeting_id", "account_id", "calendar_id"],
        )
        batch.drop_column("reminder_plan_sha256")
        batch.drop_column("priority_basis")
        batch.drop_column("priority")
        batch.drop_column("operator_tags")
        batch.drop_column("system_tags")
        batch.drop_column("recurrence_kind")
        batch.drop_column("logical_key")
        batch.drop_column("origin_kind")
