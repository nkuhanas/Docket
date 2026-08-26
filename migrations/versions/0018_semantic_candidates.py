"""Retire autonomous Gmail housekeeping and add semantic candidates.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_item_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=128), nullable=False),
        sa.Column("semantic_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("mutation", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.String(length=2000), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolution", sa.JSON()),
        sa.Column("queue_item_id", sa.Uuid()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('event', 'deadline', 'response', 'task', 'information', 'noise')",
            name="ck_semantic_candidates_kind",
        ),
        sa.CheckConstraint(
            "mutation IN ('create', 'update', 'cancel', 'none')",
            name="ck_semantic_candidates_mutation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolving', 'needs_clarification', 'proposed', "
            "'executing', 'resolved', 'suppressed', 'failed')",
            name="ck_semantic_candidates_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_semantic_candidates_confidence",
        ),
        sa.CheckConstraint("failure_count >= 0", name="ck_semantic_candidates_failure_count"),
        sa.ForeignKeyConstraint(["queue_item_id"], ["queue_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_item_id",
            "candidate_index",
            name="uq_semantic_candidates_source_index",
        ),
        sa.UniqueConstraint("semantic_key", name="uq_semantic_candidates_semantic_key"),
    )

    connection = op.get_bind()
    now = datetime.now(UTC)
    connection.execute(
        sa.text(
            "UPDATE approvals SET status = 'superseded', responded_at = :now "
            "WHERE status = 'pending' AND action_revision_id IN ("
            "SELECT ar.id FROM action_revisions ar "
            "WHERE ar.action_type IN ('gmail_archive_message', 'gmail_mark_read'))"
        ),
        {"now": now},
    )
    connection.execute(
        sa.text(
            "UPDATE actions SET status = 'superseded', updated_at = :now "
            "WHERE status = 'approval_pending' "
            "AND action_type IN ('gmail_archive_message', 'gmail_mark_read')"
        ),
        {"now": now},
    )
    connection.execute(
        sa.text(
            "UPDATE queue_items SET status = 'completed', presentation = 'awareness', "
            "resolved_at = :now, resolution_code = 'gmail_notification', "
            "resolution_note = 'Autonomous Gmail housekeeping retired; no decision required.', "
            "version = version + 1, updated_at = :now "
            "WHERE status = 'awaiting_approval' AND primary_source_item_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.queue_item_id = queue_items.id "
            "AND a.status IN ('approval_pending', 'ready', 'executing', "
            "'reconciliation_required'))"
        ),
        {"now": now},
    )


def downgrade() -> None:
    op.drop_table("semantic_candidates")
