"""Canonical native task commands execute through the shared provider queue."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
import test_native_plugin
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    GoogleTaskList,
    Operation,
    OperationTarget,
    ProviderAccount,
    Task,
    TaskProviderBinding,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.providers.google.fake_tasks import FakeTasksProvider
from docket.services.operations import OperationRunner
from docket.services.task_sync import TaskSyncService

linked_plugin = test_native_plugin.linked_plugin


@pytest.fixture
def tasks_client(session_factory, linked_plugin):
    app, settings, token, _ = linked_plugin
    settings.tasks_enabled = True
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="synthetic-tasks",
            capabilities=["google_tasks"],
        )
        session.add(account)
        session.flush()
        listing = GoogleTaskList(
            account_id=account.id,
            operator_ref=settings.canonical_operator_ref,
            provider_list_id="synthetic-list",
            title="Validation",
        )
        session.add(listing)
        session.flush()
        list_ref = listing.ref_id
    provider = FakeTasksProvider(page_size=1)
    provider.seed_list("synthetic-list", "Validation")
    runner = OperationRunner(
        session_factory,
        FakeCalendarProvider(),
        settings=settings,
        tasks_provider=provider,
    )

    @asynccontextmanager
    async def connect():
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app), headers={"Authorization": "Bearer " + token}
            ) as http,
            Client(
                streamable_http_client("https://docket.you/mcp/", http_client=http), mode="legacy"
            ) as client,
        ):

            async def call(name, args):
                result = (await client.call_tool(name, args)).structured_content
                assert result is not None
                return result

            packet = await call("docket_get_tasks_context", {})
            policy = await call(
                "docket_configure_task_list",
                {
                    "context_id": packet["context_id"],
                    "operation_id": "task-policy",
                    "task_list_ref": list_ref,
                    "mode": "two_way",
                    "is_default": True,
                },
            )
            assert policy.get("disposition") == "committed", policy
            yield call, runner, provider, packet

    return connect


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_native_task_delivery_updates_completion_reopening_and_deletion(
    session_factory,
    tasks_client,
):
    async with tasks_client() as (call, runner, provider, packet):
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "create-delivered",
                "title": "A" * 1024,
                "notes": "Canonical notes",
                "scheduled_date": "2026-11-13",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        with session_factory() as session:
            task = session.scalar(select(Task))
            task_ref = task.ref_id
            operation = session.scalar(select(Operation))
            assert operation.status == "succeeded", operation.last_error_code
            assert task.description == "Canonical notes"
        for index, fields in enumerate(
            ({"notes": "Updated notes"}, {"state": "completed"}, {"state": "not_started"})
        ):
            context = await call("docket_get_tasks_context", {"refs": [task_ref]})
            updated = await call(
                "docket_update_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "task-edit-" + str(index),
                    "task_ref": task_ref,
                    **fields,
                },
            )
            assert updated.get("disposition") == "committed", updated
            assert runner.run_due_once()
            with session_factory() as session:
                assert set(session.scalars(select(Operation.status))) == {"succeeded"}
        remote = next(iter(provider.tasks.values()))
        assert remote["title"] == "A" * 1024
        assert remote["due"].startswith("2026-11-13") and remote["status"] == "needsAction"
        assert remote["notes"].startswith("Updated notes\n\n[Docket-Task-v1:")
        context = await call("docket_get_context", {"refs": [task_ref]})
        removed = await call(
            "docket_retract_record",
            {
                "context_id": context["context_id"],
                "operation_id": "retract-task",
                "target_ref": task_ref,
            },
        )
        assert removed.get("disposition") == "committed", removed
        assert runner.run_due_once()
        assert next(iter(provider.tasks.values()))["deleted"] is True
        with session_factory() as session:
            assert set(session.scalars(select(Operation.status))) == {"succeeded"}


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_unknown_creation_recovers_after_complete_pages_without_another_insert(
    session_factory,
    tasks_client,
    monkeypatch,
):
    async with tasks_client() as (call, runner, provider, packet):
        now = [datetime.now(UTC)]
        monkeypatch.setattr("docket.services.operations.utc_now", lambda: now[0])
        monkeypatch.setattr("docket.services.tasks_execution.utc_now", lambda: now[0])
        provider.insert_task("synthetic-list", {"title": "Unrelated record"})
        provider.uncertain_insert = True
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "create-uncertain",
                "title": "Recover original",
                "notes": "Canonical notes",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        with session_factory() as session:
            operation = session.scalar(select(Operation))
            assert operation.status == "reconciliation_required"
        posts = sum(method == "POST" for method, _ in provider.calls)
        assert runner.reconcile_once()
        with session_factory() as session:
            target = session.scalar(select(OperationTarget))
            assert target.recovery["page"] == "1"
            assert session.scalar(select(Operation.status)) == "reconciliation_required"
        now[0] += timedelta(seconds=2)
        assert runner.reconcile_once()
        with session_factory() as session:
            assert session.scalar(select(Operation.status)) == "succeeded"
        assert sum(method == "POST" for method, _ in provider.calls) == posts
        assert len(provider.tasks) == 2


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_stale_conditional_write_retries_original_field_after_independent_google_edit(
    session_factory,
    tasks_client,
    monkeypatch,
):
    async with tasks_client() as (call, runner, provider, packet):
        monkeypatch.setattr(get_settings(), "tasks_enabled", True)
        monkeypatch.setattr(
            get_settings(), "plugin_oauth_issuer", runner.settings.plugin_oauth_issuer
        )
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "create-stale-source",
                "title": "Original",
                "notes": "Original notes",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        with session_factory() as session:
            task_ref = session.scalar(select(Task.ref_id))
        packet = await call("docket_get_tasks_context", {"refs": [task_ref]})
        renamed = await call(
            "docket_update_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "rename-with-stale-etag",
                "task_ref": task_ref,
                "title": "Native rename",
            },
        )
        assert renamed.get("disposition") == "committed", renamed
        remote = next(iter(provider.tasks.values()))
        newer = provider.patch_task(
            "synthetic-list",
            remote["id"],
            {"notes": "Independent Google notes"},
            etag=remote["etag"],
        )
        assert runner.run_due_once()
        with session_factory() as session:
            assert "pending" in set(session.scalars(select(Operation.status)))
            # The rejected write remains in attempt history; a real fresh read
            # re-admits the same intent without waiting for the next poll.
            assert session.scalar(select(Task.description)) == "Independent Google notes"
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            service = TaskSyncService(session, runner.settings)
            observation = service.observe(
                listing, newer, generation=uuid.uuid4(), read_started_at=datetime.now(UTC)
            )
            service.reconcile(observation)
        assert runner.run_due_once()
        with session_factory() as session:
            task = session.scalar(select(Task))
            assert task.title == "Native rename" and task.description == "Independent Google notes"
            assert set(session.scalars(select(Operation.status))) == {"succeeded"}
        remote = next(iter(provider.tasks.values()))
        assert remote["title"] == "Native rename" and remote["notes"] == "Independent Google notes"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_removed_creation_marker_stays_uncertain_without_duplicate_post(
    session_factory,
    tasks_client,
):
    async with tasks_client() as (call, runner, provider, packet):
        provider.uncertain_insert = True
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "unknown-marker-removed",
                "title": "Keep one record",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        remote = next(iter(provider.tasks.values()))
        provider.patch_task("synthetic-list", remote["id"], {"notes": ""}, etag=remote["etag"])
        assert runner.reconcile_once()
        assert not runner.run_due_once()
        with session_factory() as session:
            assert session.scalar(select(Operation.status)) == "reconciliation_required"
            assert session.scalar(select(TaskProviderBinding.provider_task_id)) is None
        assert sum(method == "POST" for method, _ in provider.calls) == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_interrupted_reconciliation_never_returns_uncertain_insert_to_execution(
    session_factory,
    tasks_client,
):
    async with tasks_client() as (call, runner, provider, packet):
        provider.uncertain_insert = True
        await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "unknown-interrupted-read",
                "title": "One record only",
            },
        )
        assert runner.run_due_once()
        claim = runner.claim_reconciliation()
        assert claim is not None
        with session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            operation.leased_until = target.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        assert runner.recover_expired_leases() == 1
        assert not runner.run_due_once()
        with session_factory() as session:
            assert session.scalar(select(Operation.status)) == "reconciliation_required"
        assert sum(method == "POST" for method, _ in provider.calls) == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("list_inaccessible", [False, True])
@pytest.mark.asyncio
async def test_source_absence_cannot_confirm_delete_even_with_accessible_collection(
    session_factory,
    tasks_client,
    monkeypatch,
    list_inaccessible,
):
    async with tasks_client() as (call, runner, provider, packet):
        now = [datetime.now(UTC)]
        monkeypatch.setattr("docket.services.operations.utc_now", lambda: now[0])
        monkeypatch.setattr("docket.services.tasks_execution.utc_now", lambda: now[0])
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "missing-source-create",
                "title": "Scoped missing task",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        with session_factory() as session:
            task_ref = session.scalar(select(Task.ref_id))
        context = await call("docket_get_context", {"refs": [task_ref]})
        removed = await call(
            "docket_retract_record",
            {
                "context_id": context["context_id"],
                "operation_id": "missing-source-remove",
                "target_ref": task_ref,
            },
        )
        assert removed.get("disposition") == "committed", removed
        provider.tasks.clear()
        assert runner.run_due_once()
        now[0] += timedelta(seconds=31)
        assert runner.reconcile_once()
        if list_inaccessible:
            provider.lists.clear()
        now[0] += timedelta(seconds=31)
        assert runner.reconcile_once()
        with session_factory() as session:
            deletion = session.scalar(
                select(Operation).where(
                    Operation.operation_type == "tasks_delete",
                )
            )
            assert deletion.status == "reconciliation_required"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("remove_marker", [False, True])
@pytest.mark.asyncio
async def test_poll_cannot_import_uncertain_creation_as_another_canonical_task(
    session_factory,
    tasks_client,
    monkeypatch,
    remove_marker,
):
    from sqlalchemy import func

    async with tasks_client() as (call, runner, provider, packet):
        monkeypatch.setattr(get_settings(), "tasks_enabled", True)
        monkeypatch.setattr(
            get_settings(), "plugin_oauth_issuer", runner.settings.plugin_oauth_issuer
        )
        provider.uncertain_insert = True
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "poll-before-create-recovery",
                "title": "Retain one canonical task",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        remote = next(iter(provider.tasks.values()))
        if remove_marker:
            provider.patch_task("synthetic-list", remote["id"], {"notes": ""}, etag=remote["etag"])
        observed = provider.get_task("synthetic-list", remote["id"])
        with session_factory.begin() as session:
            listing = session.scalar(select(GoogleTaskList))
            service = TaskSyncService(session, runner.settings)
            evidence = service.observe(
                listing, observed, generation=uuid.uuid4(), read_started_at=datetime.now(UTC)
            )
            result = service.reconcile(evidence)
            assert result["state"] == (
                "creation_identity_ambiguous"
                if remove_marker
                else "creation_reconciliation_pending"
            )
            assert session.scalar(select(func.count()).select_from(Task)) == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_explicit_link_repairs_removed_marker_without_fabricating_original_outcome(
    session_factory,
    tasks_client,
):
    async with tasks_client() as (call, runner, provider, packet):
        provider.uncertain_insert = True
        created = await call(
            "docket_create_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "explicit-repair-source",
                "title": "One linked task",
            },
        )
        assert created.get("disposition") == "committed", created
        assert runner.run_due_once()
        remote = next(iter(provider.tasks.values()))
        observed = provider.patch_task(
            "synthetic-list", remote["id"], {"notes": ""}, etag=remote["etag"]
        )
        with session_factory.begin() as session:
            task_ref = session.scalar(select(Task.ref_id))
            listing = session.scalar(select(GoogleTaskList))
            evidence = TaskSyncService(session, runner.settings).observe(
                listing,
                observed,
                generation=uuid.uuid4(),
                read_started_at=datetime.now(UTC),
            )
            observation_ref = evidence.ref_id
        packet = await call("docket_get_tasks_context", {"refs": [task_ref, observation_ref]})
        repaired = await call(
            "docket_link_google_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "explicit-repair-link",
                "task_ref": task_ref,
                "observation_ref": observation_ref,
            },
        )
        assert repaired.get("disposition") == "committed", repaired
        with session_factory() as session:
            original = session.scalar(select(Operation))
            assert original.status == "superseded"
            assert original.result["provider_outcome"] == "unresolved"
            assert session.scalar(select(TaskProviderBinding.provider_task_id)) == remote["id"]
        packet = await call("docket_get_tasks_context", {"refs": [task_ref]})
        edited = await call(
            "docket_update_task",
            {
                "context_id": packet["context_id"],
                "operation_id": "edit-after-explicit-repair",
                "task_ref": task_ref,
                "title": "Continue normal work",
            },
        )
        assert edited.get("disposition") == "committed", edited
        assert runner.run_due_once()
        assert next(iter(provider.tasks.values()))["title"] == "Continue normal work"
        assert sum(method == "POST" for method, _ in provider.calls) == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("moved_in_google", [False, True])
@pytest.mark.asyncio
async def test_parent_subtree_move_completion_and_retraction_preserve_identity(
    session_factory,
    tasks_client,
    moved_in_google,
    monkeypatch,
):
    async with tasks_client() as (call, runner, provider, _packet):
        monkeypatch.setattr("docket.services.task_policy_authority.get_settings",
                            lambda: runner.settings)
        with session_factory.begin() as session:
            source = session.scalar(select(GoogleTaskList))
            source_ref, account_id = source.ref_id, source.account_id
            destination = GoogleTaskList(
                account_id=account_id,
                operator_ref=get_settings().canonical_operator_ref,
                provider_list_id="second-list",
                title="Other tasks",
            )
            session.add(destination)
            session.flush()
            destination_ref = destination.ref_id
        provider.seed_list("second-list", "Other tasks")
        context = await call("docket_get_tasks_context", {})
        result = await call(
            "docket_configure_task_list",
            {
                "context_id": context["context_id"],
                "operation_id": "second-list-policy",
                "task_list_ref": destination_ref,
                "mode": "two_way",
                "is_default": True,
            },
        )
        assert result.get("disposition") == "committed", result
        refs = []
        for index, title in enumerate(("Parent", "Child", "Grandchild")):
            context = await call("docket_get_tasks_context", {"refs": refs[-1:]})
            result = await call(
                "docket_create_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "tree-" + str(index),
                    "title": title,
                    **({"parent_task_ref": refs[-1]} if refs else {"task_list_ref": source_ref}),
                },
            )
            assert result.get("disposition") == "committed", result
            assert runner.run_due_once()
            with session_factory() as session:
                task = session.scalar(select(Task).where(Task.title == title))
                assert task.task_list_ref == source_ref  # Parent wins over the changed default.
                refs.append(task.ref_id)
        with session_factory() as session:
            identities = {
                row.task_ref: (row.provider_task_id, row.task_ref)
                for row in session.scalars(select(TaskProviderBinding))
            }
        if moved_in_google:
            remote = provider.get_task("synthetic-list", identities[refs[0]][0])
            provider.move_task(
                "synthetic-list",
                remote.task_id,
                etag=remote.etag,
                destination_task_list_id="second-list",
            )
            for ref in refs:
                remote = provider.get_task("second-list", identities[ref][0])
                with session_factory.begin() as session:
                    listing = session.get(GoogleTaskList, destination_ref)
                    sync = TaskSyncService(session, runner.settings)
                    observation = sync.observe(
                        listing, remote, generation=uuid.uuid4(), read_started_at=datetime.now(UTC)
                    )
                    result = sync.reconcile(observation)
                    assert result["state"] in {"reconciled", "unchanged"}, result
        else:
            context = await call("docket_get_tasks_context", {"refs": [refs[0], destination_ref]})
            moved = await call(
                "docket_update_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "move-tree",
                    "task_ref": refs[0],
                    "task_list_ref": destination_ref,
                },
            )
            assert moved.get("disposition") == "committed", moved
            for _ in range(3):
                assert runner.run_due_once()
        assert sum(url.endswith("/move") for _, url in provider.calls) == 1
        assert {owner for owner, _ in provider.tasks} == {"second-list"}
        with session_factory() as session:
            assert {task.task_list_ref for task in session.scalars(select(Task))} == {
                destination_ref
            }
            assert {
                row.task_ref: (row.provider_task_id, row.task_ref)
                for row in session.scalars(select(TaskProviderBinding))
            } == identities
            assert set(session.scalars(select(Operation.status))) == {"succeeded"}
        context = await call("docket_get_tasks_context", {"refs": [refs[0]]})
        completed = await call(
            "docket_update_task",
            {
                "context_id": context["context_id"],
                "operation_id": "complete-tree",
                "task_ref": refs[0],
                "state": "completed",
                "title": "Finished parent",
            },
        )
        assert completed.get("disposition") == "committed", completed
        for _ in range(4):
            assert runner.run_due_once()
        with session_factory() as session:
            tasks = list(session.scalars(select(Task)))
            assert {task.task_state for task in tasks} == {"completed"}
            assert {task.title for task in tasks} == {"Finished parent", "Child", "Grandchild"}
            assert set(session.scalars(select(Operation.status))) == {"succeeded"}
        # New, unobserved children are covered by the standing subtree policy.
        root_id = identities[refs[0]][0]
        late = provider.insert_task("second-list", {"title": "Added on Google"}, parent=root_id)
        context = await call("docket_get_context", {"refs": [refs[0]]})
        removed = await call(
            "docket_retract_record",
            {
                "context_id": context["context_id"],
                "operation_id": "retract-tree",
                "target_ref": refs[0],
            },
        )
        assert removed.get("disposition") == "committed", removed
        for _ in range(10):
            if not runner.run_due_once():
                break
        assert provider.tasks[("second-list", late.task_id)]["deleted"] is True
        with session_factory() as session:
            assert set(session.scalars(select(Task.canonical_status))) == {"retracted"}
            assert set(session.scalars(select(Operation.status))) == {"succeeded"}
