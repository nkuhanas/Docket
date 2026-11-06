"""Synthetic Tasks imports, retained bindings and concurrent native edits on PostgreSQL."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier


async def check_tasks(settings, factory, _principal):
    from sqlalchemy import func, inspect, select, update
    from sqlalchemy.exc import DBAPIError

    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.models import (
        AuthenticatedRequest,
        CalendarEventCache,
        CalendarLane,
        GoogleTaskList,
        GoogleTaskObservation,
        Item,
        Operation,
        OperationTarget,
        OperatorUtterance,
        ProviderAccount,
        ProviderEventBinding,
        Task,
        TaskBindingRevision,
        TaskConflictResolution,
        TaskListPolicyRevision,
        TaskPolicyEffect,
        TaskProviderBinding,
        TaskReminderGeneration,
        TasksAccountState,
        TasksMethodValidation,
        TaskSyncConflict,
        TaskSyncState,
        TemporalCalendarProjection,
    )
    from docket.models.tracked_context import TaskItemTitleOwnership
    from docket.plugin.server import PluginMCP
    from docket.providers.google.tasks import GoogleTask
    from docket.services.task_sync import TaskSyncService

    principal = AgentPrincipal(
        "agent:interactive", settings.canonical_operator_ref, "interactive",
        frozenset({"read", "stage", "commit", "resolve_conflict"}),
    )

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    engine = factory.kw["bind"]
    assert engine.dialect.name == "postgresql"
    for model in (
        GoogleTaskList, GoogleTaskObservation, TaskProviderBinding, TaskBindingRevision,
        TaskListPolicyRevision, TaskPolicyEffect, TaskSyncConflict, TaskConflictResolution,
        TaskSyncState, TasksAccountState, TasksMethodValidation, TaskItemTitleOwnership,
        Operation, OperationTarget,
        TaskReminderGeneration, CalendarEventCache,
        ProviderEventBinding,
    ):
        columns = {column["name"]: column for column in inspect(engine).get_columns(
            model.__tablename__,
        )}
        assert set(columns) == set(model.__table__.columns.keys()), model.__tablename__
        for column in model.__table__.columns:
            assert columns[column.name]["nullable"] == column.nullable
            assert columns[column.name]["type"]._type_affinity == column.type._type_affinity
            assert getattr(columns[column.name]["type"], "length", None) == getattr(
                column.type, "length", None,
            )
    saved_enabled, saved_agent = settings.tasks_enabled, settings.interactive_agent_enabled
    settings.tasks_enabled = settings.interactive_agent_enabled = True
    try:
        with factory.begin() as session:
            account = ProviderAccount(
                provider="google", external_account_id="synthetic-task-pg",
                capabilities=["google_tasks"],
            )
            session.add(account)
            session.flush()
            listing = GoogleTaskList(
                operator_ref=principal.operator_ref, account_id=account.id,
                provider_list_id="synthetic-validation", title="Task validation",
            )
            session.add(listing)
            session.flush()
            list_ref = listing.ref_id
        packet = await call("docket_get_tasks_context", {"refs": [list_ref]})
        policy = await call("docket_configure_task_list", {
            "context_id": packet["context_id"], "operation_id": "pg-task-policy",
            "task_list_ref": list_ref, "mode": "two_way",
        })
        assert policy.get("disposition") == "committed", policy
        with factory() as session:
            requests_before = session.scalar(select(func.count()).select_from(AuthenticatedRequest))
            utterances_before = session.scalar(select(func.count()).select_from(OperatorUtterance))
        with factory.begin() as session:
            listing = session.get(GoogleTaskList, list_ref)
            sync = TaskSyncService(session, settings)
            observation = sync.observe(
                listing, GoogleTask("synthetic-task", '"one"', {
                    "title": "A" * 1024, "status": "needsAction",
                }), generation=uuid.uuid4(), read_started_at=datetime.now(UTC),
            )
            result = sync.reconcile(observation)
            task_ref = result["task_ref"]
            binding_ref = result["binding_ref"]
        with factory() as session:
            task = session.scalar(select(Task).where(Task.ref_id == task_ref))
            item = session.scalar(select(Item).where(Item.ref_id == task.item_ref))
            assert task.title == item.title == "A" * 1024
            assert requests_before == session.scalar(
                select(func.count()).select_from(AuthenticatedRequest),
            )
            assert utterances_before == session.scalar(
                select(func.count()).select_from(OperatorUtterance),
            )
            binding = session.get(TaskProviderBinding, binding_ref)
            version = binding.version
        try:
            with factory.begin() as session:
                session.execute(update(TaskProviderBinding).where(
                    TaskProviderBinding.ref_id == binding_ref,
                ).values(version=version + 1, baseline={"title": "unretained"}))
        except DBAPIError as exc:
            assert "retained revision" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL accepted an unretained Task baseline")
        packet = await call("docket_get_tasks_context", {"refs": [task_ref]})
        previews = [await call("docket_update_task", {
            "context_id": packet["context_id"], "operation_id": "pg-task-race-" + str(index),
            "task_ref": task_ref, "title": "Concurrent title " + str(index), "preview": True,
        }) for index in (1, 2)]
        assert all(row.get("disposition") == "prepared" for row in previews), previews
        barrier = Barrier(2)

        def apply(preview):
            barrier.wait(timeout=15)
            return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, previews))
        assert sum(row.get("disposition") == "committed" for row in results) == 1, results
        with factory() as session:
            task = session.scalar(select(Task).where(Task.ref_id == task_ref))
            assert session.scalar(
                select(Item.title).where(Item.ref_id == task.item_ref)
            ) == task.title
        from docket.providers.google.fake_tasks import FakeTasksProvider
        from docket.services.operations import OperationRunner

        provider = FakeTasksProvider()
        provider.seed_list("synthetic-validation", "Task validation")
        provider.tasks[("synthetic-validation", "synthetic-task")] = {
            "id": "synthetic-task", "etag": '"one"',
            "title": "A" * 1024, "status": "needsAction",
        }
        runner = OperationRunner(factory, None, settings=settings, tasks_provider=provider)
        assert runner.run_due_once()
        with factory() as session:
            operation = session.scalar(select(Operation).join(OperationTarget).where(
                OperationTarget.canonical_target_ref == task_ref,
            ))
            assert operation.status == "succeeded", operation.last_error_code
            binding = session.get(TaskProviderBinding, binding_ref)
            task = session.scalar(select(Task).where(Task.ref_id == task_ref))
            assert binding.baseline["title"] == task.title
        from docket.providers.google.fake_calendar import FakeCalendarProvider

        with factory.begin() as session:
            listing = session.get(GoogleTaskList, list_ref)
            account = session.get(ProviderAccount, listing.account_id)
            account.capabilities = ["google_tasks", "google_calendar"]
            config_root = session.get(TaskListPolicyRevision, listing.policy_ref)
            lane = CalendarLane(
                account_id=account.id, lane="task-validation", display_name="Task reminders",
                color_hex="#123456", calendar_id="synthetic-reminder-calendar", status="active",
                created_by_changeset_ref=config_root.created_by_changeset_ref,
            )
            session.add(lane)
            session.flush()
            lane_ref = lane.ref_id
        packet = await call("docket_get_tasks_context", {"refs": [list_ref, lane_ref]})
        configured = await call("docket_configure_task_list", {
            "context_id": packet["context_id"], "operation_id": "pg-task-reminder-route",
            "task_list_ref": list_ref, "mode": "two_way", "reminder_calendar_ref": lane_ref,
        })
        assert configured.get("disposition") == "committed", configured
        packet = await call("docket_get_tasks_context", {"refs": [task_ref]})
        reminded = await call("docket_update_task", {
            "context_id": packet["context_id"], "operation_id": "pg-task-reminder",
            "task_ref": task_ref, "scheduled_date": "2026-11-13",
            "remind_at": "2026-11-13T17:00:00-08:00", "timezone": "America/Los_Angeles",
        })
        assert reminded.get("disposition") == "committed", reminded
        calendar = FakeCalendarProvider()
        runner = OperationRunner(factory, calendar, settings=settings, tasks_provider=provider)

        def drain():
            for _ in range(12):
                if not runner.run_due_once():
                    return
            raise AssertionError("Synthetic reminder queue did not drain")

        drain()
        with factory() as session:
            task = session.scalar(select(Task).where(Task.ref_id == task_ref))
            assert task.reminder_generation == 1
            assert provider.get_task("synthetic-validation", "synthetic-task").fields[
                "title"
            ] == "5:00 PM — " + task.title
            binding = session.get(TaskProviderBinding, binding_ref)
            assert binding.provider_snapshot["title"] == task.title
            assert binding.provider_snapshot["transport_title_prefix"] == "5:00 PM — "
            assert session.scalar(select(CalendarEventCache.origin_kind)) == "task_reminder"
        packet = await call("docket_get_tasks_context", {"refs": [task_ref]})
        replacements = [await call("docket_update_task", {
            "context_id": packet["context_id"], "operation_id": "pg-reminder-race-" + str(hour),
            "task_ref": task_ref, "remind_at": f"2026-11-13T{hour}:00:00-08:00", "preview": True,
        }) for hour in (16, 18)]
        assert all(row.get("disposition") == "prepared" for row in replacements), replacements
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            replacements = list(pool.map(apply, replacements))
        assert sum(row.get("disposition") == "committed" for row in replacements) == 1, replacements
        with factory() as session:
            assert session.scalar(select(Task.reminder_generation).where(
                Task.ref_id == task_ref,
            )) == 2
            assert session.scalar(select(func.count()).select_from(TaskReminderGeneration)) == 2
            assert session.scalar(select(func.count()).select_from(
                TemporalCalendarProjection,
            ).where(TemporalCalendarProjection.enabled.is_(True))) == 1
        drain()
        assert sum(row.snapshot.get("status") != "cancelled"
                   for row in calendar.events.values()) == 1
        packet = await call("docket_get_tasks_context", {"refs": [task_ref]})
        completed = await call("docket_update_task", {
            "context_id": packet["context_id"], "operation_id": "pg-complete-reminded-task",
            "task_ref": task_ref, "state": "completed",
        })
        assert completed.get("disposition") == "committed", completed
        drain()
        assert all(row.snapshot.get("status") == "cancelled" for row in calendar.events.values())
        return {
            "task_storage_shape": "passed", "task_import_genuine_policy_root": "passed",
            "task_full_title_and_derived_item": "passed",
            "task_binding_requires_retained_revision": "passed",
            "task_native_concurrent_edit": "one_commit",
            "task_shared_operation_and_retained_delivery": "passed",
            "task_reminder_atomic_queue_and_title_header": "passed",
            "task_reminder_concurrent_replacement": "one_commit",
            "task_reminder_completion_cancellation": "passed",
        }
    finally:
        settings.tasks_enabled, settings.interactive_agent_enabled = saved_enabled, saved_agent


if __name__ == "__main__":
    import argparse
    import json
    import os
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url

    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--roundtrip-empty", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="docket-task-storage-") as directory:
        os.chdir(directory)
        from docket.config import get_settings
        from docket.database import configure_database, get_session_factory

        config_settings = get_settings()
        url = make_url(config_settings.database_url)
        assert config_settings.environment.value == "test"
        assert not config_settings.external_writes_enabled
        assert url.get_backend_name() == "postgresql"
        assert url.database in {"docket_work_test", "docket_work_smoke"}
        assert url.host in {"127.0.0.1", "localhost", "postgres"}
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        command.upgrade(config, "head")
        if args.roundtrip_empty:
            command.downgrade(config, "20261106c3d4")
            command.upgrade(config, "head")
        configure_database(config_settings.database_url)
        print(json.dumps(asyncio.run(check_tasks(config_settings, get_session_factory(), None))))
