"""Tasks remain small, authenticated native workflows rather than compiler payloads."""

import csv
import io
import json
import uuid

import httpx2
import pytest
import test_native_plugin
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from docket.models import (
    GoogleTaskList,
    GoogleTaskObservation,
    ProviderAccount,
    Task,
    TaskConflictResolution,
    TaskListPolicyRevision,
    TaskProviderBinding,
    TaskSyncConflict,
)
from docket.schemas.native_tasks import TASK_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("format", ["json", "csv"])
@pytest.mark.asyncio
async def test_task_exports_keep_full_text_across_bounded_replayable_chunks(
    session_factory, linked_plugin, format,
):
    app, _settings, token, _principal = linked_plugin
    expected = []
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        for index in range(3):
            title = (f"Export {index}: " + "東京 🧪" * 256)[:1024]
            notes = ('Work, "quoted"\n東京 🧪\t' * 1000)[:16000]
            created = (await client.call_tool("docket_create_task", {
                "context_id": context["context_id"], "operation_id": f"export-task-{index}",
                "title": title, "notes": notes,
            })).structured_content
            assert created["disposition"] == "committed", created
            ref = next(ref for ref in created["affected_refs"] if ref.startswith("task_"))
            expected.append((ref, title, notes))
        arguments = {"format": format, "limit": 2}
        first = (await client.call_tool("docket_query_tasks", arguments)).structured_content
        assert first["total_matching"] == 3 and first["next_cursor"]
        # The rest of this captured page must survive a later real command edit.
        selected = (await client.call_tool("docket_get_context", {
            "refs": [expected[0][0]],
        })).structured_content
        edited = (await client.call_tool("docket_update_task", {
            "context_id": selected["context_id"], "operation_id": "edit-during-export",
            "task_ref": expected[0][0], "notes": "Changed after the export started",
        })).structured_content
        assert edited["disposition"] == "committed", edited
        wrong = (await client.call_tool("docket_query_tasks", {
            **arguments, "text": "changed filters", "cursor": first["next_cursor"],
        })).structured_content
        assert wrong["error"]["code"] == "task_cursor_invalid"
        content, packet, count = "", first, 0
        while True:
            assert len(json.dumps(packet).encode()) <= 16384
            content += packet["content"]
            count += 1
            assert count < 100
            if not packet.get("next_cursor"):
                break
            continuation = {**arguments, "cursor": packet["next_cursor"]}
            packet = (await client.call_tool("docket_query_tasks", continuation)).structured_content
            replay = (await client.call_tool("docket_query_tasks", continuation)).structured_content
            assert packet["content"] == replay["content"]
        assert count > 3
        rows = (json.loads(content) if format == "json" else
                list(csv.DictReader(io.StringIO(content))))
        assert [(row["ref"], row["title"], row["notes"]) for row in rows] == expected
        assert all(len(row["title"]) == 1024 and len(row["notes"]) == 16000 for row in rows)
        empty = (await client.call_tool("docket_query_tasks", {
            **arguments, "text": "no matching task",
        })).structured_content
        assert empty.get("next_cursor") is None and empty["total_matching"] == 0
        assert (json.loads(empty["content"]) if format == "json" else
                list(csv.DictReader(io.StringIO(empty["content"])))) == []
    with session_factory() as session:
        changed = session.scalar(select(Task).where(Task.ref_id == expected[0][0]))
        assert changed.description == "Changed after the export started"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_flat_task_list_configuration_logging_and_completion(session_factory, linked_plugin):
    app, settings, token, principal = linked_plugin
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google", external_account_id="synthetic-tasks", capabilities=["google_tasks"],
        )
        session.add(account)
        session.flush()
        listing = GoogleTaskList(
            operator_ref=settings.canonical_operator_ref, account_id=account.id,
            provider_list_id="synthetic-list", title="Everyday tasks",
        )
        session.add(listing)
        session.flush()
        list_ref = listing.ref_id
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(TASK_TOOL_PURPOSES) <= set(tools)
        assert len(tools) == 114
        for name in TASK_TOOL_PURPOSES:
            assert len(json.dumps(tools[name].input_schema).encode()) <= 4096, name
            assert len(json.dumps(tools[name].model_dump(mode="json")).encode()) <= 6144, name
        packet = (await client.call_tool("docket_get_tasks_context", {})).structured_content
        assert packet["task_lists"][0]["ref"] == list_ref
        configured = (await client.call_tool("docket_configure_task_list", {
            "context_id": packet["context_id"], "operation_id": "configure-tasks",
            "task_list_ref": list_ref, "mode": "two_way", "is_default": True,
        })).structured_content
        assert configured.get("disposition") == "committed", configured
        created = (await client.call_tool("docket_create_task", {
            "context_id": packet["context_id"], "operation_id": "flat-task",
            "title": "Prepare report", "scheduled_date": "2026-11-13",
        })).structured_content
        assert created.get("disposition") == "committed", created
        found = (await client.call_tool("docket_query_tasks", {
            "task_list_ref": list_ref, "text": "Prepare",
        })).structured_content
        assert found["total_matching"] == 1
        task_ref = found["tasks"][0]["ref"]
        updated = (await client.call_tool("docket_update_task", {
            "context_id": found["context_id"], "operation_id": "complete-flat-task",
            "task_ref": task_ref, "state": "completed",
        })).structured_content
        assert updated.get("disposition") == "committed", updated
        refreshed = (await client.call_tool("docket_refresh_tasks", {
            "task_list_ref": list_ref,
        })).structured_content
        assert refreshed["refresh"] == "queued"
    with session_factory() as session:
        task = session.scalar(select(Task))
        assert task.task_list_ref == list_ref and task.task_state == "completed"
        assert task.completed_at is not None
        policy = session.scalar(select(TaskListPolicyRevision))
        assert policy.principal_ref == principal
        assert policy.authorized_request_ref.startswith("req_")


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_link_resolve_fields_independently_and_detach(session_factory, linked_plugin):
    app, settings, token, _ = linked_plugin
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google", external_account_id="synthetic-tasks", capabilities=["google_tasks"],
        )
        session.add(account)
        session.flush()
        listing = GoogleTaskList(
            operator_ref=settings.canonical_operator_ref, account_id=account.id,
            provider_list_id="synthetic-list", title="Validation",
        )
        session.add(listing)
        session.flush()
        list_ref = listing.ref_id
        observation = GoogleTaskObservation(
            task_list_ref=list_ref, provider_task_id="synthetic-task", provider_etag='"observed"',
            source_hash="2" * 64, inventory_generation=uuid.uuid4(),
            fields={"title": "Google title", "notes": "Google notes", "status": "needsAction"},
        )
        session.add(observation)
        session.flush()
        observation_ref = observation.ref_id
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        packet = (await client.call_tool("docket_get_tasks_context", {})).structured_content
        configured = (await client.call_tool("docket_configure_task_list", {
            "context_id": packet["context_id"], "operation_id": "policy",
            "task_list_ref": list_ref, "mode": "two_way",
        })).structured_content
        assert configured.get("disposition") == "committed", configured
        created = (await client.call_tool("docket_create_task", {
            "context_id": packet["context_id"], "operation_id": "local-task",
            "title": "Local title", "notes": "Local notes", "task_list_ref": list_ref,
        })).structured_content
        assert created.get("disposition") == "committed", created
        with session_factory() as session:
            task_ref = session.scalar(select(Task.ref_id))
        packet = (await client.call_tool("docket_get_tasks_context", {
            "refs": [task_ref, observation_ref],
        })).structured_content
        linked = (await client.call_tool("docket_link_google_task", {
            "context_id": packet["context_id"], "operation_id": "link",
            "task_ref": task_ref, "observation_ref": observation_ref,
        })).structured_content
        assert linked.get("disposition") == "committed", linked
        with session_factory() as session:
            conflict_ref = session.scalar(select(TaskSyncConflict.ref_id))
        for field in ("title", "notes"):
            packet = (await client.call_tool("docket_get_tasks_context", {
                "refs": [conflict_ref],
            })).structured_content
            resolved = (await client.call_tool("docket_resolve_task_sync", {
                "context_id": packet["context_id"], "operation_id": "resolve-" + field,
                "conflict_ref": conflict_ref, "choose": "google", "fields": [field],
            })).structured_content
            assert resolved.get("disposition") == "committed", resolved
        packet = (await client.call_tool("docket_get_tasks_context", {
            "refs": [task_ref],
        })).structured_content
        detached = (await client.call_tool("docket_link_google_task", {
            "context_id": packet["context_id"], "operation_id": "detach",
            "task_ref": task_ref, "detach": True,
        })).structured_content
        assert detached.get("disposition") == "committed", detached
    with session_factory() as session:
        task = session.scalar(select(Task))
        assert task.title == "Google title" and task.description == "Google notes"
        assert set(session.scalars(select(TaskConflictResolution.field_name))) == {"title", "notes"}
        binding = session.scalar(select(TaskProviderBinding))
        assert not binding.active and binding.detached
