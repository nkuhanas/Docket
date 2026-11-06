"""Atomic snapshot creation, concurrent intent and source invalidation on PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


async def check_work_calendar(settings, factory, principal):
    from sqlalchemy import func, select, update
    from sqlalchemy.exc import DBAPIError

    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import (
        CalendarLane,
        CanonicalEvent,
        OperationTarget,
        ProviderAccount,
        WorkCalendarProjection,
        WorkCalendarRevision,
        WorkSession,
    )
    from docket.plugin.server import PluginMCP

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    with factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="synthetic-work-calendar",
            capabilities=["google_calendar"],
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id,
            lane="work",
            display_name="Work snapshots",
            color_hex="#123456",
            calendar_id="synthetic-work-calendar",
            status="active",
            created_by_changeset_ref="synthetic-fixture",
        )
        session.add(lane)
        session.flush()
        lane_ref = lane.ref_id
    context = await call("docket_get_work_context", {})
    logged = await call(
        "docket_log_work_session",
        {
            "context_id": context["context_id"],
            "operation_id": "pg-work-display-source",
            "description": "Synthetic bounded activity",
            "start": "2026-11-03T09:00:00+00:00",
            "end": "2026-11-03T10:00:00+00:00",
            "timezone": "UTC",
        },
    )
    assert logged.get("disposition") == "committed", logged
    work = logged["work_records"][0]["ref"]
    context = await call("docket_get_context", {"refs": [work, lane_ref]})
    previews = []
    for i in (1, 2):
        result = await call(
            "docket_show_work_session",
            {
                "context_id": context["context_id"],
                "operation_id": f"pg-work-display-{i}",
                "work_ref": work,
                "calendar_ref": lane_ref,
                "preview": True,
            },
        )
        assert result.get("disposition") == "prepared", result
        previews.append(result)
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=15)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, previews))
    assert sum(row.get("disposition") == "committed" for row in results) == 1, results
    with factory() as session:
        row = session.scalar(
            select(WorkCalendarProjection).where(WorkCalendarProjection.work_ref == work)
        )
        assert row is not None
        ref, event_ref, pinned = row.ref_id, row.event_ref, row.source_revision_ref
        assert (
            session.scalar(
                select(func.count())
                .select_from(CanonicalEvent)
                .where(CanonicalEvent.canonical_key.startswith("actual-work:" + work + ":"))
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationTarget)
                .where(OperationTarget.canonical_target_ref == event_ref)
            )
            == 1
        )
    context = await call("docket_get_work_context", {"refs": [work]})
    corrected = await call(
        "docket_update_work_session",
        {
            "context_id": context["context_id"],
            "operation_id": "pg-work-display-correction",
            "work_ref": work,
            "end": "2026-11-03T11:00:00+00:00",
        },
    )
    assert corrected.get("disposition") == "committed", corrected
    assert corrected["work_records"][0]["calendar_visibility"]["needs_refresh"]
    with factory() as session:
        row = session.scalar(
            select(WorkCalendarProjection).where(WorkCalendarProjection.ref_id == ref)
        )
        assert row.source_revision_ref == pinned and row.needs_refresh and row.version == 2
        assert session.scalar(select(WorkSession.version).where(WorkSession.ref_id == work)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkCalendarRevision)
                .where(WorkCalendarRevision.projection_ref == ref)
            )
            == 2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationTarget)
                .where(OperationTarget.canonical_target_ref == event_ref)
            )
            == 1
        )
    for statement in (
        update(WorkCalendarProjection)
        .where(WorkCalendarProjection.ref_id == ref)
        .values(version=3),
        update(WorkCalendarRevision)
        .where(WorkCalendarRevision.projection_ref == ref)
        .values(reason="mutated"),
        update(WorkCalendarProjection)
        .where(WorkCalendarProjection.ref_id == ref)
        .values(operator_ref="synthetic-other-owner", version=3),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
            raise AssertionError("work Calendar history/complete version/owner guard was bypassed")
        except DBAPIError:
            pass
    return {"work_calendar_atomic_intent_race_and_source_invalidation": "passed"}
