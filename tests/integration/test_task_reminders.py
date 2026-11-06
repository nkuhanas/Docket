"""Flat native reminders commit and retire through the shared Calendar queue."""

import uuid
from datetime import timedelta

import pytest
import test_tasks_operations
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    CalendarLane,
    GoogleTaskList,
    Item,
    Operation,
    ProviderAccount,
    Task,
    TaskListPolicyRevision,
    TaskReminderGeneration,
    TemporalCalendarProjection,
)
from docket.models.base import utc_now
from docket.services.task_sync import TaskSyncService

linked_plugin = test_tasks_operations.linked_plugin
tasks_client = test_tasks_operations.tasks_client


async def configure(call, factory):
    with factory.begin() as session:
        listing = session.scalar(select(GoogleTaskList))
        account = session.get(ProviderAccount, listing.account_id)
        account.capabilities = ["google_tasks", "google_calendar"]
        lane = CalendarLane(
            account_id=account.id, lane="reminders", display_name="Reminder validation",
            color_hex="#123456", calendar_id="validation-calendar", status="active",
            created_by_changeset_ref=session.get(
                TaskListPolicyRevision, listing.policy_ref,
            ).created_by_changeset_ref,
        )
        session.add(lane)
        session.flush()
        list_ref, lane_ref = listing.ref_id, lane.ref_id
    packet = await call("docket_get_tasks_context", {"refs": [list_ref, lane_ref]})
    result = await call("docket_configure_task_list", {
        "context_id": packet["context_id"], "operation_id": "configure-reminder-calendar",
        "task_list_ref": list_ref, "mode": "two_way", "reminder_calendar_ref": lane_ref,
    })
    assert result.get("disposition") == "committed", result
    return (await call("docket_get_tasks_context", {}))["context_id"]


async def edit(call, ref, operation_id, **fields):
    context = await call("docket_get_tasks_context", {"refs": [ref]})
    result = await call("docket_update_task", {
        "context_id": context["context_id"], "operation_id": operation_id,
        "task_ref": ref, **fields,
    })
    assert result.get("disposition") == "committed", result
    return result


def drain(runner):
    for _ in range(10):
        if not runner.run_due_once():
            return
    pytest.fail("The bounded synthetic queue did not drain")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_flat_reminder_reschedules_and_completes_without_changing_scheduled_day(
    session_factory, tasks_client, monkeypatch,
):
    async with tasks_client() as (call, runner, provider, _packet):
        monkeypatch.setattr(get_settings(), "tasks_enabled", True)
        monkeypatch.setattr(
            get_settings(), "plugin_oauth_issuer", runner.settings.plugin_oauth_issuer,
        )
        context_id = await configure(call, session_factory)
        result = await call("docket_create_task", {
            "context_id": context_id, "operation_id": "create-with-reminder",
            "title": "Task with an exact clock",
            "scheduled_date": "2026-11-13", "remind_at": "2026-11-13T17:00:00-08:00",
            "timezone": "America/Los_Angeles",
        })
        assert result.get("disposition") == "committed", result
        assert runner.provider.events == {} and provider.tasks == {}
        with session_factory() as session:
            task = session.scalar(select(Task))
            ref = task.ref_id
            assert task.reminder_generation == 1 and task.remind_at is not None
            assert set(session.scalars(select(Operation.operation_type))) == {
                "calendar_create_event", "tasks_insert",
            }
        drain(runner)
        first = next(iter(runner.provider.events.values()))
        assert first.snapshot["visibility"] == "private"
        assert first.snapshot["transparency"] == "transparent"
        assert first.snapshot["reminders"]["overrides"] == [{"method": "popup", "minutes": 0}]
        assert next(iter(provider.tasks.values()))["title"] == (
            "5:00 PM — Task with an exact clock"
        )
        with session_factory() as session:
            assert session.scalar(select(Task.title)) == "Task with an exact clock"
        await edit(call, ref, "edit-independent-notes", notes="Unrelated edit")
        with session_factory() as session:
            assert len(list(session.scalars(select(TaskReminderGeneration)))) == 1
        drain(runner)
        remote = provider.list_tasks("synthetic-list").tasks[0]
        renamed = provider.patch_task("synthetic-list", remote.task_id,
                                      {"title": "5:00 PM — Renamed in Google"}, etag=remote.etag)
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            sync = TaskSyncService(session, runner.settings)
            observation = sync.observe(
                listing, renamed, generation=uuid.uuid4(), read_started_at=utc_now(),
            )
            sync.reconcile(observation)
            assert session.scalar(select(Task.title)) == "Renamed in Google"
            assert session.scalar(select(Item.title)) == "Renamed in Google"
        drain(runner)
        await edit(call, ref, "reschedule-reminder", remind_at="2026-11-14T17:00:00-08:00")
        drain(runner)
        with session_factory() as session:
            task = session.scalar(select(Task))
            assert task.scheduled_date.isoformat() == "2026-11-13"
            assert task.reminder_generation == 3
            assert len(list(session.scalars(select(TaskReminderGeneration)))) == 3
        assert sum(e.snapshot.get("status") != "cancelled"
                   for e in runner.provider.events.values()) == 1
        assert next(iter(provider.tasks.values()))["title"] == (
            "2026-11-14 5:00 PM — Renamed in Google"
        )
        remote = provider.list_tasks("synthetic-list").tasks[0]
        completed = provider.patch_task("synthetic-list", remote.task_id,
                                        {"status": "completed",
                                         "completed": "2026-11-14T20:00:00Z"}, etag=remote.etag)
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            sync = TaskSyncService(session, runner.settings)
            sync.reconcile(sync.observe(
                listing, completed, generation=uuid.uuid4(), read_started_at=utc_now(),
            ))
        drain(runner)
        with session_factory() as session:
            assert session.scalar(select(Task.remind_at)) is None
            assert not any(session.scalars(select(TemporalCalendarProjection.enabled)))
            assert set(session.scalars(select(Operation.status))) <= {"succeeded", "superseded"}
        assert all(e.snapshot["status"] == "cancelled" for e in runner.provider.events.values())
        assert next(iter(provider.tasks.values()))["title"] == "Renamed in Google"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("window", ["pending", "claimed", "unknown"])
async def test_completion_cannot_resurrect_an_older_reminder_creation(
    session_factory, tasks_client, window,
):
    async with tasks_client() as (call, runner, _provider, _packet):
        context_id = await configure(call, session_factory)
        result = await call("docket_create_task", {
            "context_id": context_id, "operation_id": "create-retired-reminder",
            "title": "Reminder with delayed delivery", "remind_at": "2026-11-13T17:00:00-08:00",
        })
        assert result.get("disposition") == "committed", result
        with session_factory() as session:
            task_ref = session.scalar(select(Task.ref_id))
        claim = runner.claim_due() if window == "claimed" else None
        if window == "unknown":
            runner.provider.next_create_outcome = "unknown_after_write"
            assert runner.run_due_once()
        await edit(call, task_ref, "complete-before-delivery", state="completed")
        if claim is not None:
            runner._execute(claim)
        if window == "unknown":
            saved = dict(runner.provider.events)
            runner.provider.events.clear()
            with session_factory.begin() as session:
                op = session.scalar(select(Operation).where(
                    Operation.operation_type == "calendar_create_event",
                ))
                op.next_attempt_at = utc_now() - timedelta(seconds=1)
            assert runner.reconcile_once()
            with session_factory.begin() as session:
                op = session.scalar(select(Operation).where(
                    Operation.operation_type == "calendar_create_event",
                ))
                assert op.status == "reconciliation_required"
                op.next_attempt_at = utc_now() - timedelta(seconds=1)
            runner.provider.events.update(saved)
            assert runner.reconcile_once()
        drain(runner)
        assert all(e.snapshot.get("status") == "cancelled"
                   for e in runner.provider.events.values())
        with session_factory() as session:
            assert not any(session.scalars(select(TemporalCalendarProjection.enabled)))
            assert set(session.scalars(select(Operation.status))) <= {"succeeded", "superseded"}
