"""Owned marker rendering and cache classification survive provider refresh."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from test_task_policy_authority import admitted_import

from docket.config import get_settings
from docket.models import (
    CalendarEventCache,
    CalendarLane,
    ChangeSet,
    ReminderPlan,
    Task,
    TaskReminderGeneration,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.providers.google.calendar import CalendarSnapshotEvent
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.authority import ChangeSetCommit, ProviderIntentInput
from docket.services.calendar_sync import CalendarSyncService
from docket.services.changeset_delivery import ChangeSetDeliveryService
from docket.services.operations import OperationRunner
from docket.services.provider_intents import ProviderIntentService
from docket.services.task_reminder_origins import task_reminder_origins
from docket.services.workload_calendar import WorkloadCalendarService


def seed(session, start):
    service, prepared, effect, listing = admitted_import(session)
    changeset, _ = service.commit(ChangeSetCommit(
        changeset_ref=prepared.ref_id, expected_version=prepared.version,
        idempotency_key=prepared.idempotency_key, authority_policy_effect_ref=effect.ref_id,
    ))
    assert changeset.state == "committed", changeset.validation_errors
    task = session.scalar(select(Task))
    task.remind_at = start.astimezone(UTC)
    task.reminder_timezone = "America/Los_Angeles"
    task.reminder_generation = 1
    # The fixture's real service root represents a standing policy, not a
    # manufactured request for every Calendar observation.
    from docket.models import ProviderAccount

    account = session.get(ProviderAccount, listing.account_id)
    account.capabilities = ["google_tasks", "google_calendar"]
    lane = CalendarLane(
        account_id=account.id, lane="reminders", display_name="Validation",
        color_hex="#123456", calendar_id="validation-calendar", status="active",
        basis_refs=changeset.basis_refs, created_by_changeset_ref=changeset.ref_id,
    )
    time = TemporalBinding(
        subject_ref=task.ref_id, role="expected_at", binding_key="task-reminder:1",
        temporal_value={"kind": "datetime",
                        "local_datetime": start.replace(tzinfo=None).isoformat(),
                        "timezone": "America/Los_Angeles", "fold": 0},
        basis_refs=changeset.basis_refs, created_by_changeset_ref=changeset.ref_id,
    )
    session.add_all([lane, time])
    session.flush()
    plan = ReminderPlan(
        subject_ref=time.ref_id, delivery_channels=["google_popup"], lead_seconds=[0],
        basis_refs=changeset.basis_refs, created_by_changeset_ref=changeset.ref_id,
    )
    session.add(plan)
    session.flush()
    projection = TemporalCalendarProjection(
        temporal_binding_ref=time.ref_id, lane_ref=lane.ref_id, reminder_plan_ref=plan.ref_id,
        display_policy={"kind": "timed_marker", "duration_seconds": 60,
                        "transparency": "transparent"},
        basis_refs=changeset.basis_refs, created_by_changeset_ref=changeset.ref_id,
    )
    session.add(projection)
    session.flush()
    generation = TaskReminderGeneration(
        task_ref=task.ref_id, generation=1, task_version=task.version,
        temporal_binding_ref=time.ref_id, projection_ref=projection.ref_id, plan_ref=plan.ref_id,
        created_by_changeset_ref=changeset.ref_id,
    )
    session.add(generation)
    session.flush()
    intent = ProviderIntentInput(
        intent_id="reminder-fixture", operation_type="calendar_create_event",
        account_ref=account.ref_id, canonical_target_refs=[projection.ref_id, lane.ref_id],
        basis_refs=changeset.basis_refs, idempotency_key="reminder-fixture-create", parameters={},
    )
    ProviderIntentService(session).materialize(session, changeset, intent, {})
    assert task_reminder_origins(session, account.id, lane.calendar_id) == {}
    return account.id, task.ref_id, projection.ref_id


@pytest.mark.parametrize("start", [
    datetime.fromisoformat("2026-11-01T01:59:00-07:00"),
    datetime.fromisoformat("2026-03-08T01:59:00-08:00"),
])
@pytest.mark.parametrize("outcome", ["success", "unknown_after_write"])
def test_rendered_owned_marker_survives_cache_rebuild_and_does_not_consume_capacity(
    session_factory, monkeypatch, start, outcome,
):
    monkeypatch.setattr(get_settings(), "tasks_enabled", True)
    with session_factory.begin() as session:
        account_id, task_ref, projection_ref = seed(session, start)
    class RecordingCalendar(FakeCalendarProvider):
        def create_event(self, request):
            self.body = request.event_body()
            return super().create_event(request)

    provider = RecordingCalendar()
    provider.next_create_outcome = outcome
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once()
    delivered = next(iter(provider.events.values()))
    snapshot = delivered.snapshot
    assert snapshot["visibility"] == "private"
    assert snapshot["transparency"] == "transparent"
    assert snapshot["docket_origin_kind"] == "task_reminder"
    assert snapshot["reminders"] == {"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 0},
    ]}
    assert len(snapshot["summary"]) == 512
    first = datetime.fromisoformat(provider.body["start"]["dateTime"])
    last = datetime.fromisoformat(provider.body["end"]["dateTime"])
    assert last.astimezone(UTC) - first.astimezone(UTC) == timedelta(seconds=60)
    zone = ZoneInfo("America/Los_Angeles")
    assert first.astimezone(zone).utcoffset() != last.astimezone(zone).utcoffset()
    with session_factory() as session:
        task = session.scalar(select(Task).where(Task.ref_id == task_ref))
        changeset = session.scalar(select(ChangeSet).where(
            ChangeSet.ref_id == task.created_by_changeset_ref,
        ))
        delivery = ChangeSetDeliveryService(session).read(changeset)
        marker = next(row for row in delivery["items"] if row["target_ref"] == projection_ref)
        assert "snapshot_diagnostic" not in marker
        timing = marker["timing"]
        reported_first = datetime.fromisoformat(timing["start_local"])
        reported_last = datetime.fromisoformat(timing["end_local"])
        assert reported_first.astimezone(UTC) == first.astimezone(UTC)
        assert reported_last.astimezone(UTC) == last.astimezone(UTC)
        assert (reported_first.astimezone(zone).utcoffset()
                != reported_last.astimezone(zone).utcoffset())
    with session_factory.begin() as session:
        row = session.scalar(select(CalendarEventCache))
        if outcome == "success":
            assert row.origin_kind == "task_reminder"
            assert row.origin_owner_ref == task_ref and row.origin_projection_ref == projection_ref
        else:
            assert row is None
            assert delivered.external_event_id in task_reminder_origins(
                session, account_id, "validation-calendar",
            )
        assert len(session.scalar(select(Task.title))) == 1024
        # A complete rebuild loses all old cache fields, not just the worker's
        # in-memory state. The immutable generation and scoped effect remain.
        session.execute(delete(CalendarEventCache))
        session.scalar(select(TemporalCalendarProjection)).enabled = False
    observed = CalendarSnapshotEvent(
        provider_event_id=delivered.external_event_id, status="confirmed",
        summary=snapshot["summary"], location=None, is_all_day=False,
        start_at=first.astimezone(UTC), end_at=last.astimezone(UTC),
        timezone="America/Los_Angeles",
        provider_etag=delivered.provider_etag,
    )
    provider.put_snapshot_event(observed)
    # This ordinary event copies the label/timing. Those are never ownership.
    provider.put_snapshot_event(replace(observed, provider_event_id="unowned-copy"))
    settings = get_settings().model_copy(update={"calendar_reads_enabled": True})
    sync = CalendarSyncService(session_factory, provider, settings, clock=lambda: first)
    assert sync.run_due_once()
    with session_factory() as session:
        rows = {row.provider_event_id: row for row in session.scalars(select(CalendarEventCache))}
        assert rows[delivered.external_event_id].origin_kind == "task_reminder"
        assert rows["unowned-copy"].origin_kind is None
        assert task_reminder_origins(session, account_id, "another-calendar") == {}
        assert task_reminder_origins(session, uuid.uuid4(), "validation-calendar") == {}
        workload = WorkloadCalendarService(session, settings).capture(
            first.astimezone(UTC) - timedelta(hours=1), last.astimezone(UTC) + timedelta(hours=1),
            first.astimezone(UTC),
        )
        assert len(workload.rows) == 1
        assert workload.rows[0].title == snapshot["summary"]
        assert "unresolved_provider_snapshot_generation" not in workload.flags
