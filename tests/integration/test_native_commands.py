import json

import httpx2
import pytest
import test_native_plugin
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from docket.domain.errors import DocketError
from docket.models import (
    AuthenticatedRequest,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    Item,
    NativeContext,
    Operation,
    PreparedCommand,
    ProviderAccount,
    TemporalBinding,
    ToolInvocation,
)
from docket.services.native_commands import event_timing

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.parametrize("prefix", [
    "ent", "idn", "aff", "rel", "fact", "pref", "lane", "item", "time", "task", "tproj", "rem",
])
def test_shared_retraction_preserves_each_existing_domain_plan(session, monkeypatch, prefix):
    from docket.config import get_settings
    from docket.domain.public_refs import new_public_ref
    from docket.schemas.business import RETRACTION_BY_PREFIX
    from docket.services.business_commands import BusinessCommandService

    target = new_public_ref(prefix)
    packet = NativeContext(snapshot={"objects": [{"ref": target, "state": {}}]},
                           versions={target: 7})
    service = BusinessCommandService(session, get_settings())
    monkeypatch.setattr(service, "execute", lambda **kwargs: kwargs["build"](packet))
    arguments = {"context_id": new_public_ref("nctx"), "operation_id": "retract",
                 "target_ref": target}
    original = service.perform(RETRACTION_BY_PREFIX[prefix], arguments, "req", "request")
    shared = service.retract_record(arguments, "req", "request")
    assert shared["patch"] == original["patch"]
    assert shared["expected_versions"] == original["expected_versions"] == {target: 7}
    assert shared["assembly_scope"]["allowed_mutation_types"] == (
        original["assembly_scope"]["allowed_mutation_types"]
    )
    assert shared["assembly_scope"]["target_refs"] == [target]


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_shared_retraction_transport_keeps_history_retry_and_retires_aliases(
    session_factory, linked_plugin,
):
    from docket.plugin.server import PluginMCP
    from docket.schemas.business import RETRACTION_BY_PREFIX, public_effect_tools

    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        retired = {definition.tool_name for definition in RETRACTION_BY_PREFIX.values()} | {
            "docket_complete_task", "docket_get_conflict",
        }
        assert retired.isdisjoint(tools)
        registered = {tool.name for tool in PluginMCP(_settings)._tool_manager.list_tools()}
        assert retired.isdisjoint(registered)
        assert set(public_effect_tools().values()) <= set(tools)
        assert len(json.dumps(tools["docket_retract_record"].input_schema).encode()) <= 1024
        overview = (await client.call_tool("docket_get_overview", {})).structured_content
        for index in range(2):
            created = (await client.call_tool("docket_create_item", {
                "context_id": overview["context_id"], "operation_id": f"create-{index}",
                "title": f"Retraction sample {index}", "kind": "test.retraction",
            })).structured_content
            assert created.get("disposition") == "committed", created
            with session_factory() as session:
                item_ref = session.scalar(select(Item.ref_id).where(
                    Item.title == f"Retraction sample {index}",
                ))
            packet = (await client.call_tool("docket_get_context", {
                "refs": [item_ref],
            })).structured_content
            assert "docket_retract_record" in packet["objects"][0]["actions"]
            assert "docket_retract_item" not in packet["objects"][0]["actions"]
            args = {"context_id": packet["context_id"], "operation_id": f"retract-{index}",
                    "target_ref": item_ref}
            receipt = (await client.call_tool("docket_retract_record", args)).structured_content
            assert receipt.get("disposition") == "committed", receipt
            retry = (await client.call_tool("docket_retract_record", args)).structured_content
            assert retry["replayed"] and retry["changeset_ref"] == receipt["changeset_ref"]
    with session_factory() as session:
        assert set(session.scalars(select(Item.canonical_status))) == {"retracted"}
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        assert session.scalar(select(func.count()).select_from(ChangeSet)) == 4
        assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 4


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_flat_event_first_attempt_and_retry_through_real_transport(
    session_factory, linked_plugin
):
    app, _settings, token, _principal = linked_plugin
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="synthetic-owner",
            capabilities=["google_calendar"],
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id,
            lane="meetings",
            display_name="Meetings",
            color_hex="#123456",
            calendar_id="synthetic-meetings",
            created_by_changeset_ref="test-fixture",
            status="active",
        )
        session.add(lane)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        schema = tools["docket_create_event"].input_schema
        assert len(json.dumps(schema).encode()) < 4096
        assert "work" not in schema["properties"]
        assert "mutation_type" not in json.dumps(schema)
        context = (await client.call_tool("docket_get_calendar_context", {})).structured_content
        assert context["coverage"]["absence_proven"] is False
        assert len(json.dumps(context).encode()) < 8192
        arguments = {
            "context_id": context["context_id"],
            "operation_id": "synthetic-meeting",
            "title": "Example meeting",
            "start": "2026-11-13T13:00:00-08:00",
            "end": "2026-11-13T14:00:00-08:00",
            "timezone": "America/Los_Angeles",
            "calendar_ref": context["calendars"][0]["calendar_ref"],
            "location": "https://example.test/meeting",
            "notes": "Reported participants",
        }
        first = (await client.call_tool("docket_create_event", arguments)).structured_content
        assert first.get("disposition") == "committed", first
        replay = (await client.call_tool("docket_create_event", arguments)).structured_content
        assert replay.pop("replayed") is True
        assert replay == first
        mismatch = (
            await client.call_tool(
                "docket_create_event",
                {
                    **arguments,
                    "title": "Different intent",
                },
            )
        ).structured_content
        assert mismatch["error"]["code"] == "operation_id_reused"
    with session_factory() as session:
        for model in (CanonicalEvent, Item, TemporalBinding, ChangeSet, PreparedCommand, Operation):
            assert session.scalar(select(func.count()).select_from(model)) == 1, model
        root = session.scalar(select(AuthenticatedRequest))
        event = session.scalar(select(CanonicalEvent))
        assert root.state == "committed" and root.ref_id in event.basis_refs
        assert root.source_utterance_ref is None
        assert session.scalar(select(func.count()).select_from(NativeContext)) == 1
        calls = list(session.scalars(select(ToolInvocation)))
        assert len(calls) == 4
        assert all(row.authenticated_request_ref == root.ref_id for row in calls[1:])


@pytest.mark.parametrize(
    "start,end,timezone,field,code",
    [
        (
            "2026-11-13T13:00:00-07:00",
            "2026-11-13T14:00:00-07:00",
            "America/Los_Angeles",
            "start",
            "timezone_offset_conflict",
        ),
        (
            "2026-03-08T02:30:00",
            "2026-03-08T04:00:00",
            "America/Los_Angeles",
            "start",
            "ambiguous_or_nonexistent_time",
        ),
        (
            "2026-11-01T01:30:00",
            "2026-11-01T02:30:00",
            "America/Los_Angeles",
            "start",
            "ambiguous_or_nonexistent_time",
        ),
        (
            "2026-11-13T14:00:00-08:00",
            "2026-11-13T13:00:00-08:00",
            "America/Los_Angeles",
            "end",
            "invalid_event_end",
        ),
        (
            "2026-11-13T14:00:00",
            "2026-11-13T15:00:00",
            "Invalid/Zone",
            "timezone",
            "invalid_timezone",
        ),
    ],
)
def test_public_timing_errors_identify_affected_field(start, end, timezone, field, code):
    with pytest.raises(DocketError) as caught:
        event_timing(start, end, timezone)
    assert caught.value.code == code and caught.value.details["field"] == field


def test_offset_disambiguates_dst_fold():
    timing = event_timing(
        "2026-11-01T01:10:00-08:00", "2026-11-01T01:40:00-08:00", "America/Los_Angeles"
    )
    assert timing["fold"] == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("caller", ["read_only", "other_owner"])
@pytest.mark.asyncio
async def test_context_possession_cannot_grant_write_or_cross_owner_access(
    session_factory,
    linked_plugin,
    caller,
):
    from docket.services.oauth import OAuthPrincipalService

    app, settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        # An empty context is still an owned snapshot, never permission to create a destination.
        context = (await client.call_tool("docket_get_calendar_context", {})).structured_content
        if caller == "read_only":
            replacement = app.state.test_issue_token(scope="docket.read")
        else:
            with session_factory.begin() as session:
                OAuthPrincipalService(session, settings).approve("other-owner", write=True)
            replacement = app.state.test_issue_token(sub="other-owner")
        http.headers["Authorization"] = "Bearer " + replacement
        arguments = {
            "context_id": context["context_id"],
            "operation_id": "borrowed-context",
            "title": "Attempted event",
            "start": "2026-11-13T13:00:00-08:00",
            "end": "2026-11-13T14:00:00-08:00",
            "timezone": "America/Los_Angeles",
            "calendar_ref": "lane_00000000000000000000000000",
        }
        result = (await client.call_tool("docket_create_event", arguments)).structured_content
        assert result["error"]["code"] == (
            "agent_authority_denied" if caller == "read_only" else "context_unavailable"
        )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 0
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        calls = list(session.scalars(select(ToolInvocation)))
        assert len(calls) == 2 and calls[1].domain_state == "rejected"


def test_context_is_immutable_purpose_bound_and_has_no_ttl(session):
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.config import get_settings
    from docket.services.native_context import NativeContextService

    settings = get_settings()
    principal = AgentPrincipal(
        "agent:context-owner", settings.canonical_operator_ref, "read_only", frozenset({"read"})
    )
    binding = agent_call_context.set(AgentCallContext(principal))
    try:
        service = NativeContextService(session, settings)
        packet = service.retain("calendar", {"coverage": "incomplete"}, {})
        row = service.require(packet["context_id"], "calendar")
        assert row.ref_id == packet["context_id"] and not hasattr(row, "expires_at")
        with pytest.raises(DocketError) as error:
            service.require(row.ref_id, "overview")
        assert error.value.code == "context_unavailable"
        row.purpose = "overview"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()
    finally:
        agent_call_context.reset(binding)


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_named_registry_commands_keep_sparse_fields_and_context_versions(
    session_factory,
    linked_plugin,
):
    from docket.models import Entity

    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        created = (
            await client.call_tool(
                "docket_create_entity",
                {
                    "context_id": context["context_id"],
                    "operation_id": "person-create",
                    "entity_kind": "person",
                    "display_name": "Example Person",
                },
            )
        ).structured_content
        assert created.get("disposition") == "committed", created
        with session_factory() as session:
            entity_ref = session.scalar(
                select(Entity.ref_id).where(Entity.display_name == "Example Person")
            )
        selected = (
            await client.call_tool("docket_get_context", {"refs": [entity_ref]})
        ).structured_content
        assert "docket_update_entity" in selected["objects"][0]["actions"]
        updated = (
            await client.call_tool(
                "docket_update_entity",
                {
                    "context_id": selected["context_id"],
                    "operation_id": "person-update",
                    "target_ref": entity_ref,
                    "display_name": "New Name",
                },
            )
        ).structured_content
        assert updated.get("disposition") == "committed", json.dumps(updated, indent=2)
        refreshed = (
            await client.call_tool(
                "docket_get_context",
                {
                    "refs": [entity_ref],
                },
            )
        ).structured_content
        fact = (
            await client.call_tool(
                "docket_create_fact",
                {
                    "context_id": refreshed["context_id"],
                    "operation_id": "person-fact",
                    "subject_ref": entity_ref,
                    "predicate": "home_city",
                    "value_json": "Exampleton",
                },
            )
        ).structured_content
        assert fact.get("disposition") == "committed", fact
        preference = (
            await client.call_tool(
                "docket_create_preference",
                {
                    "context_id": refreshed["context_id"],
                    "operation_id": "person-preference",
                    "preference_key": "example-person-contact",
                    "policy_kind": "behavior",
                    "target_type": "entity",
                    "target_ref": entity_ref,
                    "policy_text": "Prefer concise status updates.",
                },
            )
        ).structured_content
        assert preference.get("disposition") == "committed", preference
    with session_factory() as session:
        entity = session.scalar(select(Entity).where(Entity.ref_id == entity_ref))
        assert entity.display_name == "New Name" and entity.canonical_status == "active"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_task_due_and_completion_use_derived_records_and_current_versions(
    session_factory,
    linked_plugin,
):
    from docket.models import Task

    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        overview = (await client.call_tool("docket_get_overview", {})).structured_content
        created = (
            await client.call_tool(
                "docket_create_task",
                {
                    "context_id": overview["context_id"],
                    "operation_id": "task-create",
                    "title": "A" * 1024,
                    "due": "2026-11-13",
                    "priority": "high",
                },
            )
        ).structured_content
        assert created.get("disposition") == "committed", created
        with session_factory() as session:
            task_ref = session.scalar(select(Task.ref_id))
        context = (
            await client.call_tool("docket_get_context", {"refs": [task_ref]})
        ).structured_content
        assert {obj["type"] for obj in context["objects"]} == {"task", "temporal_binding"}
        arguments = {
            "context_id": context["context_id"],
            "operation_id": "task-complete",
            "task_ref": task_ref,
            "state": "completed",
        }
        completed = (await client.call_tool("docket_update_task", arguments)).structured_content
        assert completed.get("disposition") == "committed", completed
        retry = (await client.call_tool("docket_update_task", arguments)).structured_content
        assert retry.pop("replayed") is True and retry == completed
        stale = (
            await client.call_tool(
                "docket_update_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "task-stale",
                    "task_ref": task_ref,
                    "title": "Stale edit",
                },
            )
        ).structured_content
        assert stale.get("ok") is False
    with session_factory() as session:
        task = session.scalar(select(Task))
        assert task.task_state == "completed" and task.completed_at is not None
        assert task.title == "A" * 1024 and task.priority == "high"
        assert session.scalar(select(Item.title)) == "A" * 1024
        assert session.scalar(select(func.count()).select_from(Item)) == 1
        assert session.scalar(select(func.count()).select_from(TemporalBinding)) == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_derived_title_renames_stop_after_independent_same_title_edit(
    session_factory, linked_plugin,
):
    from docket.models import Task
    from docket.models.tracked_context import TaskItemTitleOwnership

    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        created = (await client.call_tool("docket_create_task", {
            "context_id": context["context_id"], "operation_id": "owned-task",
            "title": "Original",
        })).structured_content
        assert created.get("disposition") == "committed", created
        with session_factory() as session:
            task = session.scalar(select(Task))
            task_ref, item_ref = task.ref_id, task.item_ref
            owner = session.get(TaskItemTitleOwnership, item_ref)
            assert owner.task_ref == task_ref and owner.relinquished_at is None

        for index, (tool, fields) in enumerate([
            ("docket_update_task", {"task_ref": task_ref, "title": "Renamed"}),
            ("docket_update_item", {"target_ref": item_ref, "title": "Renamed"}),
            ("docket_update_task", {"task_ref": task_ref, "title": "Task only"}),
        ]):
            packet = (await client.call_tool("docket_get_context", {
                "refs": [task_ref, item_ref],
            })).structured_content
            result = (await client.call_tool(tool, {
                "context_id": packet["context_id"], "operation_id": f"rename-{index}", **fields,
            })).structured_content
            assert result.get("disposition") == "committed", result
            with session_factory() as session:
                item = session.scalar(select(Item).where(Item.ref_id == item_ref))
                assert item.title == "Renamed"
                owner = session.get(TaskItemTitleOwnership, item_ref)
                assert (owner.relinquished_at is None) == (index == 0)
        with session_factory() as session:
            assert session.scalar(select(Task.title)) == "Task only"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_event_edit_and_cancel_use_observed_versions_and_sparse_fields(
    session_factory,
    linked_plugin,
):
    from docket.providers.google.fake_calendar import FakeCalendarProvider
    from docket.services.operations import OperationRunner

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider, consistency_window_seconds=0)
    app, _settings, token, _principal = linked_plugin
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="event-edit-owner",
            capabilities=["google_calendar"],
        )
        session.add(account)
        session.flush()
        session.add(
            CalendarLane(
                account_id=account.id,
                lane="meetings",
                display_name="Meetings",
                color_hex="#123456",
                calendar_id="synthetic-edit-calendar",
                status="active",
                created_by_changeset_ref="test-fixture",
            )
        )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        choices = (await client.call_tool("docket_get_calendar_context", {})).structured_content
        created = (
            await client.call_tool(
                "docket_create_event",
                {
                    "context_id": choices["context_id"],
                    "operation_id": "editable-event",
                    "title": "Original title",
                    "start": "2026-11-13T13:00:00-08:00",
                    "end": "2026-11-13T14:00:00-08:00",
                    "timezone": "America/Los_Angeles",
                    "calendar_ref": choices["calendars"][0]["calendar_ref"],
                    "location": "Room A",
                    "notes": "Keep these notes",
                },
            )
        ).structured_content
        assert created.get("disposition") == "committed", created
        provider.next_create_outcome = "unknown_after_write"
        assert runner.run_due_once()
        assert not runner.run_due_once()  # Unknown writes reconcile rather than resend.
        assert len(provider.events) == 1
        assert runner.reconcile_once()

        with session_factory() as session:
            ref = session.scalar(select(CanonicalEvent.ref_id))
        context = (await client.call_tool("docket_get_context", {"refs": [ref]})).structured_content
        updated = (
            await client.call_tool(
                "docket_update_event",
                {
                    "context_id": context["context_id"],
                    "operation_id": "event-edit",
                    "event_ref": ref,
                    "title": "Updated title",
                    "location": None,
                },
            )
        ).structured_content
        assert updated.get("disposition") == "committed", json.dumps(updated, indent=2)
        assert runner.run_due_once()  # Apply the guarded sparse provider patch.
        stale = (
            await client.call_tool(
                "docket_cancel_event",
                {
                    "context_id": context["context_id"],
                    "operation_id": "event-stale",
                    "event_ref": ref,
                },
            )
        ).structured_content
        assert stale.get("ok") is False
        current = (await client.call_tool("docket_get_context", {"refs": [ref]})).structured_content
        cancelled = (
            await client.call_tool(
                "docket_cancel_event",
                {
                    "context_id": current["context_id"],
                    "operation_id": "event-cancel",
                    "event_ref": ref,
                },
            )
        ).structured_content
        assert cancelled.get("disposition") == "committed", cancelled
        assert runner.run_due_once()
        assert len(provider.events) == 0

    with session_factory() as session:
        event = session.scalar(select(CanonicalEvent))
        assert event.status == "cancelled" and event.title == "Updated title"
        assert event.event_spec["notes"] == "Keep these notes"
        assert event.event_spec["location"] is None
        assert event.event_spec["timing"]["start_local"] == "2026-11-13T13:00:00"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_preview_correction_preserves_root_and_old_handle_never_applies_latest(
    session_factory,
    linked_plugin,
):
    from docket.models import Task

    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        first = (
            await client.call_tool(
                "docket_create_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "task-preview-1",
                    "title": "First reading",
                    "preview": True,
                },
            )
        ).structured_content
        assert first["disposition"] == "prepared", first
        corrected = (
            await client.call_tool(
                "docket_create_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "task-preview-2",
                    "title": "Corrected reading",
                    "preview": True,
                    "replaces_change_id": first["change_id"],
                },
            )
        ).structured_content
        assert corrected["disposition"] == "prepared", corrected
        assert corrected["change_id"] != first["change_id"]
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Task)) == 0
            proposals = list(session.scalars(select(PreparedCommand)))
            assert len(proposals) == 2
            assert proposals[0].request_ref == proposals[1].request_ref
            assert proposals[0].revision < proposals[1].revision
        old = (
            await client.call_tool(
                "docket_read_change",
                {
                    "change_id": first["change_id"],
                },
            )
        ).structured_content
        assert old["stale"] and old["preview"]["title"] == "First reading"
        rejected = (
            await client.call_tool(
                "docket_apply_change",
                {
                    "change_id": first["change_id"],
                },
            )
        ).structured_content
        assert rejected["error"]["code"] == "prepared_change_stale"
        applied = (
            await client.call_tool(
                "docket_apply_change",
                {
                    "change_id": corrected["change_id"],
                },
            )
        ).structured_content
        assert applied["disposition"] == "committed", applied
        retry = (
            await client.call_tool(
                "docket_apply_change",
                {
                    "change_id": corrected["change_id"],
                },
            )
        ).structured_content
        assert retry.pop("replayed") is True and retry == applied
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(Task.title)) == "Corrected reading"
        assert session.scalar(select(func.count()).select_from(AuthenticatedRequest)) == 1


@pytest.mark.asyncio
async def test_named_contracts_cover_current_effects_without_public_compiler_bookkeeping(
    linked_plugin,
):
    from docket.plugin.instructions import NATIVE_INSTRUCTIONS
    from docket.schemas.business import (
        BUSINESS_DEFINITIONS,
        CANONICAL_MUTATIONS,
        SPECIAL_MUTATIONS,
        public_business_tool,
    )

    app, _settings, _token, _principal = linked_plugin
    tools = {tool.name: tool for tool in await app.state.mcp.list_tools()}
    covered = {
        definition.mutation.model_fields["mutation_type"].default
        for definition in BUSINESS_DEFINITIONS
    } | SPECIAL_MUTATIONS
    assert covered == {model.model_fields["mutation_type"].default for model in CANONICAL_MUTATIONS}
    assert len(NATIVE_INSTRUCTIONS.encode()) <= 2048

    def check(schema):
        if isinstance(schema, dict):
            for name in schema.get("properties", {}):
                assert name not in {
                    "work",
                    "mutation_type",
                    "create_spec",
                    "payload",
                    "basis_refs",
                    "affected_fields",
                    "expected_versions",
                }
                assert (
                    not name.endswith(("_change_id", "_change_ids")) or name == "replaces_change_id"
                )
            for value in schema.values():
                check(value)
        elif isinstance(schema, list):
            for value in schema:
                check(value)

    for definition in BUSINESS_DEFINITIONS:
        check(tools[public_business_tool(definition)].input_schema)
    # Required references remain required after removing internal create-ID alternatives.
    assert "subject_ref" in tools["docket_create_fact"].input_schema["required"]


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.parametrize("all_day", [True, False])
@pytest.mark.asyncio
async def test_event_creation_retains_all_day_and_recurring_capabilities(
    session_factory,
    linked_plugin,
    all_day,
):
    app, _settings, token, _principal = linked_plugin
    with session_factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="calendar-capability",
            capabilities=["google_calendar"],
        )
        session.add(account)
        session.flush()
        session.add(
            CalendarLane(
                account_id=account.id,
                lane="meetings",
                display_name="Meetings",
                color_hex="#123456",
                calendar_id="synthetic-calendar",
                status="active",
                created_by_changeset_ref="test-fixture",
            )
        )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client(
                "http://docket.you/mcp/",
                http_client=http,
            ),
            mode="legacy",
        ) as client,
    ):
        context = (await client.call_tool("docket_get_calendar_context", {})).structured_content
        arguments = {
            "context_id": context["context_id"],
            "operation_id": "calendar-capability",
            "title": "Synthetic calendar entry",
            "timezone": "America/Los_Angeles",
            "calendar_ref": context["calendars"][0]["calendar_ref"],
            "all_day": all_day,
            "context_labels": ["synthetic"],
        }
        arguments.update(
            {"start": "2026-11-13", "end": "2026-11-14"}
            if all_day
            else {
                "start": "2026-11-13T13:00:00-08:00",
                "end": "2026-11-13T14:00:00-08:00",
                "recurrence": {"frequency": "weekly", "weekdays": ["FR"], "count": 3},
            }
        )
        result = (await client.call_tool("docket_create_event", arguments)).structured_content
        assert result.get("disposition") == "committed", json.dumps(result, indent=2)
    with session_factory() as session:
        event = session.scalar(select(CanonicalEvent))
        assert event.context_labels == ["synthetic"]
        assert event.event_spec["timing"]["kind"] == ("all_day" if all_day else "timed")
        if all_day:
            assert event.event_spec["timing"]["end_date"] == "2026-11-14"
        else:
            assert event.event_spec["recurrence"]["count"] == 3
        assert session.scalar(select(func.count()).select_from(Item)) == 1
        assert session.scalar(select(func.count()).select_from(TemporalBinding)) == 1


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_batch_applies_once_and_members_cannot_duplicate_canonical_effects(
    session_factory,
    linked_plugin,
):
    from docket.models import PreparedConsumption, Task

    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as http,
        Client(
            streamable_http_client("http://docket.you/mcp/", http_client=http), mode="legacy"
        ) as client,
    ):
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        changes = []
        for index in range(2):
            prepared = (
                await client.call_tool(
                    "docket_create_task",
                    {
                        "context_id": context["context_id"],
                        "operation_id": f"batch-task-{index}",
                        "title": f"Batch task {index}",
                        "preview": True,
                    },
                )
            ).structured_content
            assert prepared.get("disposition") == "prepared", prepared
            changes.append(prepared["change_id"])
        arguments = {"operation_id": "atomic-task-batch", "change_ids": changes}
        batch = (await client.call_tool("docket_prepare_batch", arguments)).structured_content
        assert batch.get("disposition") == "prepared", batch
        again = (await client.call_tool("docket_prepare_batch", arguments)).structured_content
        assert again["change_id"] == batch["change_id"]
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Task)) == 0
        applied = (
            await client.call_tool(
                "docket_apply_change",
                {
                    "change_id": batch["change_id"],
                },
            )
        ).structured_content
        assert applied.get("disposition") == "committed", applied
        for ref in [batch["change_id"], *changes]:
            retry = (
                await client.call_tool(
                    "docket_apply_change",
                    {
                        "change_id": ref,
                    },
                )
            ).structured_content
            assert retry.get("replayed") is True, retry
            assert retry["changeset_ref"] == applied["changeset_ref"]
        member = (
            await client.call_tool(
                "docket_read_change",
                {
                    "change_id": changes[0],
                },
            )
        ).structured_content
        assert member["state"] == "included_in_batch"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 2
        assert session.scalar(select(func.count()).select_from(PreparedConsumption)) == 2
        assert (
            session.scalar(
                select(func.count()).select_from(ChangeSet).where(ChangeSet.state == "committed")
            )
            == 1
        )


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_clarification_and_saved_work_are_bounded_non_executable_and_owner_bound(
    session_factory,
    linked_plugin,
):
    from docket.services.oauth import OAuthPrincipalService

    app, settings, token, _principal = linked_plugin
    with session_factory.begin() as session:
        OAuthPrincipalService(session, settings).approve("other-owner", write=True)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), headers={"Authorization": "Bearer " + token}
        ) as http,
        Client(
            streamable_http_client("http://docket.you/mcp/", http_client=http), mode="legacy"
        ) as client,
    ):
        question = {
            "operation_id": "choose-time",
            "question": "Which time?",
            "choices": [
                {"option_id": "morning", "label": "Morning", "interpretation": {"time": "morning"}},
            ],
        }
        first = (
            await client.call_tool("docket_request_clarification", question)
        ).structured_content
        assert first.get("disposition") == "clarification_required", first
        replay = (
            await client.call_tool("docket_request_clarification", question)
        ).structured_content
        assert replay["replayed"] and replay["ref"] == first["ref"]
        changed = (
            await client.call_tool(
                "docket_request_clarification",
                {
                    **question,
                    "question": "Different question",
                },
            )
        ).structured_content
        assert changed["error"]["code"] == "operation_id_reused"
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        preview = (
            await client.call_tool(
                "docket_create_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "saved-task",
                    "title": "Saved task",
                    "preview": True,
                },
            )
        ).structured_content
        read = (
            await client.call_tool(
                "docket_read_change",
                {
                    "change_id": preview["change_id"],
                },
            )
        ).structured_content
        saved_args = {"saved_request_ref": read["request_ref"]}
        saved = (await client.call_tool("docket_read_saved_work", saved_args)).structured_content
        assert "Saved task" in saved["saved_json_chunk"] and saved["revision"] == 1, saved
        listed = (await client.call_tool("docket_list_saved_work", {})).structured_content
        assert read["request_ref"] in [row["request_ref"] for row in listed["requests"]]
        http.headers["Authorization"] = "Bearer " + app.state.test_issue_token(sub="other-owner")
        rejected = (await client.call_tool("docket_read_saved_work", saved_args)).structured_content
        assert rejected["error"]["code"] == "saved_work_unavailable"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_conflict_resolution_can_atomically_consume_an_exact_business_preview(
    session_factory,
    linked_plugin,
):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import Conflict, Task
    from docket.schemas.authority import ConflictOpen, StatementInput
    from docket.services.agent_requests import AgentRequestService
    from docket.services.conflicts import ConflictService
    from docket.services.statements import StatementService

    app, _settings, token, _principal = linked_plugin
    principal = app.user_middleware[0].kwargs["verifier"].authenticate(token, session_factory)
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        with session_factory.begin() as session:
            roots = [
                AgentRequestService(session).admit(request_key=key)
                for key in ("prior-time", "incoming-time")
            ]
            subject = "ent_01M13MZZZZZZZZZZZZZZZZZZZZ"
            statements = [
                StatementService(session).derive(
                    root.ref_id,
                    [
                        StatementInput(
                            statement_kind="fact_assertion",
                            subject_refs=[subject],
                            predicate="office_hours",
                            value_json=value,
                            affected_fields=["office_hours"],
                            interpreter_version="test-v1",
                        )
                    ],
                )[0]
                for root, value in zip(roots, ("Monday", "Wednesday"), strict=True)
            ]
            prior_ref, incoming_ref = [row.ref_id for row in statements]
            conflict_ref = (
                ConflictService(session)
                .open(
                    ConflictOpen(
                        subject_refs=[subject],
                        affected_fields=["office_hours"],
                        prior_statement_refs=[prior_ref],
                        incoming_statement_refs=[incoming_ref],
                        conflicting_effects_json={"prior": "Monday", "incoming": "Wednesday"},
                    )
                )
                .ref_id
            )
    finally:
        agent_call_context.reset(context_token)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), headers={"Authorization": "Bearer " + token}
        ) as http,
        Client(
            streamable_http_client("http://docket.you/mcp/", http_client=http), mode="legacy"
        ) as client,
    ):
        context = (await client.call_tool("docket_get_overview", {})).structured_content
        preview = (
            await client.call_tool(
                "docket_create_task",
                {
                    "context_id": context["context_id"],
                    "operation_id": "conflict-task",
                    "title": "Resolved work",
                    "preview": True,
                },
            )
        ).structured_content
        conflict = (
            await client.call_tool(
                "docket_get_context",
                {
                    "refs": [conflict_ref],
                },
            )
        ).structured_content
        arguments = {
            "context_id": conflict["context_id"],
            "operation_id": "resolve-time",
            "conflict_ref": conflict_ref,
            "resolution": "resolved_supersession",
            "chosen_interpretation": {"office_hours": "Wednesday"},
            "statements_superseded": [prior_ref],
            "statements_retained": [incoming_ref],
            "change_id": preview["change_id"],
        }
        resolved = (await client.call_tool("docket_resolve_conflict", arguments)).structured_content
        assert resolved.get("disposition") == "committed", resolved
        replay = (await client.call_tool("docket_resolve_conflict", arguments)).structured_content
        assert replay["replayed"] and replay["ref"] == resolved["ref"]
        member = (
            await client.call_tool(
                "docket_apply_change",
                {
                    "change_id": preview["change_id"],
                },
            )
        ).structured_content
        assert member["replayed"] and member["ref"] == resolved["ref"], member
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(Conflict.status)) == "resolved_supersession"


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_attention_resolution_blocks_only_affected_case_and_keeps_request_attribution(
    session_factory,
    linked_plugin,
):
    from docket.models import AttentionCase, AttentionCaseRevision, CaseItem, Decision
    from docket.models.base import utc_now

    app, _settings, token, principal_ref = linked_plugin
    with session_factory.begin() as session:
        case = AttentionCase(
            situation_key="synthetic-case",
            title="Synthetic case",
            summary="Case",
            first_observed_at=utc_now(),
            last_observed_at=utc_now(),
        )
        session.add(case)
        session.flush()
        items = [
            CaseItem(
                attention_case_id=case.id,
                item_key=role,
                item_type="decision_required",
                resolution_role=role,
            )
            for role in ("required", "supporting")
        ]
        session.add_all(items)
        session.flush()
        case_ref = case.ref_id
        item_ref = items[0].ref_id
        session.add(
            AttentionCaseRevision(
                attention_case_id=case.id,
                case_ref=case.ref_id,
                revision=1,
                title=case.title,
                summary=case.summary,
                case_item_refs=[row.ref_id for row in items],
                admission_rule_ref="synthetic",
                admission_basis_refs=[],
                required_case_item_refs=[item_ref],
                canonical_consequence_classes=[],
                content_hash="a" * 64,
            )
        )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), headers={"Authorization": "Bearer " + token}
        ) as http,
        Client(
            streamable_http_client("http://docket.you/mcp/", http_client=http), mode="legacy"
        ) as client,
    ):
        context = (
            await client.call_tool(
                "docket_get_context",
                {
                    "refs": [case_ref],
                },
            )
        ).structured_content
        args = {
            "context_id": context["context_id"],
            "case_ref": case_ref,
            "outcome": "resolved",
            "operation_id": "case-resolve",
        }
        blocked = (await client.call_tool("docket_resolve_attention_case", args)).structured_content
        assert blocked.get("ok") is False, blocked
        overview = (await client.call_tool("docket_get_overview", {})).structured_content
        unrelated = (
            await client.call_tool(
                "docket_create_task",
                {
                    "context_id": overview["context_id"],
                    "operation_id": "independent-task",
                    "title": "Unrelated work",
                },
            )
        ).structured_content
        assert unrelated.get("disposition") == "committed", unrelated
        resolved = (
            await client.call_tool(
                "docket_resolve_attention_case",
                {
                    **args,
                    "items": [{"case_item_ref": item_ref, "disposition": "resolved"}],
                },
            )
        ).structured_content
        assert resolved.get("disposition") == "committed", resolved
    with session_factory() as session:
        assert session.scalar(select(AttentionCase.status)) == "resolved"
        assert {row.status for row in session.scalars(select(CaseItem))} == {
            "resolved",
            "not_pursued",
        }
        assert (
            session.scalar(
                select(Decision.actor_ref).where(
                    Decision.decision_kind == "attention_case_resolution"
                )
            )
            == principal_ref
        )
