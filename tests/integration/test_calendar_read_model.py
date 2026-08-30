from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CalendarEventCache,
    CalendarLane,
    CalendarSyncState,
    CanonicalEvent,
    Item,
    ProviderAccount,
    ProviderEventBinding,
    ReminderPlan,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.providers.google.calendar import CalendarSnapshotEvent
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.calendar_sync import CalendarReadService, CalendarSyncService


def _account_and_lane(
    session_factory: sessionmaker[Session],
    *,
    calendar_id: str,
    lane: str = "personal",
) -> tuple[uuid.UUID, str, str]:
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id=f"calendar-read-{calendar_id}",
            display_name="Calendar read fixture",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        lane_row = CalendarLane(
            account_id=account.id,
            lane=lane,
            display_name=lane.title(),
            color_hex="#8E24AA",
            calendar_id=calendar_id,
            status="active",
            basis_refs=[new_public_ref("dec")],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(lane_row)
        session.flush()
        return account.id, account.ref_id, lane_row.ref_id


def _timed(event_id: str, start: datetime, *, summary: str) -> CalendarSnapshotEvent:
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


@pytest.mark.integration
def test_calendar_snapshot_failure_preserves_prior_generation(
    session_factory: sessionmaker[Session],
) -> None:
    base = datetime(2026, 8, 30, 16, tzinfo=UTC)
    clock = [base]
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    account_id, _account_ref, _lane_ref = _account_and_lane(
        session_factory, calendar_id=settings.google_calendar_id
    )
    provider = FakeCalendarProvider()
    provider.snapshot_page_size = 1
    provider.put_snapshot_event(_timed("event-a", base + timedelta(days=1), summary="A1"))
    provider.put_snapshot_event(_timed("event-b", base + timedelta(days=2), summary="B1"))
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: clock[0])

    assert sync.sync_target(account_id, settings.google_calendar_id, force=True) is True
    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None
        first_generation = state.snapshot_generation
        assert [
            (row.provider_event_id, row.summary)
            for row in session.scalars(
                select(CalendarEventCache).order_by(CalendarEventCache.provider_event_id)
            )
        ] == [("event-a", "A1"), ("event-b", "B1")]

    provider.put_snapshot_event(
        replace(provider.snapshot_events["event-a"], summary="A2")
    )
    provider.remove_snapshot_event("event-b")
    provider.put_snapshot_event(_timed("event-c", base + timedelta(days=3), summary="C1"))
    provider.fail_snapshot_page = 1
    clock[0] += timedelta(days=31)

    assert sync.sync_target(account_id, settings.google_calendar_id, force=True) is True
    with session_factory() as session:
        state = session.scalar(select(CalendarSyncState))
        assert state is not None
        assert state.status == "stale"
        assert state.snapshot_generation == first_generation
        assert state.last_error_code == "fake_snapshot_failure"
        assert [
            (row.provider_event_id, row.summary)
            for row in session.scalars(
                select(CalendarEventCache).order_by(CalendarEventCache.provider_event_id)
            )
        ] == [("event-a", "A1"), ("event-b", "B1")]


@pytest.mark.integration
def test_calendar_series_read_resolves_clean_binding_and_reminder(
    session_factory: sessionmaker[Session],
) -> None:
    base = datetime(2026, 8, 30, 16, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    calendar_id = "personal@example.com"
    account_id, account_ref, lane_ref = _account_and_lane(
        session_factory, calendar_id=calendar_id
    )
    basis_ref = new_public_ref("utt")
    with session_factory.begin() as session:
        lane = session.scalar(select(CalendarLane).where(CalendarLane.ref_id == lane_ref))
        assert lane is not None
        event = CanonicalEvent(
            canonical_key="calendar-read-series",
            title="MATH 1263",
            status="active",
            event_spec={
                "title": "MATH 1263",
                "timing": {
                    "kind": "timed",
                    "start_local": "2026-08-30T09:00:00",
                    "end_local": "2026-08-30T10:00:00",
                    "timezone": settings.timezone,
                },
            },
            authority="explicit_operator",
            lane_id=lane.id,
            lane_ref=lane.ref_id,
            basis_refs=[basis_ref],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                ProviderEventBinding(
                    canonical_target_ref=event.ref_id,
                    target_kind="event",
                    account_id=account_id,
                    calendar_id=calendar_id,
                    provider_event_id="series-1",
                    provider_etag='"series-1"',
                    status="active",
                ),
                ReminderPlan(
                    subject_ref=event.ref_id,
                    delivery_channels=["google_popup", "docket_queue"],
                    lead_seconds=[600],
                    basis_refs=[basis_ref],
                    created_by_changeset_ref=new_public_ref("chg"),
                ),
                *[
                    CalendarEventCache(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        provider_event_id=f"occurrence-{index}",
                        recurring_event_id="series-1",
                        snapshot_generation=uuid.uuid4(),
                        status="confirmed",
                        summary="MATH 1263",
                        is_all_day=False,
                        start_at=base + timedelta(days=index),
                        end_at=base + timedelta(days=index, hours=1),
                        timezone=settings.timezone,
                        synced_at=base,
                    )
                    for index in (1, 2)
                ],
            ]
        )
        event_ref = event.ref_id

    read = CalendarReadService(
        session_factory,
        CalendarSyncService(session_factory, FakeCalendarProvider(), settings, clock=lambda: base),
        settings,
        clock=lambda: base,
    )
    result = read.list_events(
        account_id=account_id,
        calendar_id=calendar_id,
        start=base,
        end=base + timedelta(days=4),
        text_filter=None,
        limit=25,
        freshness="prefer_cache",
        result_view="series",
    )

    assert result["account_ref"] == account_ref
    assert result["count"] == 1
    projected = result["events"][0]
    assert projected["provider_event_id"] == "series-1"
    assert projected["ref"] == event_ref
    assert projected["lane_ref"] == lane_ref
    assert projected["target_kind"] == "event"
    assert projected["scope"] == "series"
    assert projected["occurrences_in_range"] == 2
    assert projected["reminder_plan"]["state"] == "canonical"
    assert projected["reminder_plan"]["delivery_channels"] == [
        "google_popup",
        "docket_queue",
    ]
    assert projected["reminder_plan"]["lead_seconds"] == [600]


@pytest.mark.integration
def test_calendar_read_exposes_time_marker_type_and_role(
    session_factory: sessionmaker[Session],
) -> None:
    base = datetime(2026, 8, 30, 16, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    calendar_id = "academics@example.com"
    account_id, _account_ref, lane_ref = _account_and_lane(
        session_factory, calendar_id=calendar_id, lane="academics"
    )
    with session_factory.begin() as session:
        item = Item(
            title="MATH 1263 Midterm",
            kind="academic.exam",
            basis_refs=[new_public_ref("utt")],
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(item)
        session.flush()
        temporal = TemporalBinding(
            subject_ref=item.ref_id,
            role="scheduled_on",
            temporal_value={
                "kind": "date",
                "date": "2026-09-18",
                "timezone": settings.timezone,
            },
            basis_refs=list(item.basis_refs),
            decision_refs=[],
            source_refs=[],
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(temporal)
        session.flush()
        projection = TemporalCalendarProjection(
            temporal_binding_ref=temporal.ref_id,
            lane_ref=lane_ref,
            display_policy={
                "kind": "all_day_marker",
                "transparency": "transparent",
            },
            basis_refs=list(item.basis_refs),
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(projection)
        session.flush()
        session.add_all(
            [
                ProviderEventBinding(
                    canonical_target_ref=projection.ref_id,
                    target_kind="temporal_projection",
                    account_id=account_id,
                    calendar_id=calendar_id,
                    provider_event_id="midterm-marker",
                    status="active",
                ),
                CalendarEventCache(
                    account_id=account_id,
                    calendar_id=calendar_id,
                    provider_event_id="midterm-marker",
                    snapshot_generation=uuid.uuid4(),
                    status="confirmed",
                    summary=item.title,
                    is_all_day=True,
                    start_date=datetime(2026, 9, 18).date(),
                    end_date=datetime(2026, 9, 19).date(),
                    timezone=settings.timezone,
                    synced_at=base,
                ),
            ]
        )
        temporal_ref = temporal.ref_id
        projection_ref = projection.ref_id

    read = CalendarReadService(
        session_factory,
        CalendarSyncService(
            session_factory,
            FakeCalendarProvider(),
            settings,
            clock=lambda: base,
        ),
        settings,
        clock=lambda: base,
    )
    result = read.list_events(
        account_id=account_id,
        calendar_id=calendar_id,
        start=datetime(2026, 9, 17, tzinfo=UTC),
        end=datetime(2026, 9, 20, tzinfo=UTC),
        text_filter=None,
        limit=25,
        freshness="prefer_cache",
    )

    assert result["count"] == 1
    marker = result["events"][0]
    assert marker["ref"] == temporal_ref
    assert marker["projection_ref"] == projection_ref
    assert marker["object_type"] == "temporal_binding"
    assert marker["semantic_role"] == "scheduled_on"


@pytest.mark.integration
def test_calendar_aggregate_page_is_globally_ordered(
    session_factory: sessionmaker[Session],
) -> None:
    base = datetime(2026, 8, 30, 16, tzinfo=UTC)
    settings = get_settings().model_copy(update={"calendar_reads_enabled": False})
    account_id, _account_ref, _lane_ref = _account_and_lane(
        session_factory, calendar_id="personal@example.com"
    )
    with session_factory.begin() as session:
        session.add(
            CalendarLane(
                account_id=account_id,
                lane="clubs",
                display_name="Clubs",
                color_hex="#0B8043",
                calendar_id="clubs@example.com",
                status="active",
                basis_refs=[new_public_ref("dec")],
                created_by_changeset_ref=new_public_ref("chg"),
            )
        )
        session.add_all(
            [
                CalendarEventCache(
                    account_id=account_id,
                    calendar_id="personal@example.com",
                    provider_event_id="later",
                    snapshot_generation=uuid.uuid4(),
                    status="confirmed",
                    summary="Later",
                    is_all_day=False,
                    start_at=base + timedelta(hours=2),
                    end_at=base + timedelta(hours=3),
                    synced_at=base,
                ),
                CalendarEventCache(
                    account_id=account_id,
                    calendar_id="clubs@example.com",
                    provider_event_id="earlier",
                    snapshot_generation=uuid.uuid4(),
                    status="confirmed",
                    summary="Earlier",
                    is_all_day=False,
                    start_at=base + timedelta(hours=1),
                    end_at=base + timedelta(hours=2),
                    synced_at=base,
                ),
            ]
        )

    read = CalendarReadService(
        session_factory,
        CalendarSyncService(session_factory, FakeCalendarProvider(), settings, clock=lambda: base),
        settings,
        clock=lambda: base,
    )
    first = read.list_events_across_calendars(
        account_id=account_id,
        calendar_ids=["personal@example.com", "clubs@example.com"],
        start=base,
        end=base + timedelta(days=1),
        text_filter=None,
        limit=1,
        freshness="prefer_cache",
    )
    second = read.list_events_across_calendars(
        account_id=account_id,
        calendar_ids=["personal@example.com", "clubs@example.com"],
        start=base,
        end=base + timedelta(days=1),
        text_filter=None,
        limit=1,
        freshness="prefer_cache",
        offset=1,
    )

    assert first["events"][0]["provider_event_id"] == "earlier"
    assert first["truncated"] is True
    assert first["cursor"] == "1"
    assert second["events"][0]["provider_event_id"] == "later"
    assert second["truncated"] is False
