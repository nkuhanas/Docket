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
        assert "work" not in schema["properties"] and "$defs" not in schema
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
