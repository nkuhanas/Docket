"""Flat native reminders commit and retire through the shared Calendar queue."""

import uuid
from dataclasses import replace
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
    ProviderEventBinding,
    Task,
    TaskConflictResolution,
    TaskListPolicyRevision,
    TaskReminderGeneration,
    TemporalCalendarProjection,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import CalendarProviderError, CalendarUnknownOutcome
from docket.services.task_provider_intents import TaskProviderIntentService
from docket.services.task_sync import TaskSyncService

linked_plugin = test_tasks_operations.linked_plugin
tasks_client = test_tasks_operations.tasks_client


async def configure(call, factory):
    with factory.begin() as session:
        listing = session.scalar(select(GoogleTaskList))
        account = session.get(ProviderAccount, listing.account_id)
        account.capabilities = ["google_tasks", "google_calendar"]
        lane = CalendarLane(
            account_id=account.id,
            lane="reminders",
            display_name="Reminder validation",
            color_hex="#123456",
            calendar_id="validation-calendar",
            status="active",
            created_by_changeset_ref=session.get(
                TaskListPolicyRevision,
                listing.policy_ref,
            ).created_by_changeset_ref,
        )
        session.add(lane)
        session.flush()
        list_ref, lane_ref = listing.ref_id, lane.ref_id
    packet = await call("docket_get_tasks_context", {"refs": [list_ref, lane_ref]})
    result = await call(
        "docket_configure_task_list",
        {
            "context_id": packet["context_id"],
            "operation_id": "configure-reminder-calendar",
            "task_list_ref": list_ref,
            "mode": "two_way",
            "reminder_calendar_ref": lane_ref,
        },
    )
    assert result.get("disposition") == "committed", result
    return (await call("docket_get_tasks_context", {}))["context_id"]


async def edit(call, ref, operation_id, **fields):
    context = await call("docket_get_tasks_context", {"refs": [ref]})
    result = await call(
        "docket_update_task",
        {
            "context_id": context["context_id"],
            "operation_id": operation_id,
            "task_ref": ref,
            **fields,
        },
    )
    assert result.get("disposition") == "committed", result
    return result


def drain(runner):
    for _ in range(10):
        if not runner.run_due_once():
            return
    pytest.fail("The bounded synthetic queue did not drain")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("provider_changed", ["none", "before_read", "after_read"])
async def test_unchanged_retired_marker_cancellation_recovers_original_intent(
    session_factory, tasks_client, monkeypatch, provider_changed,
):
    async with tasks_client() as (call, runner, _provider, _packet):
        monkeypatch.setattr(get_settings(), "tasks_enabled", True)
        monkeypatch.setattr(get_settings(), "plugin_oauth_issuer",
                            runner.settings.plugin_oauth_issuer)
        context = await configure(call, session_factory)
        created = await call("docket_create_task", {
            "context_id": context, "operation_id": "cancel-recovery-create",
            "title": "Cancellation recovery", "remind_at": "2026-11-13T17:00:00-08:00",
            "timezone": "America/Los_Angeles",
        })
        assert created.get("disposition") == "committed", created
        drain(runner)
        with session_factory() as session:
            task_ref = session.scalar(select(Task.ref_id))
        original = next(iter(runner.provider.events))
        cancel = runner.provider.cancel_event
        lost = False

        def unknown_before_cancel(request):
            nonlocal lost
            if not lost:
                lost = True
                raise CalendarUnknownOutcome()
            current = runner.provider.events.get(request.external_event_id)
            if current and request.provider_etag != current.provider_etag:
                raise CalendarProviderError(
                    "google_calendar_precondition_failed", "The marker changed.", transient=False,
                )
            return cancel(request)

        monkeypatch.setattr(runner.provider, "cancel_event", unknown_before_cancel)
        await edit(call, task_ref, "cancel-recovery-reschedule",
                   remind_at="2026-11-13T18:00:00-08:00")
        drain(runner)
        assert original in runner.provider.events
        with session_factory() as session:
            cancellation = session.scalar(select(Operation).where(
                Operation.operation_type == "calendar_cancel_event",
            ))
            operation_ref = cancellation.ref_id
            assert cancellation.status == "reconciliation_required"
        if provider_changed == "before_read":
            prior = runner.provider.events[original]
            runner.provider.events[original] = replace(
                prior, provider_etag='"independent-edit"',
                snapshot={**prior.snapshot, "summary": "An independently edited marker"},
            )
        assert runner.reconcile_once()
        with session_factory() as session:
            cancellation = session.scalar(select(Operation).where(
                Operation.ref_id == operation_ref,
            ))
            assert cancellation.status == (
                "reconciliation_required" if provider_changed == "before_read" else "pending"
            )
        if provider_changed == "before_read":
            assert original in runner.provider.events
            assert len(runner.provider.events) == 2
            return
        if provider_changed == "after_read":
            prior = runner.provider.events[original]
            runner.provider.events[original] = replace(
                prior, provider_etag='"edit-after-read"',
                snapshot={**prior.snapshot, "summary": "A concurrent Calendar edit"},
            )
            # A Calendar refresh may update the binding after the proof read.
            with session_factory.begin() as session:
                binding = session.scalar(select(ProviderEventBinding).where(
                    ProviderEventBinding.provider_event_id == original,
                ))
                binding.provider_etag = '"edit-after-read"'
                binding.version += 1
        drain(runner)
        if provider_changed == "after_read":
            assert original in runner.provider.events
            assert len(runner.provider.events) == 2
            with session_factory() as session:
                assert session.scalar(select(Operation.last_error_code).where(
                    Operation.ref_id == operation_ref,
                )) == "google_calendar_precondition_failed"
            return
        assert original not in runner.provider.events
        assert len(runner.provider.events) == 1
        with session_factory() as session:
            assert session.scalar(select(Operation.status).where(
                Operation.ref_id == operation_ref,
            )) == "succeeded"
            assert session.scalar(select(Task.reminder_generation)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_flat_reminder_reschedules_and_completes_without_changing_scheduled_day(
    session_factory,
    tasks_client,
    monkeypatch,
):
    async with tasks_client() as (call, runner, provider, _packet):
        monkeypatch.setattr(get_settings(), "tasks_enabled", True)
        monkeypatch.setattr(
            get_settings(),
            "plugin_oauth_issuer",
            runner.settings.plugin_oauth_issuer,
        )
        context_id = await configure(call, session_factory)
        result = await call(
            "docket_create_task",
            {
                "context_id": context_id,
                "operation_id": "create-with-reminder",
                "title": "Task with an exact clock",
                "scheduled_date": "2026-11-13",
                "remind_at": "2026-11-13T17:00:00-08:00",
                "timezone": "America/Los_Angeles",
            },
        )
        assert result.get("disposition") == "committed", result
        assert runner.provider.events == {} and provider.tasks == {}
        with session_factory() as session:
            task = session.scalar(select(Task))
            ref = task.ref_id
            assert task.reminder_generation == 1 and task.remind_at is not None
            assert set(session.scalars(select(Operation.operation_type))) == {
                "calendar_create_event",
                "tasks_insert",
            }
        drain(runner)
        first = next(iter(runner.provider.events.values()))
        assert first.snapshot["visibility"] == "private"
        assert first.snapshot["transparency"] == "transparent"
        assert first.snapshot["reminders"]["overrides"] == [{"method": "popup", "minutes": 0}]
        delivery = await call("docket_get_history_entry", {
            "ref": result["changeset_ref"], "view": "delivery",
        })
        marker = next(row for row in delivery["items"]
                      if row["operation_type"] == "calendar_create_event")
        assert "snapshot_diagnostic" not in marker
        assert marker["timing"] == {
            "kind": "timed", "start_local": "2026-11-13T17:00:00-08:00",
            "end_local": "2026-11-13T17:01:00-08:00", "timezone": "America/Los_Angeles",
        }
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
        renamed = provider.patch_task(
            "synthetic-list",
            remote.task_id,
            {"title": "5:00 PM — Renamed in Google"},
            etag=remote.etag,
        )
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            sync = TaskSyncService(session, runner.settings)
            observation = sync.observe(
                listing,
                renamed,
                generation=uuid.uuid4(),
                read_started_at=utc_now(),
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
        assert (
            sum(e.snapshot.get("status") != "cancelled" for e in runner.provider.events.values())
            == 1
        )
        assert next(iter(provider.tasks.values()))["title"] == (
            "2026-11-14 5:00 PM — Renamed in Google"
        )
        remote = provider.list_tasks("synthetic-list").tasks[0]
        completed = provider.patch_task(
            "synthetic-list",
            remote.task_id,
            {"status": "completed", "completed": "2026-11-14T20:00:00Z"},
            etag=remote.etag,
        )
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            sync = TaskSyncService(session, runner.settings)
            sync.reconcile(
                sync.observe(
                    listing,
                    completed,
                    generation=uuid.uuid4(),
                    read_started_at=utc_now(),
                )
            )
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
    session_factory,
    tasks_client,
    window,
):
    async with tasks_client() as (call, runner, _provider, _packet):
        context_id = await configure(call, session_factory)
        result = await call(
            "docket_create_task",
            {
                "context_id": context_id,
                "operation_id": "create-retired-reminder",
                "title": "Reminder with delayed delivery",
                "remind_at": "2026-11-13T17:00:00-08:00",
            },
        )
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
                op = session.scalar(
                    select(Operation).where(
                        Operation.operation_type == "calendar_create_event",
                    )
                )
                op.next_attempt_at = utc_now() - timedelta(seconds=1)
            assert runner.reconcile_once()
            with session_factory.begin() as session:
                op = session.scalar(
                    select(Operation).where(
                        Operation.operation_type == "calendar_create_event",
                    )
                )
                assert op.status == "reconciliation_required"
                op.next_attempt_at = utc_now() - timedelta(seconds=1)
            runner.provider.events.update(saved)
            assert runner.reconcile_once()
        drain(runner)
        assert all(e.snapshot.get("status") == "cancelled" for e in runner.provider.events.values())
        with session_factory() as session:
            assert not any(session.scalars(select(TemporalCalendarProjection.enabled)))
            assert set(session.scalars(select(Operation.status))) <= {"succeeded", "superseded"}


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_reminder_edit_does_not_resolve_an_unrelated_title_conflict(
    session_factory,
    tasks_client,
    monkeypatch,
):
    async with tasks_client() as (call, runner, provider, _packet):
        monkeypatch.setattr(get_settings(), "tasks_enabled", True)
        monkeypatch.setattr(
            get_settings(),
            "plugin_oauth_issuer",
            runner.settings.plugin_oauth_issuer,
        )
        context_id = await configure(call, session_factory)
        created = await call(
            "docket_create_task",
            {
                "context_id": context_id,
                "operation_id": "clock-title-conflict",
                "title": "Original",
                "scheduled_date": "2026-11-13",
                "remind_at": "2026-11-13T17:00:00-08:00",
            },
        )
        assert created.get("disposition") == "committed", created
        drain(runner)
        with session_factory() as session:
            ref = session.scalar(select(Task.ref_id))
        await edit(call, ref, "local-title-before-google", title="Local title")
        remote = provider.list_tasks("synthetic-list").tasks[0]
        changed = provider.patch_task(
            "synthetic-list", remote.task_id, {"title": "5:00 PM — Google title"}, etag=remote.etag
        )
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            sync = TaskSyncService(session, runner.settings)
            sync.reconcile(
                sync.observe(
                    listing,
                    changed,
                    generation=uuid.uuid4(),
                    read_started_at=utc_now(),
                )
            )
        await edit(
            call, ref, "change-clock-with-title-conflict", remind_at="2026-11-13T18:00:00-08:00"
        )
        with session_factory() as session:
            binding = TaskProviderIntentService(session).bindings.active(ref)
            assert "title" in TaskProviderIntentService(session)._open_fields(binding)
            assert session.scalar(select(TaskConflictResolution)) is None
            assert session.scalar(select(Task.title)) == "Local title"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("external_edit", ["clock", "delete", "privacy", "all_day", "revoked"])
async def test_owned_calendar_observation_changes_only_reminder_under_its_original_grant(
    session_factory,
    tasks_client,
    monkeypatch,
    external_edit,
):
    from dataclasses import replace
    from datetime import UTC, datetime

    from sqlalchemy import func

    from docket.models import (
        AuthenticatedRequest,
        OAuthPrincipal,
        OperatorUtterance,
        PluginNotification,
        TaskPolicyEffect,
    )
    from docket.providers.google.calendar import CalendarSnapshotEvent
    from docket.services.calendar_sync import CalendarSyncService

    async with tasks_client() as (call, runner, _provider, _packet):
        monkeypatch.setattr(
            get_settings(), "plugin_oauth_issuer", runner.settings.plugin_oauth_issuer
        )
        context_id = await configure(call, session_factory)
        created = await call(
            "docket_create_task",
            {
                "context_id": context_id,
                "operation_id": "calendar-observation-task",
                "title": "Owned reminder",
                "scheduled_date": "2026-11-13",
                "remind_at": "2026-11-13T17:00:00-08:00",
                "timezone": "America/Los_Angeles",
            },
        )
        assert created.get("disposition") == "committed", created
        drain(runner)
        initial_at = datetime.fromisoformat("2026-11-13T17:00:00-08:00").astimezone(UTC)
        with session_factory() as session:
            task = session.scalar(select(Task))
            task_ref = task.ref_id
            list_ref = task.task_list_ref
            item_ref = task.item_ref
        context = await call("docket_get_tasks_context", {"refs": [list_ref]})
        disabled = await call(
            "docket_configure_task_list",
            {
                "context_id": context["context_id"],
                "operation_id": "suspend-tasks-sync",
                "task_list_ref": list_ref,
                "mode": "disabled",
            },
        )
        assert disabled.get("disposition") == "committed", disabled
        with session_factory.begin() as session:
            requests_before = session.scalar(select(func.count()).select_from(AuthenticatedRequest))
            utterances_before = session.scalar(select(func.count()).select_from(OperatorUtterance))
            if external_edit == "revoked":
                session.scalar(select(OAuthPrincipal)).enabled = False
        first = next(iter(runner.provider.events.values()))
        changed_at = initial_at + timedelta(hours=1)
        observed = CalendarSnapshotEvent(
            provider_event_id=first.external_event_id,
            status="confirmed",
            summary="Owned reminder",
            location=None,
            is_all_day=False,
            start_at=changed_at,
            end_at=changed_at + timedelta(seconds=60),
            timezone="America/Los_Angeles",
            provider_etag='"external-edit"',
            visibility="private",
            transparency="transparent",
            provider_reminders={
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 0}],
            },
        )
        if external_edit == "delete":
            observed = replace(observed, status="cancelled", start_at=None, end_at=None)
        elif external_edit == "privacy":
            observed = replace(
                observed,
                start_at=initial_at,
                end_at=initial_at + timedelta(seconds=60),
                visibility="default",
            )
        elif external_edit == "all_day":
            observed = replace(
                observed,
                is_all_day=True,
                start_at=None,
                end_at=None,
                start_date=initial_at.date(),
                end_date=initial_at.date() + timedelta(days=1),
            )
        runner.provider.events[first.external_event_id] = replace(
            first,
            provider_etag=observed.provider_etag,
            snapshot={**first.snapshot, "status": observed.status},
        )
        runner.provider.put_snapshot_event(observed)
        # A copied label and private properties do not grant reminder authority.
        runner.provider.put_snapshot_event(replace(observed, provider_event_id="unowned-copy"))
        settings = runner.settings.model_copy(
            update={
                "calendar_reads_enabled": True,
                "tasks_enabled": False,
            }
        )
        sync = CalendarSyncService(
            session_factory,
            runner.provider,
            settings,
            clock=lambda: utc_now() + timedelta(seconds=1),
        )
        assert sync.run_due_once()
        with session_factory() as session:
            task = session.scalar(select(Task).where(Task.ref_id == task_ref))
            expected = (
                changed_at
                if external_edit == "clock"
                else None
                if external_edit == "delete"
                else initial_at
            )
            assert (task.remind_at.replace(tzinfo=UTC) if task.remind_at else None) == expected
            assert task.item_ref == item_ref and task.title == "Owned reminder"
            assert (
                task.scheduled_date.isoformat() == "2026-11-13" and task.task_state == "not_started"
            )
            assert session.scalar(select(func.count()).select_from(Task)) == 1
            assert (
                session.scalar(select(func.count()).select_from(AuthenticatedRequest))
                == requests_before
            )
            assert (
                session.scalar(select(func.count()).select_from(OperatorUtterance))
                == utterances_before
            )
            effects = list(
                session.scalars(
                    select(TaskPolicyEffect).where(
                        TaskPolicyEffect.reminder_generation_ref.is_not(None),
                    )
                )
            )
            assert len(effects) == (1 if external_edit in {"clock", "delete", "privacy"} else 0)
            notices = session.scalar(
                select(func.count())
                .select_from(PluginNotification)
                .where(
                    PluginNotification.event_name == "docket.system.alert",
                )
            )
            assert notices == (1 if external_edit in {"all_day", "revoked"} else 0)
            if external_edit == "privacy":
                assert task.reminder_generation == 2
        if external_edit in {"clock", "delete", "privacy"}:
            drain(runner)
            with session_factory() as session:
                calendar_ops = list(
                    session.scalars(
                        select(Operation).where(
                            Operation.operation_type.like("calendar_%"),
                        )
                    )
                )
                assert {operation.status for operation in calendar_ops} == {"succeeded"}
