"""Allow operator-managed Calendar lanes.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LANE_CHECK = "calendar_lane IN ('academic', 'work', 'organizations', 'personal', 'unsorted')"


def upgrade() -> None:
    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.add_column(
            sa.Column(
                "event_type",
                sa.String(length=32),
                server_default="unknown",
                nullable=False,
            )
        )
    with op.batch_alter_table("canonical_events") as batch:
        batch.drop_constraint("ck_canonical_events_calendar_lane", type_="check")
    with op.batch_alter_table("calendar_lanes") as batch:
        batch.drop_constraint("ck_calendar_lanes_lane", type_="check")
        batch.drop_constraint("ck_calendar_lanes_status", type_="check")
        batch.create_check_constraint(
            "ck_calendar_lanes_status",
            "status IN ('unprovisioned', 'provisioning', 'active', 'failed', "
            "'deleting', 'deleted')",
        )


def downgrade() -> None:
    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.drop_column("event_type")
    with op.batch_alter_table("calendar_lanes") as batch:
        batch.drop_constraint("ck_calendar_lanes_status", type_="check")
        batch.create_check_constraint(
            "ck_calendar_lanes_status",
            "status IN ('unprovisioned', 'provisioning', 'active', 'failed')",
        )
        batch.create_check_constraint(
            "ck_calendar_lanes_lane",
            "lane IN ('academic', 'work', 'organizations', 'personal', 'unsorted')",
        )
    with op.batch_alter_table("canonical_events") as batch:
        batch.create_check_constraint("ck_canonical_events_calendar_lane", _LANE_CHECK)
