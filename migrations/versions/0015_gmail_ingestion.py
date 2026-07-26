"""Add Gmail checkpoint, staged-source, and queue provenance state.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[sa.DateTime], sa.Column[sa.DateTime]]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "connector_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(length=128), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("observed_through", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="ck_connector_checkpoints_version"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "stream",
            name="uq_connector_checkpoints_account_stream",
        ),
    )
    op.create_table(
        "source_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_object_id", sa.String(length=1024), nullable=False),
        sa.Column("external_parent_id", sa.String(length=1024)),
        sa.Column("source_version", sa.String(length=255), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("minimal_headers", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_by", sa.String(length=255)),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("classification", sa.JSON()),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint("provider = 'gmail'", name="ck_source_items_provider"),
        sa.CheckConstraint(
            "status IN ('staged', 'claimed', 'classified', 'ignored', 'failed')",
            name="ck_source_items_status",
        ),
        sa.CheckConstraint(
            "failure_count >= 0",
            name="ck_source_items_failure_count",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "provider",
            "external_object_id",
            "source_version",
            name="uq_source_items_external_version",
        ),
        sa.UniqueConstraint(
            "account_id",
            "provider",
            "source_fingerprint",
            name="uq_source_items_fingerprint",
        ),
    )
    op.create_index(
        "ix_source_items_claimable",
        "source_items",
        ["status", "next_attempt_at", "claimed_until", "received_at"],
    )
    op.create_table(
        "queue_item_sources",
        sa.Column("queue_item_id", sa.Uuid(), nullable=False),
        sa.Column("source_item_id", sa.Uuid(), nullable=False),
        sa.Column("relationship", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "relationship IN ('primary', 'supporting', 'update')",
            name="ck_queue_item_sources_relationship",
        ),
        sa.ForeignKeyConstraint(
            ["queue_item_id"],
            ["queue_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_item_id"],
            ["source_items.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("queue_item_id", "source_item_id"),
    )
    with op.batch_alter_table("queue_items") as batch:
        batch.create_foreign_key(
            "fk_queue_items_primary_source_item",
            "source_items",
            ["primary_source_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("queue_items") as batch:
        batch.drop_constraint(
            "fk_queue_items_primary_source_item",
            type_="foreignkey",
        )
    op.drop_table("queue_item_sources")
    op.drop_index("ix_source_items_claimable", table_name="source_items")
    op.drop_table("source_items")
    op.drop_table("connector_checkpoints")
