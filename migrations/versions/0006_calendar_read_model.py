"""Add the Calendar read model and durable reminders for Milestone 3.5.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
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
        "calendar_sync_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_id", sa.String(length=1024), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_generation", sa.Uuid()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'syncing', 'current', 'stale', 'failed')",
            name="ck_calendar_sync_states_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "calendar_id", name="uq_calendar_sync_states_target"),
    )
    op.create_table(
        "calendar_event_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_id", sa.String(length=1024), nullable=False),
        sa.Column("provider_event_id", sa.String(length=1024), nullable=False),
        sa.Column("snapshot_generation", sa.Uuid(), nullable=False),
        sa.Column("recurring_event_id", sa.String(length=1024)),
        sa.Column("original_start_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=512)),
        sa.Column("location", sa.String(length=1000)),
        sa.Column("is_all_day", sa.Boolean(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True)),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("timezone", sa.String(length=128)),
        sa.Column("provider_etag", sa.String(length=1024)),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True)),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('confirmed', 'tentative', 'cancelled')",
            name="ck_calendar_event_cache_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "calendar_id",
            "provider_event_id",
            name="uq_calendar_event_cache_provider_event",
        ),
    )
    op.create_index(
        "ix_calendar_event_cache_timed",
        "calendar_event_cache",
        ["account_id", "calendar_id", "start_at"],
    )
    op.create_index(
        "ix_calendar_event_cache_all_day",
        "calendar_event_cache",
        ["account_id", "calendar_id", "start_date"],
    )
    op.create_index(
        "ix_calendar_event_cache_generation",
        "calendar_event_cache",
        ["account_id", "calendar_id", "snapshot_generation"],
    )
    op.create_table(
        "reminder_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_id", sa.String(length=1024), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("provider_event_id", sa.String(length=1024)),
        sa.Column("lead_seconds", sa.Integer(), nullable=False),
        sa.Column("destination_channel_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_id", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("scope IN ('calendar', 'event')", name="ck_reminder_rules_scope"),
        sa.CheckConstraint(
            "(scope = 'calendar' AND provider_event_id IS NULL) OR "
            "(scope = 'event' AND provider_event_id IS NOT NULL)",
            name="ck_reminder_rules_scope_event",
        ),
        sa.CheckConstraint("lead_seconds >= 0", name="ck_reminder_rules_lead_seconds"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "scheduled_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reminder_rule_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_event_id", sa.Uuid()),
        sa.Column("provider_event_id", sa.String(length=1024), nullable=False),
        sa.Column("event_start_key", sa.String(length=255), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid()),
        sa.Column("discord_message_id", sa.String(length=64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=128)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'cancelled', 'failed')",
            name="ck_scheduled_notifications_status",
        ),
        sa.ForeignKeyConstraint(["reminder_rule_id"], ["reminder_rules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["calendar_event_id"], ["calendar_event_cache.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["outbox_event_id"], ["outbox_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reminder_rule_id",
            "provider_event_id",
            "event_start_key",
            name="uq_scheduled_notifications_occurrence",
        ),
    )
    op.create_index(
        "ix_scheduled_notifications_due",
        "scheduled_notifications",
        ["status", "scheduled_for"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_notifications_due", table_name="scheduled_notifications")
    op.drop_table("scheduled_notifications")
    op.drop_table("reminder_rules")
    op.drop_index("ix_calendar_event_cache_generation", table_name="calendar_event_cache")
    op.drop_index("ix_calendar_event_cache_all_day", table_name="calendar_event_cache")
    op.drop_index("ix_calendar_event_cache_timed", table_name="calendar_event_cache")
    op.drop_table("calendar_event_cache")
    op.drop_table("calendar_sync_states")
