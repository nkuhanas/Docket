import uuid
from datetime import UTC, date, datetime, timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.errors import VersionConflict
from docket.models import (
    Account,
    AuditEvent,
    CalendarEventCache,
    CalendarSyncState,
    OutboxEvent,
    ReminderRule,
    ScheduledNotification,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.calendar import CalendarSnapshotEvent, CalendarSnapshotPage
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.calendar import DisableReminderRuleInput, SetReminderRuleInput
from docket.schemas.records import RecordSourceInput
from docket.services.calendar_sync import CalendarReadService, CalendarSyncService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.reminders import ReminderDispatcher, ReminderRuleService, materialize_reminders


def _account(session_factory) -> uuid.UUID:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="primary",
            display_name="Configured Google account",
            capabilities=["calendar_read", "calendar_write"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        return account.id


def _timed(
    event_id: str, start: datetime, *, summary: str = "Office hours"
) -> CalendarSnapshotEvent:
    return CalendarSnapshotEvent(
        provider_event_id=event_id,
        status="confirmed",
        summary=summary,
        location="Building 14",
        is_all_day=False,
        start_at=start,
        end_at=start + timedelta(hours=1),
        timezone="America/Los_Angeles",
        provider_etag=f'"{event_id}"',
        provider_updated_at=start - timedelta(days=1),
    )


def _source(message_id: str, intent_index: int = 0) -> RecordSourceInput:
    settings = get_settings()
    return RecordSourceInput(
        source_type="discord_message",
        source_object_id=message_id,
        metadata={
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "user_id": settings.operator_discord_user_id,
            "intent_index": intent_index,
        },
    )


def _request_key(message_id: str, intent_index: int = 0) -> str:
    settings = get_settings()
    return (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:{intent_index}"
    )


@pytest.mark.integration
def test_paginated_snapshot_promotes_atomically_and_partial_failure_preserves_prior_generation(
    session_factory,
) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    clock_value = [base]
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.snapshot_page_size = 1
    provider.put_snapshot_event(_timed("event-a", base + timedelta(days=1), summary="A1"))
    provider.put_snapshot_event(_timed("event-b", base + timedelta(days=2), summary="B1"))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock_value[0])

    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None and state.status == "current"
        first_generation = state.snapshot_generation
        first_window = (state.window_start, state.window_end)
        rows = session.scalars(
            select(CalendarEventCache).order_by(CalendarEventCache.provider_event_id)
        ).all()
        assert [(row.provider_event_id, row.summary) for row in rows] == [
            ("event-a", "A1"),
            ("event-b", "B1"),
        ]

    provider.put_snapshot_event(_timed("event-a", base + timedelta(days=1), summary="A2"))
    provider.remove_snapshot_event("event-b")
    provider.put_snapshot_event(_timed("event-c", base + timedelta(days=3), summary="C1"))
    provider.fail_snapshot_page = 1
    clock_value[0] += timedelta(days=31)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None and state.status == "stale"
        assert state.snapshot_generation == first_generation
        assert (state.window_start, state.window_end) == first_window
        assert state.last_error_code == "fake_snapshot_failure"
        rows = session.scalars(
            select(CalendarEventCache).order_by(CalendarEventCache.provider_event_id)
        ).all()
        assert [(row.provider_event_id, row.summary) for row in rows] == [
            ("event-a", "A1"),
            ("event-b", "B1"),
        ]

    provider.fail_snapshot_page = None
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None and state.status == "current"
        assert state.snapshot_generation != first_generation
        assert (state.window_start, state.window_end) != first_window
        rows = session.scalars(
            select(CalendarEventCache).order_by(CalendarEventCache.provider_event_id)
        ).all()
        assert [(row.provider_event_id, row.summary) for row in rows] == [
            ("event-a", "A2"),
            ("event-c", "C1"),
        ]


@pytest.mark.integration
def test_snapshot_rejects_duplicate_events_and_page_token_loops(
    session_factory, monkeypatch
) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    event = _timed("duplicate", base + timedelta(hours=1))

    def duplicate_page(**_kwargs) -> CalendarSnapshotPage:
        return CalendarSnapshotPage(events=(event, event), next_page_token=None)

    monkeypatch.setattr(provider, "list_events_page", duplicate_page)
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None
        assert state.status == "failed"
        assert state.last_error_code == "calendar_snapshot_duplicate_event"

    def looping_page(**_kwargs) -> CalendarSnapshotPage:
        return CalendarSnapshotPage(events=(), next_page_token="loop")

    monkeypatch.setattr(provider, "list_events_page", looping_page)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None
        assert state.status == "failed"
        assert state.last_error_code == "calendar_snapshot_page_loop"


@pytest.mark.integration
def test_indexed_lookup_is_bounded_redacted_and_reports_staleness(session_factory) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    clock_value = [base]
    settings = get_settings().model_copy(
        update={"calendar_reads_enabled": True, "calendar_stale_seconds": 60}
    )
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("timed", base + timedelta(hours=2)))
    provider.put_snapshot_event(
        CalendarSnapshotEvent(
            provider_event_id="all-day",
            status="tentative",
            summary="Registration deadline",
            location=None,
            is_all_day=True,
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 24),
            timezone="America/Los_Angeles",
        )
    )
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock_value[0])
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: clock_value[0])
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    result = read.list_events(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        start=base,
        end=base + timedelta(days=2),
        text_filter=None,
        limit=100,
        freshness="prefer_cache",
    )
    assert [event["provider_event_id"] for event in result["events"]] == [
        "timed",
        "all-day",
    ]
    assert result["freshness"]["stale"] is False
    assert result["freshness"]["covered"] is True
    assert not {
        "description",
        "attendees",
        "conference_data",
        "provider_etag",
    }.intersection(result["events"][0])

    clock_value[0] += timedelta(seconds=61)
    stale = read.get_sync_status(account_id, settings.google_calendar_id)
    assert stale["stale"] is True
    assert "snapshot_generation" not in stale


@pytest.mark.integration
def test_all_day_lookup_honors_exclusive_midnight_end(session_factory) -> None:
    base = datetime(2026, 7, 22, 7, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(
        CalendarSnapshotEvent(
            provider_event_id="today",
            status="confirmed",
            summary="Today",
            location=None,
            is_all_day=True,
            start_date=date(2026, 7, 22),
            end_date=date(2026, 7, 23),
            timezone="America/Los_Angeles",
        )
    )
    provider.put_snapshot_event(
        CalendarSnapshotEvent(
            provider_event_id="tomorrow",
            status="confirmed",
            summary="Tomorrow",
            location=None,
            is_all_day=True,
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 24),
            timezone="America/Los_Angeles",
        )
    )
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    result = read.list_events(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        start=base,
        end=base + timedelta(days=1),
        text_filter=None,
        limit=100,
        freshness="prefer_cache",
    )

    assert [event["provider_event_id"] for event in result["events"]] == ["today"]


@pytest.mark.integration
def test_require_fresh_returns_stale_cache_within_configured_wait(
    session_factory, monkeypatch
) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    settings = get_settings().model_copy(
        update={
            "calendar_reads_enabled": True,
            "calendar_require_fresh_wait_seconds": 0.05,
        }
    )
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("slow-event", base + timedelta(hours=2)))
    clock_value = [base]
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock_value[0])
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: clock_value[0])
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    clock_value[0] += timedelta(seconds=1)

    started = Event()
    release = Event()
    original = provider.list_events_page

    def slow_page(**kwargs) -> CalendarSnapshotPage:
        started.set()
        release.wait(timeout=1)
        return original(**kwargs)

    monkeypatch.setattr(provider, "list_events_page", slow_page)
    before = monotonic()
    try:
        result = read.list_events(
            account_id=account_id,
            calendar_id=settings.google_calendar_id,
            start=base,
            end=base + timedelta(days=1),
            text_filter=None,
            limit=100,
            freshness="require_fresh",
        )
        elapsed = monotonic() - before
    finally:
        release.set()

    assert started.is_set()
    assert elapsed < 0.5
    assert result["freshness"]["stale"] is True
    assert result["refresh_pending"] is True

    deadline = monotonic() + 1
    while monotonic() < deadline:
        if read.get_sync_status(account_id, settings.google_calendar_id)["status"] == "current":
            break
        sleep(0.01)
    assert read.get_sync_status(account_id, settings.google_calendar_id)["status"] == "current"


@pytest.mark.integration
def test_event_change_reschedules_then_cancellation_cancels_one_notification(
    session_factory,
) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("event-1", base + timedelta(hours=4)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    message_id = "777777777777777777"
    with session_factory.begin() as session:
        result = ReminderRuleService(session, settings).set(
            SetReminderRuleInput(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="event-1",
                lead_seconds=1800,
                request_key=_request_key(message_id),
                source=_source(message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )
        rule_id = result.rule_id
    with session_factory() as session:
        original = session.scalar(
            select(ScheduledNotification).where(ScheduledNotification.reminder_rule_id == rule_id)
        )
        assert original is not None
        original_id = original.id
        original_key = original.event_start_key

    provider.put_snapshot_event(_timed("event-1", base + timedelta(hours=5)))
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    with session_factory() as session:
        moved = session.get(ScheduledNotification, original_id)
        assert moved is not None and moved.status == "pending"
        assert moved.event_start_key != original_key

    provider.remove_snapshot_event("event-1")
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)
    with session_factory() as session:
        cancelled = session.get(ScheduledNotification, original_id)
        assert cancelled is not None and cancelled.status == "cancelled"
        assert cancelled.last_error_code == "calendar_event_removed"


@pytest.mark.integration
def test_recurring_series_rule_tracks_cancelled_provider_tombstone(session_factory) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    instance = CalendarSnapshotEvent(
        provider_event_id="series-instance",
        recurring_event_id="series-master",
        status="confirmed",
        summary="Recurring office hours",
        location="Building 14",
        is_all_day=False,
        start_at=base + timedelta(hours=4),
        end_at=base + timedelta(hours=5),
        timezone="America/Los_Angeles",
    )
    provider.put_snapshot_event(instance)
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    message_id = "666666666666666666"
    with session_factory.begin() as session:
        result = ReminderRuleService(session, settings).set(
            SetReminderRuleInput(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="series-master",
                lead_seconds=1800,
                request_key=_request_key(message_id),
                source=_source(message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )
        rule_id = result.rule_id

    provider.put_snapshot_event(
        CalendarSnapshotEvent(
            provider_event_id=instance.provider_event_id,
            recurring_event_id=instance.recurring_event_id,
            status="cancelled",
            summary=None,
            location=None,
            is_all_day=False,
            start_at=instance.start_at,
            end_at=instance.end_at,
            timezone=instance.timezone,
        )
    )
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    with session_factory() as session:
        cached = session.scalar(select(CalendarEventCache))
        notification = session.scalar(
            select(ScheduledNotification).where(ScheduledNotification.reminder_rule_id == rule_id)
        )
        assert cached is not None and cached.status == "cancelled"
        assert notification is not None and notification.status == "cancelled"
        assert notification.last_error_code == "calendar_event_cancelled"


@pytest.mark.integration
def test_reminder_rule_is_idempotent_versioned_disabled_and_audited(session_factory) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("rule-event", base + timedelta(hours=2)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    create_message = "555555555555555555"
    create = SetReminderRuleInput(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        scope="event",
        provider_event_id="rule-event",
        lead_seconds=900,
        request_key=_request_key(create_message),
        source=_source(create_message),
        actor_id=settings.operator_discord_user_id,
    )
    with session_factory.begin() as session:
        created = ReminderRuleService(session, settings).set(create)
        replayed = ReminderRuleService(session, settings).set(create)
        assert replayed.rule_id == created.rule_id
        assert replayed.disposition == "replayed_request"

    update_message = "444444444444444444"
    update = SetReminderRuleInput(
        rule_id=created.rule_id,
        expected_version=1,
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        scope="event",
        provider_event_id="rule-event",
        lead_seconds=600,
        request_key=_request_key(update_message),
        source=_source(update_message),
        actor_id=settings.operator_discord_user_id,
    )
    with session_factory.begin() as session:
        updated = ReminderRuleService(session, settings).set(update)
        assert updated.version == 2

    stale_message = "333333333333333333"
    stale_update = update.model_copy(
        update={
            "request_key": _request_key(stale_message),
            "source": _source(stale_message),
        }
    )
    with pytest.raises(VersionConflict), session_factory.begin() as session:
        ReminderRuleService(session, settings).set(stale_update)

    disable_message = "222222222222222222"
    with session_factory.begin() as session:
        disabled = ReminderRuleService(session, settings).disable(
            DisableReminderRuleInput(
                rule_id=created.rule_id,
                expected_version=2,
                request_key=_request_key(disable_message),
                source=_source(disable_message),
                actor_id=settings.operator_discord_user_id,
                reason="Milestone smoke complete",
            )
        )
        assert disabled.version == 3
        assert disabled.enabled is False

    with session_factory() as session:
        rules = session.scalars(select(ReminderRule)).all()
        notifications = session.scalars(select(ScheduledNotification)).all()
        audit_types = set(
            session.scalars(
                select(AuditEvent.event_type).where(
                    AuditEvent.entity_type == "reminder_rule",
                    AuditEvent.entity_id == created.rule_id,
                )
            ).all()
        )
        assert len(rules) == 1
        assert all(item.status == "cancelled" for item in notifications)
        assert audit_types == {
            "reminder_rule.created",
            "reminder_rule.updated",
            "reminder_rule.disabled",
        }


@pytest.mark.integration
def test_late_calendar_refresh_produces_visibly_late_reminder(session_factory) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("late-event", base + timedelta(minutes=5)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    message_id = "111111111111111111"
    with session_factory.begin() as session:
        ReminderRuleService(session, settings).set(
            SetReminderRuleInput(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="late-event",
                lead_seconds=600,
                request_key=_request_key(message_id),
                source=_source(message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )

    dispatcher = ReminderDispatcher(
        session_factory, settings, clock=lambda: base + timedelta(seconds=1)
    )
    assert dispatcher.run_due_once()
    backend = FakeDiscordBackend()
    assert DiscordProjectionRunner(
        session_factory, FakeDiscordProjectionAdapter(backend), settings
    ).run_due_once()

    message = next(iter(backend.notification_messages.values()))
    assert message["render"]["late"] is True


@pytest.mark.integration
def test_due_reminder_survives_lost_ack_without_duplicate_discord_message(
    session_factory,
) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("event-live", base + timedelta(minutes=5)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    message_id = "888888888888888888"
    with session_factory.begin() as session:
        rule_result = ReminderRuleService(session, settings).set(
            SetReminderRuleInput(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="event-live",
                lead_seconds=300,
                request_key=_request_key(message_id),
                source=_source(message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )
        rule_id = rule_result.rule_id

    dispatcher = ReminderDispatcher(
        session_factory, settings, clock=lambda: base + timedelta(seconds=2)
    )
    assert dispatcher.run_due_once()
    backend = FakeDiscordBackend()
    adapter = FakeDiscordProjectionAdapter(backend)
    adapter.discard_next_notification_ack = True
    runner = DiscordProjectionRunner(session_factory, adapter, settings)
    assert runner.run_due_once()
    assert len(backend.notification_messages) == 1

    with session_factory.begin() as session:
        outbox = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "discord.calendar_reminder.requested"
            )
        )
        assert outbox is not None and outbox.status == "pending"
        outbox.next_attempt_at = None
    restarted = DiscordProjectionRunner(
        session_factory, FakeDiscordProjectionAdapter(backend), settings
    )
    assert restarted.run_due_once()

    with session_factory() as session:
        notification = session.scalar(
            select(ScheduledNotification)
            .join(ReminderRule, ReminderRule.id == ScheduledNotification.reminder_rule_id)
            .where(ReminderRule.id == rule_id)
        )
        assert notification is not None and notification.status == "delivered"
        assert notification.attempt_count == 2
        assert notification.discord_message_id is not None
        assert len(backend.notification_messages) == 1


@pytest.mark.integration
def test_stale_sync_reports_one_durable_system_alert(session_factory) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    clock_value = [base]
    settings = get_settings().model_copy(
        update={"calendar_reads_enabled": True, "calendar_stale_seconds": 60}
    )
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("event", base + timedelta(hours=1)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock_value[0])
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    clock_value[0] += timedelta(seconds=61)
    assert sync.evaluate_staleness() == 1
    assert sync.evaluate_staleness() == 1
    with session_factory() as session:
        alerts = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "discord.system_alert.requested",
                OutboxEvent.aggregate_type == "calendar_sync_state",
            )
        ).all()
        state = session.scalar(select(CalendarSyncState))
        assert len(alerts) == 1
        assert state is not None and state.status == "stale"


@pytest.mark.integration
def test_all_day_reminder_uses_event_timezone_across_dst(session_factory) -> None:
    settings = get_settings()
    account_id = _account(session_factory)
    generation = uuid.uuid4()
    with session_factory.begin() as session:
        event = CalendarEventCache(
            account_id=account_id,
            calendar_id=settings.google_calendar_id,
            provider_event_id="all-day-dst",
            snapshot_generation=generation,
            status="confirmed",
            summary="DST day",
            is_all_day=True,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 2),
            timezone="America/Los_Angeles",
            synced_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        rule = ReminderRule(
            account_id=account_id,
            calendar_id=settings.google_calendar_id,
            scope="event",
            provider_event_id=event.provider_event_id,
            lead_seconds=3600,
            destination_channel_id=settings.effective_reminder_channel_id(),
            enabled=True,
            created_by_actor_id=settings.operator_discord_user_id,
        )
        session.add_all([event, rule])
        session.flush()
        assert (
            materialize_reminders(
                session,
                now=datetime(2026, 10, 1, tzinfo=UTC),
                settings=settings,
                rule_ids={rule.id},
            )
            == 1
        )
        rule_id = rule.id

    with session_factory() as session:
        notification = session.scalar(
            select(ScheduledNotification).where(ScheduledNotification.reminder_rule_id == rule_id)
        )
        assert notification is not None
        # Midnight on the fallback date is still PDT (UTC-07); one hour earlier is 06:00 UTC.
        assert notification.scheduled_for.replace(tzinfo=UTC) == datetime(
            2026, 11, 1, 6, tzinfo=UTC
        )
        assert notification.event_start_key == "2026-11-01@America/Los_Angeles"
