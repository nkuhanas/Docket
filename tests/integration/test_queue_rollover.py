from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    Action,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.rollover import RolloverService


def _drain(runner: DiscordProjectionRunner, limit: int = 30) -> int:
    count = 0
    while count < limit and runner.run_due_once():
        count += 1
    assert count < limit
    return count


@pytest.mark.integration
def test_rollover_carries_once_refreshes_history_and_archives_without_duplicates(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        item = QueueItem(
            deduplication_key="synthetic:rollover",
            material_fingerprint="b" * 64,
            category="general_action",
            title="Carry this item",
            summary="This unresolved item should appear once on each applicable day.",
            status="pending",
            priority="high",
            received_at=datetime(2026, 7, 22, 18, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        session.add(
            OutboxEvent(
                event_type="discord.projection.requested",
                aggregate_type="queue_item",
                aggregate_id=item.id,
                deduplication_key=f"discord_projection:{item.id}:date:2026-07-22",
                payload={
                    "queue_item_id": str(item.id),
                    "target_local_date": "2026-07-22",
                },
                status="pending",
            )
        )
        item_id = item.id

    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory, FakeDiscordProjectionAdapter(backend), settings
    )
    assert _drain(runner) == 1

    rollover = RolloverService(session_factory, settings)
    rollover_time = datetime(2026, 7, 23, 14, 5, tzinfo=UTC)  # 07:05 Los Angeles
    assert rollover.run_due_once(rollover_time) is True
    assert rollover.run_due_once(rollover_time) is False
    assert _drain(runner) >= 3

    with session_factory() as session:
        item = session.get(QueueItem, item_id)
        rows = session.execute(
            select(DiscordProjection, DiscordDailyThread)
            .join(
                DiscordDailyThread,
                DiscordDailyThread.id == DiscordProjection.daily_thread_id,
            )
            .where(DiscordProjection.queue_item_id == item_id)
            .order_by(DiscordDailyThread.local_date)
        ).all()
        assert item is not None
        assert [str(thread.local_date) for _, thread in rows] == [
            "2026-07-22",
            "2026-07-23",
        ]
        old_projection, old_thread = rows[0]
        current_projection, current_thread = rows[1]
        assert old_projection.status == current_projection.status == "delivered"
        assert old_thread.thread_id != current_thread.thread_id
        assert backend.messages[str(old_projection.id)]["controls"] == []
        assert {
            control["action_type"]
            for control in backend.messages[str(current_projection.id)]["controls"]
        } == {"snooze_queue_item", "ignore_queue_item"}
        current_event = next(
            event
            for event in session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == item_id,
                    OutboxEvent.event_type == "discord.projection.requested",
                )
            ).all()
            if event.payload.get("target_local_date") == "2026-07-23"
        )
        current_event.status = "pending"
        current_event.next_attempt_at = None
        session.commit()

    thread_count = len(backend.threads)
    message_count = len(backend.messages)
    restarted = DiscordProjectionRunner(
        session_factory, FakeDiscordProjectionAdapter(backend), settings
    )
    assert restarted.run_due_once()
    assert len(backend.threads) == thread_count
    assert len(backend.messages) == message_count

    assert rollover.maintain_archives(rollover_time) == 1
    assert restarted.run_due_once()
    with session_factory() as session:
        old = session.scalar(
            select(DiscordDailyThread).where(
                DiscordDailyThread.local_date == datetime(2026, 7, 22).date()
            )
        )
        assert old is not None and old.status == "archived"
        backend_thread = next(
            value for value in backend.threads.values() if value["thread_id"] == old.thread_id
        )
        assert backend_thread["archived"] is True

    with session_factory.begin() as session:
        old_projection = session.scalar(
            select(DiscordProjection)
            .join(
                DiscordDailyThread,
                DiscordDailyThread.id == DiscordProjection.daily_thread_id,
            )
            .where(
                DiscordProjection.queue_item_id == item_id,
                DiscordDailyThread.local_date == datetime(2026, 7, 22).date(),
            )
        )
        assert old_projection is not None
        session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=item_id,
                deduplication_key=f"discord_projection:{item_id}:historical-edit",
                payload={
                    "queue_item_id": str(item_id),
                    "projection_id": str(old_projection.id),
                    "target_local_date": "2026-07-22",
                    "reason": "historical_edit",
                },
                status="pending",
            )
        )
    assert restarted.run_due_once()
    assert len(backend.threads) == thread_count
    assert len(backend.messages) == message_count
    with session_factory() as session:
        old = session.scalar(
            select(DiscordDailyThread).where(
                DiscordDailyThread.local_date == datetime(2026, 7, 22).date()
            )
        )
        assert old is not None and old.status == "active"
    assert rollover.maintain_archives(rollover_time) == 1
    assert restarted.run_due_once()
    with session_factory() as session:
        old = session.scalar(
            select(DiscordDailyThread).where(
                DiscordDailyThread.local_date == datetime(2026, 7, 22).date()
            )
        )
        assert old is not None and old.status == "archived"


@pytest.mark.integration
def test_rollover_resumes_due_snooze_once_at_local_seven(session_factory) -> None:
    settings = get_settings()
    wake_at = datetime(2026, 11, 1, 15, tzinfo=UTC)  # 07:00 after DST fallback
    with session_factory.begin() as session:
        item = QueueItem(
            deduplication_key="synthetic:snooze-rollover",
            material_fingerprint="d" * 64,
            category="general_action",
            title="Wake this item",
            summary="The item should resume on its exact local date.",
            status="snoozed",
            priority="normal",
            received_at=datetime(2026, 10, 31, 18, tzinfo=UTC),
            snoozed_until=wake_at,
            snooze_local_date=date(2026, 11, 1),
        )
        session.add(item)
        session.flush()
        item_id = item.id

    rollover = RolloverService(session_factory, settings)
    assert rollover.run_due_once(wake_at + datetime.resolution)
    assert not rollover.run_due_once(wake_at + datetime.resolution)
    with session_factory() as session:
        item = session.get(QueueItem, item_id)
        actions = session.scalars(
            select(Action).where(Action.queue_item_id == item_id)
        ).all()
        projections = session.scalars(
            select(DiscordProjection).where(DiscordProjection.queue_item_id == item_id)
        ).all()
        assert item is not None and item.status == "pending" and item.version == 2
        assert item.snoozed_until is None and item.snooze_local_date is None
        assert {action.action_type for action in actions} == {
            "snooze_queue_item",
            "ignore_queue_item",
        }
        assert len(projections) == 1
