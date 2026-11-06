"""Operational reminder ownership, retained receipts and concurrent review evidence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch


async def check_followup(settings, factory, principal):
    from sqlalchemy import func, select, update
    from sqlalchemy.exc import DBAPIError

    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import (
        AttentionCase,
        OperationalReminder,
        PluginNotification,
        ReminderPlan,
        WorkloadReconciliation,
    )
    from docket.models.base import utc_now
    from docket.plugin.server import PluginMCP

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    works = []
    for i, (first, last) in enumerate(((10, 12), (11, 13))):
        context = await call("docket_get_work_context", {})
        result = await call(
            "docket_log_work_session",
            {
                "context_id": context["context_id"],
                "operation_id": f"pg-followup-work-{i}",
                "description": "Synthetic follow-up overlap",
                "timezone": "UTC",
                "start": f"2026-11-04T{first}:00:00+00:00",
                "end": f"2026-11-04T{last}:00:00+00:00",
            },
        )
        assert result.get("disposition") == "committed", result
        works.append(result["work_records"][0]["ref"])
    context = await call("docket_get_work_context", {"refs": [works[0]]})
    result = await call(
        "docket_schedule_followup",
        {
            "context_id": context["context_id"],
            "operation_id": "pg-followup-reminder",
            "subject_ref": works[0],
            "at": (utc_now() + timedelta(days=1)).isoformat(),
            "timezone": "UTC",
            "label": "Synthetic reporting cutoff",
        },
    )
    assert result.get("disposition") == "committed", result
    ref = next(r for r in result["affected_refs"] if r.startswith("rem_"))
    with factory() as session:
        source = session.scalar(select(ReminderPlan).where(ReminderPlan.ref_id == ref))
        source_values = {
            key: getattr(source, key)
            for key in (
                "subject_ref",
                "delivery_channels",
                "lead_seconds",
                "timezone",
                "basis_refs",
                "created_by_changeset_ref",
            )
        }
    try:
        with factory.begin() as session:
            wrong = ReminderPlan(**source_values)
            session.add(wrong)
            session.flush()
            session.add(
                OperationalReminder(
                    reminder_ref=wrong.ref_id,
                    operator_ref="synthetic-other-owner",
                    trigger_at=utc_now() + timedelta(days=1),
                )
            )
        raise AssertionError("cross-owner operational reminder was accepted")
    except DBAPIError:
        pass
    try:
        with factory.begin() as session:
            session.execute(
                update(OperationalReminder)
                .where(OperationalReminder.reminder_ref == ref)
                .values(label="mutated")
            )
        raise AssertionError("operational trigger was mutable")
    except DBAPIError:
        pass
    previews = []
    for i in (1, 2):
        context = await call("docket_get_overview", {})
        result = await call(
            "docket_reconcile_workload",
            {
                "context_id": context["context_id"],
                "operation_id": f"pg-followup-review-{i}",
                "date_from": "2026-11-04",
                "date_to": "2026-11-05",
                "timezone": "UTC",
                "limit": 100,
                "preview": True,
            },
        )
        assert result.get("disposition") == "prepared", result
        previews.append(result)
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=15)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    notifications = settings.model_copy(
        update={"plugin_enabled": True, "discord_adapter_enabled": False}
    )
    with (
        patch("docket.services.interactive_authority.get_settings", return_value=notifications),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        applied = list(pool.map(apply, previews))
    assert all(row.get("disposition") == "committed" for row in applied), applied
    with factory() as session:
        records = list(session.scalars(select(WorkloadReconciliation)))
        assert len(records) == 2
        assert set(records[0].case_refs) == set(records[1].case_refs)
        assert session.scalar(
            select(func.count())
            .select_from(AttentionCase)
            .where(AttentionCase.ref_id.in_(records[0].case_refs))
        ) == len(records[0].case_refs)
        for case_ref in records[0].case_refs:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PluginNotification)
                    .where(PluginNotification.subject_ref == case_ref)
                )
                <= 1
            )
    try:
        with factory.begin() as session:
            session.execute(update(WorkloadReconciliation).values(snapshot={}))
        raise AssertionError("operational receipt was mutable")
    except DBAPIError:
        pass
    return {"operational_reminder_owner_immutability_and_review_races": "passed"}
