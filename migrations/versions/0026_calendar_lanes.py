"""Add durable Calendar lanes and event lane classification.

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LANE_CHECK = "calendar_lane IN ('academic', 'work', 'organizations', 'personal', 'unsorted')"


def upgrade() -> None:
    op.create_table(
        "calendar_lanes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("color_hex", sa.String(length=7), nullable=False),
        sa.Column("calendar_id", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lane IN ('academic', 'work', 'organizations', 'personal', 'unsorted')",
            name="ck_calendar_lanes_lane",
        ),
        sa.CheckConstraint(
            "status IN ('unprovisioned', 'provisioning', 'active', 'failed')",
            name="ck_calendar_lanes_status",
        ),
        sa.CheckConstraint(
            "color_hex LIKE '#______' AND length(color_hex) = 7",
            name="ck_calendar_lanes_color_hex",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "lane", name="uq_calendar_lanes_account_lane"),
        sa.UniqueConstraint("account_id", "calendar_id", name="uq_calendar_lanes_account_calendar"),
    )
    with op.batch_alter_table("canonical_events") as batch:
        batch.add_column(
            sa.Column(
                "calendar_lane",
                sa.String(length=32),
                server_default="unsorted",
                nullable=False,
            )
        )
        batch.create_check_constraint("ck_canonical_events_calendar_lane", _LANE_CHECK)


def downgrade() -> None:
    with op.batch_alter_table("canonical_events") as batch:
        batch.drop_constraint("ck_canonical_events_calendar_lane", type_="check")
        batch.drop_column("calendar_lane")
    op.drop_table("calendar_lanes")
