"""Bind reminders to queue parents and due-date daily threads.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reminder_rules") as batch:
        batch.alter_column(
            "destination_channel_id",
            new_column_name="queue_channel_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
        )
    with op.batch_alter_table("scheduled_notifications") as batch:
        batch.add_column(sa.Column("daily_thread_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_scheduled_notifications_daily_thread",
            "discord_daily_threads",
            ["daily_thread_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_scheduled_notifications_daily_thread",
            ["daily_thread_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_notifications") as batch:
        batch.drop_index("ix_scheduled_notifications_daily_thread")
        batch.drop_constraint(
            "fk_scheduled_notifications_daily_thread",
            type_="foreignkey",
        )
        batch.drop_column("daily_thread_id")
    with op.batch_alter_table("reminder_rules") as batch:
        batch.alter_column(
            "queue_channel_id",
            new_column_name="destination_channel_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
        )
