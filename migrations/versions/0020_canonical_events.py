"""Add canonical events, observations, and provider bindings.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
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
        "canonical_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("event_spec", sa.JSON(), nullable=False),
        sa.Column("reminder_plan", sa.JSON()),
        sa.Column("entity_refs", sa.JSON(), nullable=False),
        sa.Column("context_labels", sa.JSON(), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('proposed', 'active', 'cancelled', 'archived')",
            name="ck_canonical_events_status",
        ),
        sa.CheckConstraint(
            "authority IN ('explicit_user', 'canonical', 'inferred')",
            name="ck_canonical_events_authority",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_key", name="uq_canonical_events_key"),
    )
    op.create_table(
        "event_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_event_id", sa.Uuid()),
        sa.Column("source_item_id", sa.Uuid()),
        sa.Column("semantic_candidate_id", sa.Uuid()),
        sa.Column("mutation", sa.String(length=16), nullable=False),
        sa.Column("observed_fields", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("correlation_state", sa.String(length=16), nullable=False),
        sa.Column("candidate_event_ids", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "mutation IN ('create', 'update', 'cancel', 'none')",
            name="ck_event_observations_mutation",
        ),
        sa.CheckConstraint(
            "correlation_state IN ('new', 'matched', 'ambiguous', 'unresolved')",
            name="ck_event_observations_correlation",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_event_id"], ["canonical_events.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["semantic_candidate_id"], ["semantic_candidates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "semantic_candidate_id", name="uq_event_observations_semantic_candidate"
        ),
    )
    op.create_table(
        "provider_event_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_event_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_id", sa.String(length=1024), nullable=False),
        sa.Column("provider_event_id", sa.String(length=1024), nullable=False),
        sa.Column("provider_etag", sa.String(length=1024)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_snapshot", sa.JSON(), nullable=False),
        sa.Column("independently_modified_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'cancelled', 'diverged')",
            name="ck_provider_event_bindings_status",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_event_id"], ["canonical_events.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "calendar_id",
            "provider_event_id",
            name="uq_provider_event_bindings_target",
        ),
        sa.UniqueConstraint(
            "canonical_event_id",
            "account_id",
            "calendar_id",
            name="uq_provider_event_bindings_canonical_target",
        ),
    )
    with op.batch_alter_table("calendar_links") as batch:
        batch.add_column(sa.Column("canonical_event_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_calendar_links_canonical_event",
            "canonical_events",
            ["canonical_event_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("calendar_links") as batch:
        batch.drop_constraint("fk_calendar_links_canonical_event", type_="foreignkey")
        batch.drop_column("canonical_event_id")
    op.drop_table("provider_event_bindings")
    op.drop_table("event_observations")
    op.drop_table("canonical_events")
