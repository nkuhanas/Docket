"""Milestone 1 durable core.

Revision ID: 0001
Revises: none
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
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
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_account_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255)),
        sa.Column("email_address", sa.String(length=320)),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("credential_ref", sa.String(length=512)),
        *_timestamps(),
        sa.CheckConstraint("provider IN ('google', 'discord')", name="ck_accounts_provider"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_account_id", name="uq_accounts_provider_external"
        ),
    )
    op.create_table(
        "records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("record_type", sa.String(length=64), nullable=False),
        sa.Column("canonical_key", sa.String(length=512), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from_date", sa.Date()),
        sa.Column("valid_until_date", sa.Date()),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_records_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_type", "canonical_key", name="uq_records_identity"),
    )
    op.create_table(
        "command_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(length=512), nullable=False),
        sa.Column("operation_name", sa.String(length=128), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error_code", sa.String(length=128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('in_progress', 'succeeded', 'failed')",
            name="ck_command_requests_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key"),
    )
    op.create_table(
        "record_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_account_id", sa.Uuid()),
        sa.Column("source_object_id", sa.String(length=255)),
        sa.Column("source_request_key", sa.String(length=512), nullable=False),
        sa.Column("source_version", sa.String(length=255)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["record_id"], ["records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_request_key"),
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("deduplication_key", sa.String(length=512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'failed')",
            name="ck_outbox_events_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64)),
        sa.Column("entity_id", sa.Uuid()),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255)),
        sa.Column("request_id", sa.Uuid()),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("outbox_events")
    op.drop_table("record_sources")
    op.drop_table("command_requests")
    op.drop_table("records")
    op.drop_table("accounts")
