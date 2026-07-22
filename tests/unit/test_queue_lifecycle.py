import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import LocalActionResponse
from docket.models import (
    Action,
    ActionRevision,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.schemas.queue import IgnoreQueueItemInput, SnoozeQueueItemInput
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.local_actions import LocalActionService
from docket.services.queue import QueueService, ensure_local_actions

OPERATOR_ID = "000000000000000001"
GUILD_ID = "000000000000000002"
CHAT_CHANNEL_ID = "000000000000000003"
MESSAGE_ID = "111111111111111111"


def _source(intent_index: int) -> dict[str, object]:
    return {
        "source_type": "discord_message",
        "source_object_id": MESSAGE_ID,
        "metadata": {
            "guild_id": GUILD_ID,
            "channel_id": CHAT_CHANNEL_ID,
            "message_id": MESSAGE_ID,
            "user_id": OPERATOR_ID,
            "intent_index": intent_index,
        },
    }


def _pending_item() -> QueueItem:
    return QueueItem(
        deduplication_key=f"synthetic:{uuid.uuid4()}",
        material_fingerprint="a" * 64,
        category="general_action",
        title="Synthetic queue item",
        summary="A bounded synthetic item used to verify the queue lifecycle.",
        status="pending",
        priority="normal",
        received_at=datetime(2026, 7, 22, 16, tzinfo=UTC),
    )


def test_manual_snooze_is_idempotent_and_uses_local_rollover(session) -> None:
    item = _pending_item()
    session.add(item)
    session.flush()
    request = SnoozeQueueItemInput(
        queue_item_id=item.id,
        expected_version=1,
        request_key=f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:1",
        source=_source(1),
        actor_id=OPERATOR_ID,
        snooze_local_date=date(2026, 11, 1),
        reason="Revisit after the weekend",
    )

    first = QueueService(session).snooze(request)
    replay = QueueService(session).snooze(request)

    assert first.status == "snoozed"
    assert first.version == 2
    assert first.snoozed_until == datetime(2026, 11, 1, 15, tzinfo=UTC)
    assert replay.disposition == "replayed_request"
    assert replay.queue_item_id == item.id
    assert (
        len(session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == item.id)).all())
        == 1
    )


def test_manual_ignore_is_optimistically_locked_and_does_not_mutate_source(session) -> None:
    item = _pending_item()
    session.add(item)
    session.flush()
    stale = IgnoreQueueItemInput(
        queue_item_id=item.id,
        expected_version=2,
        request_key=f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:2",
        source=_source(2),
        actor_id=OPERATOR_ID,
        reason="Not actionable",
    )

    with pytest.raises(DocketError) as conflict:
        QueueService(session).ignore(stale)
    assert conflict.value.code == "version_conflict"
    session.rollback()

    session.add(item)
    session.flush()
    request = IgnoreQueueItemInput(
        queue_item_id=item.id,
        expected_version=1,
        request_key=f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:3",
        source=_source(3),
        actor_id=OPERATOR_ID,
        reason="Not actionable",
    )
    result = QueueService(session).ignore(request)
    assert result.status == "ignored"
    assert item.resolution_code == "operator_ignored"
    assert item.resolution_note == "Not actionable"


def test_manual_queue_write_rejects_forged_configured_discord_context(session) -> None:
    item = _pending_item()
    session.add(item)
    session.flush()
    attacker = "999999999999999999"
    source = _source(4)
    source["metadata"] = {**source["metadata"], "user_id": attacker}
    request = IgnoreQueueItemInput(
        queue_item_id=item.id,
        expected_version=1,
        request_key=f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:4",
        source=source,
        actor_id=attacker,
        reason="Forged actor",
    )

    with pytest.raises(DocketError) as rejected:
        QueueService(session).ignore(request)

    assert rejected.value.code == "invalid_source_context"
    assert item.status == "pending"
    assert item.version == 1


def test_failed_queue_item_only_exposes_valid_local_controls(session) -> None:
    item = _pending_item()
    session.add(item)
    session.flush()
    initial = ensure_local_actions(session, item, projection_date=date(2026, 7, 22))
    assert {revision.action_type for revision in initial} == {
        "snooze_queue_item",
        "ignore_queue_item",
    }

    item.status = "failed"
    item.version += 1
    current = ensure_local_actions(session, item, projection_date=date(2026, 7, 22))
    actions = session.scalars(select(Action).where(Action.queue_item_id == item.id)).all()

    assert [revision.action_type for revision in current] == ["ignore_queue_item"]
    assert next(
        action for action in actions if action.action_type == "snooze_queue_item"
    ).status == "superseded"


def test_queue_reads_filter_and_return_primary_source_identity(session) -> None:
    source_item_id = uuid.uuid4()
    matching = _pending_item()
    matching.primary_source_item_id = source_item_id
    session.add_all([matching, _pending_item()])
    session.flush()

    items = QueueService(session).list(source_item_id=source_item_id)

    assert len(items) == 1
    assert items[0]["queue_item_id"] == str(matching.id)
    assert items[0]["primary_source_item_id"] == str(source_item_id)


def test_signed_local_button_executes_once_and_refreshes_the_same_card(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        item = _pending_item()
        session.add(item)
        session.flush()
        session.add(
            OutboxEvent(
                event_type="discord.projection.requested",
                aggregate_type="queue_item",
                aggregate_id=item.id,
                deduplication_key=f"discord_projection:{item.id}:synthetic",
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
    assert runner.run_due_once()

    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        daily_thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and daily_thread is not None
        controls = backend.messages[str(projection.id)]["controls"]
        ignore = next(
            control for control in controls if control["action_type"] == "ignore_queue_item"
        )
        revision = session.get(ActionRevision, uuid.UUID(ignore["action_revision_id"]))
        assert revision is not None
        response = LocalActionResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id="local-ignore-1",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=daily_thread.thread_id,
            parent_channel_id=settings.queue_channel_id,
            projection_id=projection.id,
            message_id=projection.message_id,
            responded_at=datetime.now(UTC),
            action_revision_id=revision.id,
            action_token=ignore["token"],
        )

    forged_actor = response.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "discord_interaction_id": "local-forged-actor",
            "discord_user_id": "999999999999999999",
        }
    )
    with session_factory.begin() as session, pytest.raises(DocketError) as forged:
        LocalActionService(session).respond(forged_actor)
    assert forged.value.code == "invalid_local_action_context"

    copied_card = response.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "discord_interaction_id": "local-copied-card",
            "message_id": "999999999999999999",
        }
    )
    with session_factory.begin() as session, pytest.raises(DocketError) as copied:
        LocalActionService(session).respond(copied_card)
    assert copied.value.code == "invalid_local_action_projection"

    with session_factory.begin() as session:
        result = LocalActionService(session).respond(response)
    assert result["queue_status"] == "ignored"

    with session_factory.begin() as session, pytest.raises(DocketError) as replay:
        LocalActionService(session).respond(response)
    assert replay.value.code == "interaction_replay"

    assert runner.run_due_once()
    with session_factory() as session:
        item = session.get(QueueItem, item_id)
        projection = session.scalar(select(DiscordProjection))
        assert item is not None and item.status == "ignored"
        assert projection is not None and projection.projection_version == 2
        assert backend.messages[str(projection.id)]["controls"] == []
