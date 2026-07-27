"""Make notification-only Gmail cards terminal and control-free.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLUTION_CODE = "gmail_notification"
_RESOLUTION_NOTE = "Notification delivered; no operator acknowledgement is required."
_GMAIL_ACTION_TYPES = ("gmail_archive_message", "gmail_mark_read")
_LOCAL_ACTION_TYPES = ("snooze_queue_item", "ignore_queue_item")


def _tables() -> tuple[sa.TableClause, sa.TableClause, sa.TableClause]:
    queue_items = sa.table(
        "queue_items",
        sa.column("id", sa.Uuid()),
        sa.column("primary_source_item_id", sa.Uuid()),
        sa.column("deduplication_key", sa.String()),
        sa.column("status", sa.String()),
        sa.column("resolved_at", sa.DateTime(timezone=True)),
        sa.column("resolution_code", sa.String()),
        sa.column("resolution_note", sa.String()),
        sa.column("snoozed_until", sa.DateTime(timezone=True)),
        sa.column("snooze_local_date", sa.Date()),
        sa.column("version", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    actions = sa.table(
        "actions",
        sa.column("id", sa.Uuid()),
        sa.column("queue_item_id", sa.Uuid()),
        sa.column("action_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    outbox_events = sa.table(
        "outbox_events",
        sa.column("id", sa.Uuid()),
        sa.column("event_type", sa.String()),
        sa.column("aggregate_type", sa.String()),
        sa.column("aggregate_id", sa.Uuid()),
        sa.column("deduplication_key", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("status", sa.String()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.column("lease_token", sa.Uuid()),
        sa.column("leased_until", sa.DateTime(timezone=True)),
        sa.column("last_error_code", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    return queue_items, actions, outbox_events


def upgrade() -> None:
    connection = op.get_bind()
    queue_items, actions, outbox_events = _tables()
    now = datetime.now(UTC)
    external_action_exists = sa.exists(
        sa.select(actions.c.id).where(
            actions.c.queue_item_id == queue_items.c.id,
            actions.c.action_type.in_(_GMAIL_ACTION_TYPES),
        )
    )
    rows = connection.execute(
        sa.select(queue_items.c.id, queue_items.c.version).where(
            queue_items.c.primary_source_item_id.is_not(None),
            queue_items.c.deduplication_key.like("gmail:%"),
            queue_items.c.status.in_(("pending", "failed", "snoozed")),
            ~external_action_exists,
        )
    ).all()
    for queue_item_id, version in rows:
        next_version = int(version) + 1
        connection.execute(
            sa.update(queue_items)
            .where(queue_items.c.id == queue_item_id)
            .values(
                status="completed",
                resolved_at=now,
                resolution_code=_RESOLUTION_CODE,
                resolution_note=_RESOLUTION_NOTE,
                snoozed_until=None,
                snooze_local_date=None,
                version=next_version,
                updated_at=now,
            )
        )
        connection.execute(
            sa.update(actions)
            .where(
                actions.c.queue_item_id == queue_item_id,
                actions.c.action_type.in_(_LOCAL_ACTION_TYPES),
                actions.c.status == "available",
            )
            .values(status="superseded", updated_at=now)
        )
        deduplication_key = f"discord_projection:{queue_item_id}:state:{next_version}"
        existing = connection.scalar(
            sa.select(outbox_events.c.id).where(
                outbox_events.c.deduplication_key == deduplication_key
            )
        )
        if existing is None:
            connection.execute(
                sa.insert(outbox_events).values(
                    id=uuid.uuid4(),
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item_id,
                    deduplication_key=deduplication_key,
                    payload={
                        "queue_item_id": str(queue_item_id),
                        "reason": "passive_gmail_notification_migrated",
                    },
                    status="pending",
                    attempt_count=0,
                    next_attempt_at=None,
                    lease_token=None,
                    leased_until=None,
                    last_error_code=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    queue_items, actions, outbox_events = _tables()
    now = datetime.now(UTC)
    rows = connection.execute(
        sa.select(queue_items.c.id, queue_items.c.version).where(
            queue_items.c.resolution_code == _RESOLUTION_CODE
        )
    ).all()
    for queue_item_id, version in rows:
        connection.execute(
            sa.update(queue_items)
            .where(queue_items.c.id == queue_item_id)
            .values(
                status="pending",
                resolved_at=None,
                resolution_code=None,
                resolution_note=None,
                version=int(version) + 1,
                updated_at=now,
            )
        )
        connection.execute(
            sa.update(actions)
            .where(
                actions.c.queue_item_id == queue_item_id,
                actions.c.action_type.in_(_LOCAL_ACTION_TYPES),
                actions.c.status == "superseded",
            )
            .values(status="available", updated_at=now)
        )
    connection.execute(
        sa.delete(outbox_events).where(
            outbox_events.c.event_type == "discord.projection.refresh_requested",
            outbox_events.c.payload["reason"].as_string() == "passive_gmail_notification_migrated",
        )
    )
