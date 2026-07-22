from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import DiscordProjection, OutboxEvent, QueueItem
from docket.providers.discord import (
    DiscordProjectionError,
    FakeDiscordBackend,
    FakeDiscordProjectionAdapter,
)
from docket.services.discord_projection import DiscordProjectionRunner


class FailingProjectionAdapter(FakeDiscordProjectionAdapter):
    def put_projection(self, projection_id, payload):
        raise DiscordProjectionError("discord_forbidden", "Projection was forbidden")


@pytest.mark.integration
def test_exhausted_projection_reports_one_durable_system_alert(session_factory) -> None:
    settings = get_settings().model_copy(update={"discord_projection_max_attempts": 2})
    with session_factory.begin() as session:
        item = QueueItem(
            deduplication_key="synthetic:projection-failure",
            material_fingerprint="c" * 64,
            category="general_action",
            title="Projection failure fixture",
            summary="Canonical state must survive a Discord projection failure.",
            status="pending",
            priority="normal",
            received_at=datetime(2026, 7, 22, 18, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        original = OutboxEvent(
            event_type="discord.projection.requested",
            aggregate_type="queue_item",
            aggregate_id=item.id,
            deduplication_key=f"discord_projection:{item.id}:failure",
            payload={
                "queue_item_id": str(item.id),
                "target_local_date": "2026-07-22",
            },
            status="pending",
        )
        session.add(original)
        session.flush()
        original_id = original.id

    backend = FakeDiscordBackend()
    failing = DiscordProjectionRunner(session_factory, FailingProjectionAdapter(backend), settings)
    assert failing.run_due_once()
    with session_factory.begin() as session:
        event = session.get(OutboxEvent, original_id)
        assert event is not None and event.status == "pending"
        event.next_attempt_at = None
    assert failing.run_due_once()

    with session_factory() as session:
        event = session.get(OutboxEvent, original_id)
        projection = session.scalar(select(DiscordProjection))
        alerts = session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "discord.system_alert.requested")
        ).all()
        assert event is not None and event.status == "failed"
        assert event.last_error_code == "discord_forbidden"
        assert projection is not None and projection.status == "failed"
        assert len(alerts) == 1 and alerts[0].status == "pending"

    recovered = DiscordProjectionRunner(
        session_factory, FakeDiscordProjectionAdapter(backend), settings
    )
    assert recovered.run_due_once()
    assert len(backend.system_messages) == 1
    with session_factory() as session:
        alert = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "discord.system_alert.requested")
        )
        assert alert is not None and alert.status == "delivered"
        assert str(alert.payload["discord_message_id"]).isdigit()
