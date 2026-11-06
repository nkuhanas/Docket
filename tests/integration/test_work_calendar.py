"""Actual work and its optional, revision-pinned Calendar display remain separate."""

import json

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client
from test_workload import calendar_fixture

from docket.domain.public_refs import new_public_ref
from docket.models import (
    CalendarLane,
    CanonicalEvent,
    Operation,
    WorkCalendarProjection,
    WorkCalendarRevision,
    WorkSession,
)
from docket.plugin.server import PluginMCP
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.operations import OperationRunner

linked_plugin = test_native_plugin.linked_plugin


async def show(call, work, lane, identity, **extra):
    context = await call("docket_get_context", {"refs": [work, lane]})
    assert context.get("ok"), context
    return await call(
        "docket_show_work_session",
        {
            "context_id": context["context_id"],
            "operation_id": identity,
            "work_ref": work,
            "calendar_ref": lane,
            **extra,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_snapshot_unknown_delivery_correction_refresh_readonly_hide_and_accounting(
    session_factory, linked_plugin
):
    account, lane = calendar_fixture(session_factory)
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider, consistency_window_seconds=0)
    async with work_client(linked_plugin) as call:
        logged = await change(
            call,
            "docket_log_work_session",
            [],
            "visibility-work",
            description="Synthetic actual activity",
            start="2026-11-03T10:00:00+00:00",
            end="2026-11-03T12:00:00+00:00",
            timezone="UTC",
        )
        work = logged["work_records"][0]["ref"]
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
        displayed = await show(call, work, lane, "visibility-first")
        assert displayed.get("disposition") == "committed", displayed
        projection = displayed["calendar_views"][0]
        assert projection["enabled"] and not projection["needs_refresh"]
        projection_ref, event_ref = projection["ref"], projection["event_ref"]
        original_revision = projection["source_revision_ref"]
        provider.next_create_outcome = "unknown_after_write"
        assert runner.run_due_once()
        assert len(provider.events) == 1 and not runner.run_due_once()
        corrected = await change(
            call,
            "docket_update_work_session",
            [work],
            "visibility-correction",
            work_ref=work,
            end="2026-11-03T13:00:00+00:00",
        )
        assert corrected["work_records"][0]["duration_us"] == 3 * 3_600_000_000
        stale = corrected["work_records"][0]["calendar_visibility"]
        assert stale["needs_refresh"] and stale["source_revision_ref"] == original_revision
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Operation)) == 1
            assert session.scalar(select(WorkSession.version)) == 2
            assert session.scalar(select(func.count()).select_from(WorkCalendarRevision)) == 2
        blocked_refresh = await show(call, work, lane, "visibility-waiting")
        assert blocked_refresh.get("ok") is False, blocked_refresh
        assert runner.reconcile_once()
        refreshed = await show(call, work, lane, "visibility-refresh")
        assert refreshed.get("disposition") == "committed", refreshed
        assert refreshed["calendar_views"][0]["event_ref"] == event_ref
        assert not refreshed["calendar_views"][0]["needs_refresh"]
        assert runner.run_due_once()
        assert len(provider.events) == 1
        with session_factory() as session:
            assert "3 h" in session.scalar(select(CanonicalEvent.title))
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 1
        with session_factory.begin() as session:
            second_lane = CalendarLane(
                account_id=account,
                lane="actual_work",
                display_name="Other work snapshots",
                color_hex="#123456",
                calendar_id="synthetic-other-work",
                status="active",
                created_by_changeset_ref=logged["changeset_ref"],
            )
            session.add(second_lane)
            session.flush()
            second_ref = second_lane.ref_id
        moved = await show(call, work, second_ref, "visibility-move")
        assert moved.get("disposition") == "committed", moved
        assert moved["calendar_views"][0]["ref"] == projection_ref
        previous_event = event_ref
        event_ref = moved["calendar_views"][0]["event_ref"]
        assert event_ref != previous_event
        assert runner.run_due_once() and runner.run_due_once()
        with session_factory() as session:
            assert (
                session.scalar(
                    select(CanonicalEvent.status).where(CanonicalEvent.ref_id == previous_event)
                )
                == "cancelled"
            )
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 1
        packet = await call(
            "docket_get_workload_context",
            {
                "date_from": "2026-11-02",
                "date_to": "2026-11-09",
                "timezone": "UTC",
            },
        )
        assert packet["commitment_count"] == 0, packet
        assert packet["work"]["totals"]["allocated_us"] == 3 * 3_600_000_000
        context = await call("docket_get_context", {"refs": [event_ref]})
        assert "docket_update_event" not in context["objects"][0]["actions"]
        refused = await call(
            "docket_update_event",
            {
                "context_id": context["context_id"],
                "operation_id": "visibility-no-independent-edit",
                "event_ref": event_ref,
                "title": "Independent time copy",
            },
        )
        assert refused["error"]["code"] == "work_calendar_read_only", refused
        split = await change(
            call,
            "docket_split_work_session",
            [work],
            "visibility-superseded",
            work_ref=work,
            parts=[
                {"start": "2026-11-03T10:00:00+00:00", "end": "2026-11-03T11:00:00+00:00"},
                {"start": "2026-11-03T11:00:00+00:00", "end": "2026-11-03T13:00:00+00:00"},
            ],
        )
        parent = next(row for row in split["work_records"] if row["ref"] == work)
        child = next(row["ref"] for row in split["work_records"] if not row["superseded"])
        assert parent["calendar_visibility"]["needs_refresh"]
        assert parent["calendar_visibility"].get("refresh") is None
        context = await call("docket_get_work_context", {"refs": [work]})
        hidden = await call(
            "docket_retract_record",
            {
                "context_id": context["context_id"],
                "operation_id": "visibility-hide",
                "target_ref": projection_ref,
            },
        )
        assert hidden.get("disposition") == "committed", hidden
        assert not hidden["calendar_views"][0]["enabled"]
        provider.next_cancel_outcome = "unknown_after_write"
        assert runner.run_due_once() and runner.reconcile_once()
        with session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(CanonicalEvent)
                    .where(CanonicalEvent.status == "active")
                )
                == 0
            )
            assert not session.scalar(select(WorkCalendarProjection.enabled))
            before = session.scalar(select(func.count()).select_from(Operation))
        await change(
            call,
            "docket_update_work_session",
            [child],
            "visibility-hidden-correction",
            work_ref=child,
            description="Another actual-work correction",
        )
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Operation)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_duration_only_record_stays_valid_and_source_change_invalidates_exact_preview(
    session_factory, linked_plugin
):
    _, lane = calendar_fixture(session_factory)
    async with work_client(linked_plugin) as call:
        logged = await change(
            call,
            "docket_log_work_session",
            [],
            "visibility-unbounded",
            description="Synthetic duration",
            duration_minutes=180,
            work_date="2026-11-03",
        )
        work = logged["work_records"][0]["ref"]
        rejected = await show(call, work, lane, "visibility-no-invented-bounds")
        assert rejected["error"]["code"] == "work_calendar_unavailable", rejected
        await change(
            call,
            "docket_update_work_session",
            [work],
            "visibility-known-bounds",
            work_ref=work,
            start="2026-11-03T10:00:00+00:00",
            end="2026-11-03T13:00:00+00:00",
            timezone="UTC",
        )
        preview = await show(call, work, lane, "visibility-exact-preview", preview=True)
        assert preview.get("disposition") == "prepared", preview
        await change(
            call,
            "docket_update_work_session",
            [work],
            "visibility-after-preview",
            work_ref=work,
            end="2026-11-03T14:00:00+00:00",
        )
        stale = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert stale.get("ok") is False, stale
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkCalendarProjection)) == 0
            assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 0
            assert session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_visibility_contract_stays_flat_and_write_authority_is_separate(linked_plugin):
    _, settings, _, _ = linked_plugin
    tools = {t.name: t for t in await PluginMCP(settings).list_tools()}
    tool = tools["docket_show_work_session"].model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert len(json.dumps(tool["inputSchema"], separators=(",", ":")).encode()) <= 4096
    assert len(json.dumps(tool, separators=(",", ":")).encode()) <= 6144
    assert "oneOf" not in json.dumps(tool["inputSchema"])

    async with work_client(linked_plugin) as call:
        context = await call("docket_get_overview", {})
        denied = await call(
            "docket_show_work_session",
            {
                "context_id": context["context_id"],
                "operation_id": "visibility-readonly",
                "work_ref": new_public_ref("work"),
                "calendar_ref": new_public_ref("lane"),
            },
        )
        assert denied["error"]["code"] == "agent_authority_denied"
