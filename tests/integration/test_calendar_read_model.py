import uuid
from datetime import UTC, date, datetime, timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import VersionConflict
from docket.models import (
    Account,
    AuditEvent,
    CalendarEventCache,
    CalendarLink,
    CalendarSyncState,
    DiscordDailyThread,
    OutboxEvent,
    ReminderRule,
    ScheduledNotification,
)
from docket.providers.discord import (
    DiscordProjectionError,
    FakeDiscordBackend,
    FakeDiscordProjectionAdapter,
)
from docket.providers.google.calendar import CalendarSnapshotEvent, CalendarSnapshotPage
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.calendar import (
    CalendarLookupInput,
    DisableReminderRuleInput,
    SetReminderRuleInput,
)
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
def test_calendar_lookup_reports_tags_priority_and_dual_projection_reminder_state(
    session_factory,
) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(
        CalendarSnapshotEvent(
            provider_event_id="classified-event",
            status="confirmed",
            summary="Classified event",
            location="Desk",
            is_all_day=False,
            start_at=base + timedelta(hours=2),
            end_at=base + timedelta(hours=3),
            timezone="America/Los_Angeles",
            provider_reminders={
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 10}],
            },
            provider_etag='"classified"',
        )
    )
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    def lookup() -> dict:
        return read.list_events(
            account_id=account_id,
            calendar_id=settings.google_calendar_id,
            start=base,
            end=base + timedelta(days=1),
            text_filter=None,
            limit=100,
            freshness="prefer_cache",
        )["events"][0]

    external = lookup()
    assert external["recurrence_kind"] == "one_time"
    assert external["system_tags"] == ["one_time", "timed", "external"]
    assert external["operator_tags"] == []
    assert external["priority"] == "normal"
    assert external["priority_basis"] == "default"
    assert external["reminder_plan"]["state"] == "external_unmanaged"

    plan = {
        "delivery_channels": ["google_popup", "docket_queue"],
        "lead_seconds": [600],
    }
    with session_factory.begin() as session:
        session.add(
            CalendarLink(
                record_id=None,
                meeting_id=None,
                origin_kind="standalone",
                logical_key="standalone:classified",
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                external_event_id="classified-event",
                provider_etag='"classified"',
                provider_correlation="classified-correlation",
                last_synced_version=1,
                recurrence_kind="one_time",
                system_tags=["one_time", "timed", "standalone"],
                operator_tags=["focused"],
                priority="high",
                priority_basis="explicit_operator",
                reminder_plan_sha256=sha256_json(plan),
                synced_snapshot={},
            )
        )
        session.add(
            ReminderRule(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="classified-event",
                lead_seconds=600,
                queue_channel_id=settings.queue_channel_id,
                source_kind="canonical_plan",
                enabled=True,
                created_by_actor_id=settings.operator_discord_user_id,
            )
        )
        cached = session.scalar(select(CalendarEventCache))
        assert cached is not None
        cached.system_tags = ["one_time", "timed", "standalone"]
        cached.operator_tags = ["focused"]
        cached.priority = "high"
        cached.priority_basis = "explicit_operator"

    synchronized = lookup()
    assert synchronized["system_tags"] == ["one_time", "timed", "standalone"]
    assert synchronized["operator_tags"] == ["focused"]
    assert synchronized["priority"] == "high"
    assert synchronized["priority_basis"] == "explicit_operator"
    assert synchronized["reminder_plan"] == {
        "state": "synchronized",
        "canonical_lead_seconds": [600],
        "delivery_channels": ["google_popup", "docket_queue"],
        "provider_use_default": False,
        "provider_popup_lead_seconds": [600],
    }

    with session_factory.begin() as session:
        cached = session.scalar(select(CalendarEventCache))
        assert cached is not None
        cached.provider_reminders = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 5}],
        }
    assert lookup()["reminder_plan"]["state"] == "drifted"


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
def test_failed_account_does_not_starve_another_calendar_sync_target(session_factory) -> None:
    base = datetime(2026, 7, 22, 14, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    with session_factory.begin() as session:
        accounts = [
            Account(
                provider="google",
                external_account_id=f"account-{suffix}",
                capabilities=["calendar_read"],
                enabled=True,
            )
            for suffix in ("a", "b")
        ]
        session.add_all(accounts)
        session.flush()
        account_ids = sorted((account.id for account in accounts), key=str)

    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("shared-event", base + timedelta(hours=1)))
    provider.fail_snapshot_page = 0
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)

    assert sync.run_due_once()
    provider.fail_snapshot_page = None
    assert sync.run_due_once()
    with session_factory() as session:
        states = {
            state.account_id: state.status
            for state in session.scalars(select(CalendarSyncState)).all()
        }
        assert states == {account_ids[0]: "failed", account_ids[1]: "current"}

    assert sync.run_due_once()
    with session_factory() as session:
        states = session.scalars(select(CalendarSyncState)).all()
        assert {state.account_id for state in states} == set(account_ids)
        assert all(state.status == "current" for state in states)


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
    assert result["range_resolution"]["mode"] == "explicit"
    assert result["events"][0]["start_at"] == "2026-07-22T16:00:00+00:00"
    assert result["events"][0]["end_at"] == "2026-07-22T17:00:00+00:00"
    assert result["events"][0]["start_local"] == "2026-07-22T09:00:00-07:00"
    assert result["events"][0]["end_local"] == "2026-07-22T10:00:00-07:00"
    assert result["events"][0]["local_timezone"] == "America/Los_Angeles"
    assert result["events"][1]["start_local"] is None
    assert result["events"][1]["end_local"] is None
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
def test_local_event_timestamps_use_the_correct_dst_fold(session_factory) -> None:
    base = datetime(2026, 11, 1, 7, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("fold", datetime(2026, 11, 1, 9, 30, tzinfo=UTC)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    result = read.list_events(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        start=datetime(2026, 11, 1, 7, tzinfo=UTC),
        end=datetime(2026, 11, 2, 8, tzinfo=UTC),
        text_filter=None,
        limit=100,
        freshness="prefer_cache",
    )

    assert result["events"][0]["start_local"] == "2026-11-01T01:30:00-08:00"
    assert result["events"][0]["end_local"] == "2026-11-01T02:30:00-08:00"


@pytest.mark.integration
def test_lookup_without_range_preserves_rolling_seven_day_default(session_factory) -> None:
    base = datetime(2026, 7, 22, 22, 15, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: base)

    result = read.list_events(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        start=None,
        end=None,
        text_filter=None,
        limit=100,
        freshness="prefer_cache",
    )

    assert result["range_start"] == base.isoformat()
    assert result["range_end"] == (base + timedelta(days=7)).isoformat()
    assert result["range_resolution"] == {
        "mode": "default",
        "relative_day": None,
        "local_date": None,
        "timezone": "America/Los_Angeles",
        "as_of": base.isoformat(),
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("as_of", "relative_day", "expected_date", "expected_start", "expected_end"),
    [
        (
            datetime(2026, 3, 8, 7, 59, 59, tzinfo=UTC),
            "today",
            "2026-03-07",
            "2026-03-07T08:00:00+00:00",
            "2026-03-08T08:00:00+00:00",
        ),
        (
            datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
            "today",
            "2026-03-08",
            "2026-03-08T08:00:00+00:00",
            "2026-03-09T07:00:00+00:00",
        ),
        (
            datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            "today",
            "2026-11-01",
            "2026-11-01T07:00:00+00:00",
            "2026-11-02T08:00:00+00:00",
        ),
        (
            datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            "tomorrow",
            "2026-11-02",
            "2026-11-02T08:00:00+00:00",
            "2026-11-03T08:00:00+00:00",
        ),
    ],
)
def test_relative_day_lookup_resolves_local_midnights_across_dst(
    session_factory,
    as_of: datetime,
    relative_day: str,
    expected_date: str,
    expected_start: str,
    expected_end: str,
) -> None:
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: as_of)
    read = CalendarReadService(session_factory, sync, settings, clock=lambda: as_of)

    result = read.list_events(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        start=None,
        end=None,
        relative_day=relative_day,
        text_filter=None,
        limit=100,
        freshness="prefer_cache",
    )

    assert result["range_start"] == expected_start
    assert result["range_end"] == expected_end
    assert result["range_resolution"] == {
        "mode": "relative_day",
        "relative_day": relative_day,
        "local_date": expected_date,
        "timezone": "America/Los_Angeles",
        "as_of": as_of.isoformat(),
    }


@pytest.mark.integration
def test_relative_day_lookup_captures_request_clock_once_before_midnight(
    session_factory,
) -> None:
    before_midnight = datetime(2026, 7, 22, 6, 59, 59, 999999, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: before_midnight)
    calls = 0

    def request_clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("Calendar lookup read the request clock more than once")
        return before_midnight

    read = CalendarReadService(session_factory, sync, settings, clock=request_clock)
    result = read.list_events(
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        start=None,
        end=None,
        relative_day="tomorrow",
        text_filter=None,
        limit=100,
        freshness="prefer_cache",
    )

    assert calls == 1
    assert result["range_resolution"]["local_date"] == "2026-07-22"
    assert result["range_start"] == "2026-07-22T07:00:00+00:00"
    assert result["range_end"] == "2026-07-23T07:00:00+00:00"


def test_calendar_lookup_rejects_mixed_or_partial_ranges() -> None:
    common = {
        "account_id": uuid.uuid4(),
        "calendar_id": "calendar@example.com",
    }
    with pytest.raises(ValueError, match="cannot be combined"):
        CalendarLookupInput(
            **common,
            relative_day="today",
            start=datetime(2026, 7, 22, tzinfo=UTC),
            end=datetime(2026, 7, 23, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="must be supplied together"):
        CalendarLookupInput(
            **common,
            start=datetime(2026, 7, 22, tzinfo=UTC),
        )


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
        listed = ReminderRuleService(session, settings).list(
            account_id=account_id,
            calendar_id=settings.google_calendar_id,
            enabled=False,
            limit=100,
        )
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
        assert listed == [
            {
                "rule_id": str(created.rule_id),
                "account_id": str(account_id),
                "calendar_id": settings.google_calendar_id,
                "scope": "event",
                "provider_event_id": "rule-event",
                "lead_seconds": 600,
                "enabled": False,
                "version": 3,
            }
        ]
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
        daily_thread = session.get(DiscordDailyThread, notification.daily_thread_id)
        assert daily_thread is not None
        assert daily_thread.channel_id == settings.queue_channel_id
        assert daily_thread.thread_id == next(iter(backend.notification_messages.values()))[
            "thread_id"
        ]
        assert notification.attempt_count == 2
        assert notification.discord_message_id is not None
        assert len(backend.notification_messages) == 1


@pytest.mark.integration
def test_reminder_crossing_midnight_uses_due_date_thread_and_unarchives(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    monkeypatch.setattr(
        "docket.services.reminders.utc_now",
        lambda: datetime(2026, 7, 23, 6, 50, tzinfo=UTC),
    )
    account_id = _account(session_factory)
    event_start = datetime(2026, 7, 23, 7, 3, tzinfo=UTC)  # 00:03 PDT
    generation = uuid.uuid4()
    with session_factory.begin() as session:
        event = CalendarEventCache(
            account_id=account_id,
            calendar_id=settings.google_calendar_id,
            provider_event_id="midnight-event",
            snapshot_generation=generation,
            status="confirmed",
            summary="Cross-midnight reminder",
            is_all_day=False,
            start_at=event_start,
            end_at=event_start + timedelta(minutes=15),
            timezone="America/Los_Angeles",
            synced_at=datetime(2026, 7, 22, 6, 45, tzinfo=UTC),
        )
        session.add(event)
    message_id = "777777777777777777"
    with session_factory.begin() as session:
        result = ReminderRuleService(session, settings).set(
            SetReminderRuleInput(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="midnight-event",
                lead_seconds=300,
                request_key=_request_key(message_id),
                source=_source(message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )

    assert ReminderDispatcher(
        session_factory,
        settings,
        clock=lambda: datetime(2026, 7, 23, 6, 58, 1, tzinfo=UTC),
    ).run_due_once()
    backend = FakeDiscordBackend()
    thread_key = (
        settings.discord_guild_id,
        settings.queue_channel_id,
        "2026-07-22",
    )
    backend.threads[thread_key] = {
        "thread_id": backend.snowflake(),
        "name": "2026-07-22 — Wednesday",
        "archived": True,
        "auto_archive_minutes": 10080,
    }
    assert DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    ).run_due_once()

    with session_factory() as session:
        notification = session.scalar(
            select(ScheduledNotification).where(
                ScheduledNotification.reminder_rule_id == result.rule_id
            )
        )
        assert notification is not None and notification.status == "delivered"
        daily_thread = session.get(DiscordDailyThread, notification.daily_thread_id)
        assert daily_thread is not None
        assert daily_thread.local_date == date(2026, 7, 22)
        assert daily_thread.channel_id == settings.queue_channel_id
        assert backend.threads[thread_key]["archived"] is False
        outbox = session.get(OutboxEvent, notification.outbox_event_id)
        assert outbox is not None
        assert outbox.payload["target_local_date"] == "2026-07-22"
        assert outbox.payload["thread_id"] == daily_thread.thread_id


@pytest.mark.integration
def test_exhausted_reminder_delivery_fails_and_emits_one_system_alert(
    session_factory,
) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    settings = get_settings().model_copy(
        update={"calendar_reads_enabled": True, "discord_projection_max_attempts": 2}
    )
    account_id = _account(session_factory)
    provider = FakeCalendarProvider()
    provider.put_snapshot_event(_timed("failed-delivery", base + timedelta(minutes=5)))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: base)
    assert sync.sync_target(account_id, settings.google_calendar_id, force=True)

    message_id = "121212121212121212"
    with session_factory.begin() as session:
        result = ReminderRuleService(session, settings).set(
            SetReminderRuleInput(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                scope="event",
                provider_event_id="failed-delivery",
                lead_seconds=300,
                request_key=_request_key(message_id),
                source=_source(message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )
        rule_id = result.rule_id
    assert ReminderDispatcher(
        session_factory, settings, clock=lambda: base + timedelta(seconds=1)
    ).run_due_once()

    class FailingAdapter(FakeDiscordProjectionAdapter):
        def post_calendar_reminder(self, payload):
            raise DiscordProjectionError("injected_delivery_failure", "Injected failure")

    runner = DiscordProjectionRunner(session_factory, FailingAdapter(), settings)
    assert runner.run_due_once()
    with session_factory.begin() as session:
        outbox = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "discord.calendar_reminder.requested"
            )
        )
        assert outbox is not None and outbox.status == "pending"
        outbox.next_attempt_at = None
    assert runner.run_due_once()

    with session_factory() as session:
        notification = session.scalar(
            select(ScheduledNotification)
            .join(ReminderRule, ReminderRule.id == ScheduledNotification.reminder_rule_id)
            .where(ReminderRule.id == rule_id)
        )
        alerts = session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "discord.system_alert.requested")
        ).all()
        assert notification is not None and notification.status == "failed"
        assert notification.attempt_count == 2
        assert notification.last_error_code == "injected_delivery_failure"
        assert len(alerts) == 1
        assert alerts[0].payload["title"] == "Docket Calendar reminder delivery failure"


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
            queue_channel_id=settings.queue_channel_id,
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
