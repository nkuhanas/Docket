"""Add the Milestone 2.5 Discord projection capability state.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
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
        "discord_daily_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("guild_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("thread_name", sa.String(length=100), nullable=False),
        sa.Column("thread_id", sa.String(length=64)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("auto_archive_minutes", sa.Integer()),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=128)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'archived', 'failed')",
            name="ck_discord_daily_threads_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id", "channel_id", "local_date", name="uq_discord_daily_thread_date"
        ),
        sa.UniqueConstraint("thread_id", name="uq_discord_daily_threads_thread_id"),
    )
    op.create_table(
        "discord_projections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("queue_item_id", sa.Uuid(), nullable=False),
        sa.Column("daily_thread_id", sa.Uuid(), nullable=False),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=64)),
        sa.Column("render_schema_version", sa.Integer(), nullable=False),
        sa.Column("render_sha256", sa.String(length=64), nullable=False),
        sa.Column("component_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_error_code", sa.String(length=128)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_discord_projections_status",
        ),
        sa.ForeignKeyConstraint(["queue_item_id"], ["queue_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["daily_thread_id"], ["discord_daily_threads.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "queue_item_id", "daily_thread_id", name="uq_discord_projection_item_thread"
        ),
        sa.UniqueConstraint("message_id", name="uq_discord_projections_message_id"),
    )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("control_projection_id", sa.Uuid()))
        batch_op.add_column(sa.Column("response_parent_channel_id", sa.String(length=64)))
        batch_op.add_column(sa.Column("response_projection_id", sa.Uuid()))
        batch_op.create_foreign_key(
            "fk_approvals_control_projection",
            "discord_projections",
            ["control_projection_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_approvals_response_projection",
            "discord_projections",
            ["response_projection_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint("fk_approvals_response_projection", type_="foreignkey")
        batch_op.drop_constraint("fk_approvals_control_projection", type_="foreignkey")
        batch_op.drop_column("response_projection_id")
        batch_op.drop_column("response_parent_channel_id")
        batch_op.drop_column("control_projection_id")
    op.drop_table("discord_projections")
    op.drop_table("discord_daily_threads")
