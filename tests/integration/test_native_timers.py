"""Timer authority, exact instants and lifecycle through real MCP transport."""

from datetime import UTC, datetime

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_reports import work_client

from docket.domain.work_time import elapsed_us
from docket.models import (
    AuthenticatedRequest,
    CanonicalEvent,
    Item,
    Operation,
    PreparedCommand,
    WorkSegment,
    WorkSession,
    WorkSessionRevision,
)
from docket.services.work_attribution import utc_instant, validate_work_time
from docket.services.work_timers import offset_at, timer_start, timer_transition

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_exact_timer_preview_pause_resume_atomic_switch_and_retry(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        context = await call("docket_get_work_context", {})
        args = {
            "context_id": context["context_id"],
            "operation_id": "timer-start",
            "description": "Example validation",
            "timezone": "America/Los_Angeles",
            "preview": True,
        }
        preview = await call("docket_start_work_session", args)
        assert preview.get("disposition") == "prepared", preview
        first_instant = datetime.fromisoformat(preview["preview"]["effective_at"])
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 0
            proposal = session.scalar(select(PreparedCommand))
            admitted = session.scalar(
                select(AuthenticatedRequest).where(
                    AuthenticatedRequest.ref_id == proposal.request_ref,
                )
            )
            assert first_instant == utc_instant(admitted.admitted_at)
        started = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert started.get("disposition") == "committed", started
        work = started["work_records"][0]["ref"]
        assert started["work_records"][0]["status"] == "running"
        replay = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert replay.pop("replayed") and replay == started
        conflict = await call(
            "docket_start_work_session",
            {
                **args,
                "operation_id": "second-timer",
                "preview": False,
            },
        )
        assert conflict["error"]["code"] == "primary_timer_exists"
        assert conflict["error"]["details"]["work_ref"] == work
        current = await call("docket_get_work_context", {})
        assert current["objects"][0]["state"]["status"] == "running"
        assert "docket_pause_work_session" in current["objects"][0]["actions"]
        paused_preview = await call(
            "docket_pause_work_session",
            {
                "context_id": current["context_id"],
                "operation_id": "timer-pause",
                "work_ref": work,
                "preview": True,
            },
        )
        pause_at = datetime.fromisoformat(paused_preview["preview"]["effective_at"])
        with session_factory() as session:
            assert session.scalar(select(WorkSession.lifecycle)) == "running"
        paused = await call("docket_apply_change", {"change_id": paused_preview["change_id"]})
        assert paused.get("disposition") == "committed", paused
        worked = elapsed_us(pause_at - first_instant)
        assert paused["work_records"][0]["duration_us"] == worked
        current = await call("docket_get_work_context", {})
        assert current["objects"][0]["state"]["estimated_duration_us"] == worked
        resumed = await call(
            "docket_resume_work_session",
            {
                "context_id": current["context_id"],
                "operation_id": "timer-resume",
                "work_ref": work,
            },
        )
        assert resumed.get("disposition") == "committed", resumed
        with session_factory() as session:
            revision = session.scalar(
                select(WorkSessionRevision).where(
                    WorkSessionRevision.work_ref == work,
                    WorkSessionRevision.revision == 3,
                )
            )
            open_segment = session.scalar(
                select(WorkSegment).where(
                    WorkSegment.revision_id == revision.id,
                    WorkSegment.end_at.is_(None),
                )
            )
            resumed_at = utc_instant(open_segment.start_at)
        current = await call("docket_get_work_context", {})
        annotated = await call(
            "docket_update_work_session",
            {
                "context_id": current["context_id"],
                "operation_id": "timer-annotation",
                "work_ref": work,
                "outcome": "Provisional finding",
            },
        )
        assert annotated.get("disposition") == "committed", annotated
        current = await call("docket_get_work_context", {})
        switch_args = {
            "context_id": current["context_id"],
            "operation_id": "timer-switch",
            "from_work_ref": work,
            "description": "Example second work",
            "attribution_label": "Unresolved arrangement",
            "previous_outcome": "Finding recorded",
        }
        switched = await call("docket_switch_work_session", switch_args)
        assert switched.get("disposition") == "committed", switched
        assert len(switched["work_records"]) == 2
        old, new = switched["work_records"]
        assert old["ref"] == work and old["status"] == "recorded" and new["status"] == "running"
        with session_factory() as session:
            stopped_revision = session.scalar(
                select(WorkSessionRevision).where(
                    WorkSessionRevision.work_ref == work,
                    WorkSessionRevision.revision == 5,
                )
            )
            new_revision = session.scalar(
                select(WorkSessionRevision).where(
                    WorkSessionRevision.work_ref == new["ref"],
                    WorkSessionRevision.revision == 1,
                )
            )
            assert stopped_revision.end_at == new_revision.start_at
            assert stopped_revision.duration_us == worked + elapsed_us(
                utc_instant(stopped_revision.end_at) - resumed_at,
            )
            assert stopped_revision.outcome == "Finding recorded"
        current = await call("docket_get_work_context", {})
        stopped = await call(
            "docket_stop_work_session",
            {
                "context_id": current["context_id"],
                "operation_id": "second-timer-stop",
                "work_ref": new["ref"],
                "outcome": "Second work complete",
            },
        )
        assert stopped.get("disposition") == "committed", stopped
        replay = await call("docket_switch_work_session", switch_args)
        assert replay.pop("replayed") and replay == switched
        empty = await call("docket_get_work_context", {})
        assert empty["objects"] == []
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkSession)) == 2
        assert session.scalar(select(func.count()).select_from(WorkSessionRevision)) == 7
        assert set(session.scalars(select(WorkSession.lifecycle))) == {"recorded"}
        for model in (CanonicalEvent, Item, Operation):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_stale_timer_proposal_and_explicit_abandonment_leave_no_invented_end(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_work_context", {})
        started = await call(
            "docket_start_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": "abandon-start",
                "description": "Incomplete timer",
            },
        )
        assert started.get("disposition") == "committed", started
        work = started["work_records"][0]["ref"]
        packet = await call("docket_get_work_context", {})
        preview = await call(
            "docket_stop_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": "stale-timer-stop",
                "work_ref": work,
                "preview": True,
            },
        )
        updated = await call(
            "docket_update_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": "abandon-metadata",
                "work_ref": work,
                "description": "Corrected description",
            },
        )
        assert updated.get("disposition") == "committed", updated
        rejected = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert rejected.get("ok") is False
        packet = await call("docket_get_work_context", {})
        abandoned = await call(
            "docket_update_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": "abandon-timer",
                "work_ref": work,
                "status": "retracted",
            },
        )
        assert abandoned.get("disposition") == "committed", abandoned
        assert "estimated_duration_us" not in abandoned["work_records"][0]
        abandoned_context = await call("docket_get_work_context", {"refs": [work]})
        corrected = await call(
            "docket_update_work_session",
            {
                "context_id": abandoned_context["context_id"],
                "operation_id": "recover-reported-work",
                "work_ref": work,
                "status": "recorded",
                "duration_minutes": 30,
            },
        )
        assert corrected.get("disposition") == "committed", corrected
        assert corrected["work_records"][0]["duration_us"] == 30 * 60_000_000
        packet = await call("docket_get_work_context", {})
        assert packet["objects"] == []
        another = await call(
            "docket_start_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": "after-abandonment",
                "description": "New timer after explicit retraction",
            },
        )
        assert another.get("disposition") == "committed", another
        latest_context = await call("docket_get_work_context", {})
        active = latest_context["objects"][0]
        zero = await call(
            "docket_stop_work_session",
            {
                "context_id": latest_context["context_id"],
                "operation_id": "reported-zero-stop",
                "work_ref": active["ref"],
                "end": active["state"]["start"],
            },
        )
        assert zero.get("disposition") == "committed", zero
        assert zero["work_records"][0]["duration_us"] == 0
        with session_factory() as session:
            reported = session.scalar(
                select(WorkSessionRevision).where(
                    WorkSessionRevision.work_ref == active["ref"],
                    WorkSessionRevision.revision == 2,
                )
            )
            assert reported.source_method == "report"
    with session_factory() as session:
        latest = session.scalar(
            select(WorkSessionRevision).where(
                WorkSessionRevision.work_ref == work,
                WorkSessionRevision.revision == 3,
            )
        )
        assert latest.lifecycle == "retracted" and latest.end_at is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkSession)
                .where(
                    WorkSession.lifecycle == "running",
                )
            )
            == 0
        )


def test_timer_segments_across_dst_use_elapsed_instants_and_keep_pause_exclusion():
    start = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    pause = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    resume = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    stop = datetime(2026, 11, 1, 10, tzinfo=UTC)
    zone = "America/Los_Angeles"
    value = timer_start({"description": "DST timer"}, start, zone)
    value = timer_transition(value, "pause", pause, offset_at(pause, zone))
    validate_work_time(value)
    assert value.time.duration_us == 45 * 60_000_000
    assert value.time.segments[0].start_offset_seconds == -25200
    assert value.time.segments[0].end_offset_seconds == -28800
    value = timer_transition(value, "resume", resume, offset_at(resume, zone))
    value = timer_transition(value, "stop", stop, offset_at(stop, zone))
    validate_work_time(value)
    assert value.time.duration_us == 75 * 60_000_000
    assert len(value.time.segments) == 3 and value.status == "recorded"
