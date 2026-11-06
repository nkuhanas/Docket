"""Tasks remain small, authenticated native workflows rather than compiler payloads."""

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
