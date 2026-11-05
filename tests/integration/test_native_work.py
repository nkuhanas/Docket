"""Actual MCP journeys for typed work, not fabricated calendar attendance."""

import json

import httpx2
import pytest
import test_native_plugin
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from docket.models import (
    AuthenticatedRequest,
    CanonicalEvent,
    Engagement,
    EngagementRevision,
    Item,
    Operation,
    ToolInvocation,
    WorkSegment,
    WorkSession,
    WorkSessionRevision,
)
from docket.schemas.work import WORK_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
@pytest.mark.asyncio
async def test_flat_work_engagement_correction_preview_and_retry(session_factory, linked_plugin):
    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        async def call(name, arguments):
            result = (await client.call_tool(name, arguments)).structured_content
            assert result is not None
            return result

        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        additional_bytes = 0
        for name in WORK_TOOL_PURPOSES:
            tool = tools[name]
            schema = json.dumps(tool.input_schema, separators=(",", ":")).encode()
            assert len(schema) <= 4096, (name, len(schema))
            assert not {"mutation_type", "basis_refs", "request_ref", "work"}.intersection(
                tool.input_schema["properties"],
            )
            size = len(json.dumps(tool.model_dump(by_alias=True, exclude_none=True),
                                  separators=(",", ":")).encode())
            assert size <= 6144, (name, size)
            additional_bytes += size
        assert additional_bytes <= 20 * 1024
        packet = await call("docket_get_work_context", {})
        assert packet["objects"] == []
        created = await call("docket_create_engagement", {
            "context_id": packet["context_id"], "operation_id": "sample-arrangement",
            "label": "Example hardware work", "role": "Provisional role",
        })
        assert created.get("disposition") == "committed", created
        with session_factory() as session:
            eng = session.scalar(select(Engagement.ref_id))
        packet = await call("docket_get_work_context", {"refs": [eng]})
        assert packet["objects"][0]["state"]["kind"] == "unknown"
        corrected_engagement = await call("docket_update_engagement", {
            "context_id": packet["context_id"], "operation_id": "arrangement-correction",
            "engagement_ref": eng, "role": None,
        })
        assert corrected_engagement.get("disposition") == "committed", corrected_engagement
        packet = await call("docket_get_work_context", {"search": "hardware"})
        assert packet["objects"][0]["ref"] == eng
        assert "role" not in packet["objects"][0]["state"]
        args = {"context_id": packet["context_id"], "operation_id": "sample-work",
                "description": "Investigated device enumeration", "engagement_ref": eng,
                "start": "2026-11-01T01:30:00-07:00", "end": "2026-11-01T01:15:00-08:00",
                "timezone": "America/Los_Angeles", "reported_compensation_class": "billable",
                "breaks": [{"start": "2026-11-01T01:50:00-07:00",
                            "end": "2026-11-01T01:00:00-08:00"}],
                "work_mode": "onsite", "reported_location": "Example lab"}
        receipt = await call("docket_log_work_session", args)
        assert receipt.get("disposition") == "committed", receipt
        replay = await call("docket_log_work_session", args)
        assert replay["replayed"] and replay["changeset_ref"] == receipt["changeset_ref"]
        with session_factory() as session:
            work = session.scalar(select(WorkSession.ref_id))
        packet = await call("docket_get_work_context", {"refs": [work]})
        assert len(json.dumps(packet).encode()) <= 8192
        state = packet["objects"][0]["state"]
        assert state["duration_us"] == 35 * 60_000_000
        assert state["reported_compensation_class"] == "billable"
        original_revision = state["revision_ref"]
        update = {"context_id": packet["context_id"], "operation_id": "sample-correction",
                  "work_ref": work, "outcome": "Failure isolated", "reported_location": None,
                  "preview": True}
        prepared = await call("docket_update_work_session", update)
        assert prepared.get("change_id"), prepared
        with session_factory() as session:
            assert session.scalar(select(WorkSession.version)) == 1
        applied = await call("docket_apply_change", {"change_id": prepared["change_id"]})
        assert applied.get("disposition") == "committed", applied
        again = await call("docket_apply_change", {"change_id": prepared["change_id"]})
        assert again["replayed"]
        stale = await call("docket_update_work_session", {
            "context_id": packet["context_id"], "operation_id": "stale-correction",
            "work_ref": work, "description": "An outdated correction",
        })
        assert stale.get("ok") is False, stale
        current = await call("docket_get_context", {"refs": [work]})
        state = current["objects"][0]["state"]
        assert state["outcome"] == "Failure isolated" and "reported_location" not in state
        assert state["duration_us"] == 35 * 60_000_000
        original = await call("docket_get_history_entry", {
            "ref": original_revision, "view": "audit",
        })
        assert original["entry"]["state"]["reported_location"] == "Example lab"
        assert original["entry"]["state"]["time"]["duration_us"] == 35 * 60_000_000
        missing = await call("docket_log_work_session", {
            "context_id": current["context_id"], "operation_id": "incomplete-work",
            "description": "Unquantified investigation", "work_date": "2026-11-02",
            "attribution_label": "Unresolved employer",
        })
        assert missing.get("disposition") == "committed", missing
        duration = await call("docket_log_work_session", {
            "context_id": current["context_id"], "operation_id": "duration-work",
            "description": "Reported hours", "work_date": "2026-11-03",
            "duration_minutes": 180, "reported_compensation_class": "paid_work",
        })
        assert duration.get("disposition") == "committed", duration
        with session_factory() as session:
            duration_ref = session.scalar(select(WorkSessionRevision.work_ref).where(
                WorkSessionRevision.description == "Reported hours",
            ))
        dated = await call("docket_get_work_context", {"refs": [duration_ref]})
        bounded = await call("docket_update_work_session", {
            "context_id": dated["context_id"], "operation_id": "quantity-to-bounds",
            "work_ref": duration_ref, "start": "2026-11-03T10:00:00-08:00",
            "end": "2026-11-03T12:00:00-08:00",
        })
        assert bounded.get("disposition") == "committed", bounded
        bounded_context = await call("docket_get_work_context", {"refs": [duration_ref]})
        assert "work_date" not in bounded_context["objects"][0]["state"]
        assert bounded_context["objects"][0]["state"]["duration_us"] == 120 * 60_000_000
        bad = await call("docket_log_work_session", {
            "context_id": current["context_id"], "operation_id": "invalid-work-end",
            "description": "Reversed bounds", "start": "2026-11-03T12:00:00Z",
            "end": "2026-11-03T11:00:00Z",
        })
        assert bad["error"]["code"] == "invalid_work_end"
        assert bad["error"]["details"]["field"] == "end"
        retracted = await call("docket_update_work_session", {
            "context_id": current["context_id"], "operation_id": "retract-work",
            "work_ref": work, "status": "retracted",
        })
        assert retracted.get("disposition") == "committed", retracted
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkSession)) == 3
        assert session.scalar(select(func.count()).select_from(WorkSessionRevision)) == 6
        revisions = list(session.scalars(select(WorkSessionRevision).where(
            WorkSessionRevision.work_ref == work,
        ).order_by(WorkSessionRevision.revision)))
        assert [r.lifecycle for r in revisions] == ["recorded", "recorded", "retracted"]
        assert revisions[0].reported_location == "Example lab"
        assert revisions[1].reported_location is None
        assert len(list(session.scalars(select(WorkSegment)))) == 10
        arrangements = list(session.scalars(select(EngagementRevision).order_by(
            EngagementRevision.revision,
        )))
        assert [r.role for r in arrangements] == ["Provisional role", None]
        assert all(r.label == "Example hardware work" for r in arrangements)
        roots = set(session.scalars(select(AuthenticatedRequest.ref_id)))
        assert all(set(r.basis_refs).intersection(roots) for r in revisions)
        for model in (Operation, CanonicalEvent, Item):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(func.count()).select_from(ToolInvocation)) > 10


@pytest.mark.asyncio
async def test_work_tools_require_write_grant_and_record_rejection(session_factory, linked_plugin):
    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                          headers={"Authorization": "Bearer " + token}) as http,
        Client(streamable_http_client("https://docket.you/mcp/", http_client=http),
               mode="legacy") as client,
    ):
        packet = (await client.call_tool("docket_get_work_context", {})).structured_content
        rejected = (await client.call_tool("docket_log_work_session", {
            "context_id": packet["context_id"], "operation_id": "denied-work",
            "description": "Source text cannot grant authority", "duration_minutes": 60,
        })).structured_content
        assert rejected["ok"] is False
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkSession)) == 0
        assert session.scalar(select(func.count()).select_from(ToolInvocation)) >= 2
