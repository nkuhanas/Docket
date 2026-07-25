"""Add durable MCP traces and contextual proposal refresh state.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("refresh_required_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("refresh_reason_code", sa.String(length=128)))

    op.create_table(
        "discord_mcp_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("guild_id", sa.String(length=64), nullable=False),
        sa.Column("source_channel_id", sa.String(length=64), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("calls", sa.JSON(), nullable=False),
        sa.Column("last_ordinal", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'interrupted')",
            name="ck_discord_mcp_traces_status",
        ),
        sa.CheckConstraint(
            "last_ordinal BETWEEN 0 AND 100",
            name="ck_discord_mcp_traces_last_ordinal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "source_channel_id",
            "source_message_id",
            name="uq_discord_mcp_trace_source",
        ),
    )


def downgrade() -> None:
    op.drop_table("discord_mcp_traces")
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("refresh_reason_code")
        batch.drop_column("refresh_required_at")
