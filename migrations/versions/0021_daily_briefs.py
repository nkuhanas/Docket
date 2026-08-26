"""Add durable triage windows and daily brief membership.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
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
        "triage_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("window_kind", sa.String(length=16), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("window_kind IN ('overnight', 'waking')", name="ck_triage_windows_kind"),
        sa.CheckConstraint(
            "status IN ('open', 'sealed', 'published')", name="ck_triage_windows_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("window_kind", "local_date", name="uq_triage_windows_kind_date"),
    )
    op.create_table(
        "triage_window_memberships",
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=256)),
        sa.CheckConstraint(
            "disposition IN ('include', 'suppress')",
            name="ck_triage_window_memberships_disposition",
        ),
        sa.ForeignKeyConstraint(["window_id"], ["triage_windows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["semantic_candidate_id"], ["semantic_candidates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("window_id", "semantic_candidate_id"),
    )
    op.create_table(
        "daily_briefs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brief_kind", sa.String(length=16), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("queue_item_id", sa.Uuid()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("brief_kind IN ('morning', 'night')", name="ck_daily_briefs_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'failed')", name="ck_daily_briefs_status"
        ),
        sa.ForeignKeyConstraint(["window_id"], ["triage_windows.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["queue_item_id"], ["queue_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brief_kind", "local_date", name="uq_daily_briefs_kind_date"),
    )
    op.create_table(
        "daily_brief_items",
        sa.Column("brief_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["brief_id"], ["daily_briefs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["semantic_candidate_id"], ["semantic_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("brief_id", "semantic_candidate_id"),
        sa.UniqueConstraint(
            "brief_id", "semantic_candidate_id", name="uq_daily_brief_items_candidate"
        ),
    )


def downgrade() -> None:
    op.drop_table("daily_brief_items")
    op.drop_table("daily_briefs")
    op.drop_table("triage_window_memberships")
    op.drop_table("triage_windows")
