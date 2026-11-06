"""Native operational review stays nonbinding, retained, selective and retry-safe."""

import json
from datetime import timedelta

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client
from test_workload import workload_change

from docket.config import get_settings
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    CanonicalEvent,
    CaseItem,
    Operation,
    OperationalReminder,
    PluginNotification,
    ReminderPlan,
    ScheduledNotification,
    WorkloadReconciliation,
    WorkSession,
)
from docket.models.base import utc_now
from docket.plugin.server import PluginMCP
from docket.schemas.followup import FOLLOWUP_TOOL_PURPOSES
from docket.services.reminders import ReminderService

linked_plugin = test_native_plugin.linked_plugin


async def reconcile(call, identity, **extra):
    context = await call("docket_get_overview", {})
    result = await call(
        "docket_reconcile_workload",
        {
            "context_id": context["context_id"],
            "operation_id": identity,
            "date_from": "2026-11-01",
            "date_to": "2026-11-09",
            "timezone": "UTC",
            **extra,
        },
    )
    assert result.get("disposition") in {"committed", "prepared"}, result
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_operational_reminder_delivers_late_once_without_calendar_or_provider_effect(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        logged = await change(
            call,
            "docket_log_work_session",
            [],
            "followup-work",
            description="Synthetic report",
            duration_minutes=60,
            work_date="2026-11-03",
        )
        work = logged["work_records"][0]["ref"]
        context = await call("docket_get_work_context", {"refs": [work]})
        when = utc_now() + timedelta(hours=2)
        arguments = {
            "context_id": context["context_id"],
            "operation_id": "followup-once",
            "subject_ref": work,
            "at": when.isoformat(),
            "timezone": "UTC",
            "label": "Review synthetic work",
        }
        scheduled = await call("docket_schedule_followup", arguments)
        assert scheduled.get("disposition") == "committed", scheduled
        replay = await call("docket_schedule_followup", arguments)
        assert replay["replayed"] and replay["changeset_ref"] == scheduled["changeset_ref"]
        reminder = next(ref for ref in scheduled["affected_refs"] if ref.startswith("rem_"))
        settings = get_settings().model_copy(
            update={"plugin_enabled": True, "discord_adapter_enabled": False}
        )
        service = ReminderService(
            session_factory, settings, clock=lambda: when + timedelta(hours=1)
        )
        assert service.run_due_once()
        service.run_due_once()
        with session_factory() as session:
            notice = session.scalar(select(PluginNotification))
            assert notice.event_name == "docket.reminder.due"
            assert notice.subject_ref == reminder
            assert session.scalar(select(func.count()).select_from(PluginNotification)) == 1
            assert session.scalar(select(ScheduledNotification.status)) == "available"
            assert session.scalar(select(func.count()).select_from(OperationalReminder)) == 1
            assert session.scalar(select(func.count()).select_from(CanonicalEvent)) == 0
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
        context = await call("docket_get_context", {"refs": [reminder]})
        cancelled = await call(
            "docket_retract_record",
            {
                "context_id": context["context_id"],
                "operation_id": "followup-retract",
                "target_ref": reminder,
            },
        )
        assert cancelled.get("disposition") == "committed", cancelled
        with session_factory() as session:
            assert session.scalar(select(ReminderPlan.canonical_status)) == "retracted"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_review_exact_sources_deduplicates_preserves_old_items_and_respects_preferences(
    session_factory, linked_plugin, monkeypatch
):
    monkeypatch.setattr(
        "docket.services.interactive_authority.get_settings", lambda: linked_plugin[1]
    )
    async with work_client(linked_plugin) as call:
        works = []
        for i, (first, last) in enumerate(((10, 12), (11, 13))):
            logged = await change(
                call,
                "docket_log_work_session",
                [],
                f"review-overlap-{i}",
                description="Synthetic overlapping report",
                start=f"2026-11-03T{first}:00:00+00:00",
                end=f"2026-11-03T{last}:00:00+00:00",
                timezone="UTC",
            )
            works.append(logged["work_records"][0]["ref"])
        first = await reconcile(call, "review-first")
        await reconcile(call, "review-again")
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AttentionCase)) == 1
            assert session.scalar(select(func.count()).select_from(PluginNotification)) == 1
            assert session.scalar(select(func.count()).select_from(WorkloadReconciliation)) == 2
            case = session.scalar(select(AttentionCase))
            old_item = session.scalar(select(CaseItem))
            old_ref, old_payload, case_ref = old_item.ref_id, old_item.payload_json, case.ref_id
            notice = session.scalar(select(PluginNotification))
            assert set(notice.data) == {"subject_ref", "summary"}
            assert all("work_" not in value for value in notice.data.values())
        await change(
            call,
            "docket_update_work_session",
            [works[0]],
            "review-correction",
            work_ref=works[0],
            description="Corrected activity description",
        )
        await reconcile(call, "review-corrected")
        with session_factory() as session:
            assert (
                session.get(
                    CaseItem, session.scalar(select(CaseItem.id).where(CaseItem.ref_id == old_ref))
                ).payload_json
                == old_payload
            )
            assert session.scalar(select(func.count()).select_from(CaseItem)) == 2
            assert session.scalar(select(func.count()).select_from(AttentionCaseRevision)) == 2
            assert session.scalar(select(func.count()).select_from(PluginNotification)) == 1
        pending = await reconcile(call, "review-stale-preview", preview=True)
        await change(
            call,
            "docket_update_work_session",
            [works[1]],
            "review-second-correction",
            work_ref=works[1],
            description="Changed after exact review preview",
        )
        rejected = await call("docket_apply_change", {"change_id": pending["change_id"]})
        assert rejected["error"]["code"] == "workload_basis_changed", rejected
        await workload_change(
            call, "docket_set_workload_preferences", "review-quiet", notifications_enabled=False
        )
        await reconcile(call, "review-disabled")
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 2
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            assert session.scalar(select(func.count()).select_from(PluginNotification)) == 1
        detail = await call("docket_get_context", {"refs": [case_ref]})
        assert detail.get("ok"), detail
        receipt_ref = next(ref for ref in first["affected_refs"] if ref.startswith("wrec_"))
        receipt = await call("docket_get_context", {"refs": [receipt_ref]})
        assert receipt.get("ok"), receipt
        audit = await call(
            "docket_get_history_entry",
            {
                "ref": receipt_ref,
                "view": "audit",
                "text_limit": 200,
            },
        )
        assert audit.get("ok"), audit
        assert audit["entry"]["workload_json_chunk"]
        assert audit["entry"]["text_next_offset"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_followup_discovery_stays_flat_and_bounded(linked_plugin):
    tools = {tool.name: tool for tool in await PluginMCP(get_settings()).list_tools()}
    for name in FOLLOWUP_TOOL_PURPOSES:
        tool = tools[name]
        assert len(json.dumps(tool.input_schema, separators=(",", ":")).encode()) <= 4096
        assert (
            len(
                json.dumps(
                    tool.model_dump(mode="json", by_alias=True), separators=(",", ":")
                ).encode()
            )
            <= 6144
        )
        assert "oneOf" not in json.dumps(tool.input_schema)

    async with work_client(linked_plugin) as call:
        context = await call("docket_get_overview", {})
        rejected = await call(
            "docket_reconcile_workload",
            {
                "context_id": context["context_id"],
                "operation_id": "review-readonly",
            },
        )
        assert rejected["error"]["code"] == "agent_authority_denied"
