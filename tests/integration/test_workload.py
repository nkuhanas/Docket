"""Native planning combines known schedules, reported work and bounded settings."""

import json
import uuid
from datetime import UTC, datetime

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client

from docket.models import (
    CalendarEventCache,
    CalendarLane,
    CalendarSyncState,
    ChangeSet,
    Operation,
    Preference,
    ProviderAccount,
    ProviderEventBinding,
    WorkSession,
)
from docket.models.base import utc_now
from docket.plugin.server import PluginMCP
from docket.schemas.workload import WORKLOAD_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin
HOUR = 3_600_000_000


def calendar_fixture(factory):
    with factory.begin() as session:
        account = ProviderAccount(
            provider="google",
            external_account_id="synthetic-planning",
            capabilities=["google_calendar"],
        )
        session.add(account)
        session.flush()
        lane = CalendarLane(
            account_id=account.id,
            lane="meetings",
            display_name="Meetings",
            color_hex="#123456",
            calendar_id="synthetic-planning",
            created_by_changeset_ref="test-fixture",
            status="active",
        )
        session.add(lane)
        session.flush()
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=lane.calendar_id,
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2028, 1, 1, tzinfo=UTC),
                status="current",
                last_success_at=utc_now(),
                snapshot_generation=uuid.uuid4(),
            )
        )
        return account.id, lane.ref_id


async def workload_change(call, name, identity, project=None, **fields):
    context = await call("docket_get_workload_context", {"project_ref": project} if project else {})
    assert context.get("ok"), context
    result = await call(
        name, {"context_id": context["context_id"], "operation_id": identity, **fields}
    )
    assert result.get("disposition") in {"committed", "prepared"}, result
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_workload_union_coverage_sparse_preferences_and_no_calendar_to_work(
    session_factory, linked_plugin
):
    account, lane = calendar_fixture(session_factory)
    async with work_client(linked_plugin) as call:
        unconfigured = await call(
            "docket_get_workload_context",
            {"date_from": "2027-01-04", "date_to": "2027-01-11", "timezone": "UTC"},
        )
        assert unconfigured["capacity"].get("potential_available_us") is None
        assert not unconfigured["capacity"]["availability_configured"]
        pref = await workload_change(
            call,
            "docket_set_workload_preferences",
            "planning-availability",
            timezone="UTC",
            weekdays=[0, 1, 2, 3, 4],
            available_start="09:00",
            available_end="17:00",
            protected_start="12:00",
            protected_end="13:00",
        )
        pref_ref = next(ref for ref in pref["affected_refs"] if ref.startswith("pref_"))
        for i, (start, end, labels) in enumerate(
            ((10, 12, ["planned_work", "firm"]), (11, 13, ["tentative"]))
        ):
            context = await call("docket_get_calendar_context", {})
            result = await call(
                "docket_create_event",
                {
                    "context_id": context["context_id"],
                    "operation_id": f"planning-meeting-{i}",
                    "title": f"Synthetic commitment {i}",
                    "calendar_ref": lane,
                    "timezone": "UTC",
                    "start": f"2027-01-04T{start}:00:00+00:00",
                    "end": f"2027-01-04T{end}:00:00+00:00",
                    "context_labels": labels,
                },
            )
            assert result.get("disposition") == "committed", result
            if i == 0:
                event = next(ref for ref in result["affected_refs"] if ref.startswith("evt_"))
        with session_factory.begin() as session:
            session.add(
                ProviderEventBinding(
                    canonical_target_ref=event,
                    target_kind="event",
                    account_id=account,
                    calendar_id="synthetic-planning",
                    provider_event_id="synthetic-duplicate",
                    status="active",
                )
            )
            session.add(
                CalendarEventCache(
                    account_id=account,
                    calendar_id="synthetic-planning",
                    provider_event_id="synthetic-duplicate",
                    snapshot_generation=session.scalar(
                        select(CalendarSyncState.snapshot_generation)
                    ),
                    event_type="default",
                    status="confirmed",
                    summary="Observed duplicate",
                    is_all_day=False,
                    start_at=datetime(2027, 1, 4, 10, tzinfo=UTC),
                    end_at=datetime(2027, 1, 4, 12, tzinfo=UTC),
                    timezone="UTC",
                    synced_at=utc_now(),
                )
            )
        packet = await call(
            "docket_get_workload_context",
            {"date_from": "2027-01-04", "date_to": "2027-01-11", "timezone": "UTC"},
        )
        assert packet.get("ok"), packet
        assert packet["commitment_count"] == 2
        assert packet["capacity"]["known_busy_union_us"] == 3 * HOUR
        assert packet["capacity"]["potential_available_us"] == 33 * HOUR
        assert packet["capacity"]["protected_commitment_overlap_us"] == HOUR
        assert packet["coverage"]["provider_calendar_complete"]
        assert any(
            row["planned_activity"] and row["firmness"] == "firm" for row in packet["commitments"]
        )
        assert len(json.dumps(packet, ensure_ascii=False).encode()) <= 8192
        stale_context = packet["context_id"]
        await workload_change(
            call, "docket_set_workload_preferences", "planning-travel", travel_buffer_minutes=30
        )
        stale = await call(
            "docket_set_workload_preferences",
            {
                "context_id": stale_context,
                "operation_id": "planning-stale",
                "notifications_enabled": False,
            },
        )
        assert stale.get("ok") is False, stale
        current = await call(
            "docket_get_workload_context",
            {"date_from": "2027-01-04", "date_to": "2027-01-11", "timezone": "UTC"},
        )
        values = next(row for row in current["preferences"] if row["ref"] == pref_ref)["values"]
        assert values["available_start"] == "09:00:00" and values["travel_buffer_minutes"] == 30
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 0
            original = session.scalar(
                select(ChangeSet).where(ChangeSet.ref_id == pref["changeset_ref"])
            )
            assert (
                original.preference_changes[0]["create_spec"]["policy_json"]["values"][
                    "travel_buffer_minutes"
                ]
                is None
            )
        calendar_context = await call("docket_get_calendar_context", {})
        planned = await call(
            "docket_create_event",
            {
                "context_id": calendar_context["context_id"],
                "operation_id": "planning-past-series",
                "calendar_ref": lane,
                "title": "Synthetic reported work plan",
                "timezone": "UTC",
                "start": "2026-11-03T10:00:00+00:00",
                "end": "2026-11-03T11:00:00+00:00",
                "recurrence": {"frequency": "daily", "count": 2},
                "context_labels": ["planned_work"],
            },
        )
        assert planned.get("disposition") == "committed", planned
        series = next(ref for ref in planned["affected_refs"] if ref.startswith("evt_"))
        review = await call(
            "docket_get_workload_context",
            {
                "date_from": "2026-11-02",
                "date_to": "2026-11-09",
                "timezone": "UTC",
            },
        )
        assert review["activity_review_candidate_count"] == 2, review
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 0
            operations_before = session.scalar(select(func.count()).select_from(Operation))
        logged = await call(
            "docket_log_work_session",
            {
                "context_id": review["context_id"],
                "operation_id": "planning-confirm-one-occurrence",
                "event_ref": series,
                "duration_minutes": 60,
                "work_date": "2026-11-03",
                "timezone": "UTC",
                "description": "Reported work for one occurrence",
            },
        )
        assert logged.get("disposition") == "committed", logged
        review = await call(
            "docket_get_workload_context",
            {
                "date_from": "2026-11-02",
                "date_to": "2026-11-09",
                "timezone": "UTC",
            },
        )
        assert review["activity_review_candidate_count"] == 1, review
        assert review["activity_review_candidates"][0]["occurrence_date"] == "2026-11-04"
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkSession)) == 1
            assert session.scalar(select(func.count()).select_from(Operation)) == operations_before


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_workload_outcomes_project_budget_deadlines_and_incomplete_coverage(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        project_result = await change(
            call,
            "docket_create_entity",
            [],
            "planning-project",
            entity_kind="project",
            display_name="Synthetic project",
        )
        project = next(ref for ref in project_result["affected_refs"] if ref.startswith("ent_"))
        budget = await workload_change(
            call,
            "docket_set_project_budget",
            "planning-budget",
            project=project,
            project_ref=project,
            weekly_hours="1",
            timezone="UTC",
        )
        await change(
            call,
            "docket_log_work_session",
            [project],
            "planning-actual",
            project_ref=project,
            description="Investigated synthetic hardware",
            duration_minutes=120,
            work_date="2026-11-03",
            outcome="Reported root cause",
            reported_compensation_class="unpaid",
        )
        context = await call("docket_get_overview", {})
        task = await call(
            "docket_create_task",
            {
                "context_id": context["context_id"],
                "operation_id": "planning-deadline",
                "title": "Synthetic overdue deliverable",
                "due": "2026-11-04",
                "timezone": "UTC",
            },
        )
        assert task.get("disposition") == "committed", task
        packet = await call(
            "docket_get_workload_context",
            {
                "date_from": "2026-11-02",
                "date_to": "2026-11-09",
                "timezone": "UTC",
                "project_ref": project,
            },
        )
        assert packet.get("ok"), packet
        assert packet["work"]["totals"]["allocated_us"] == 2 * HOUR
        assert packet["outcomes"][0]["asserted_outcome"] == "Reported root cause"
        assert packet["project_budgets"][0]["over_budget_week_count"] == 1
        assert any(row["overdue"] for row in packet["deadlines"])
        assert "provider_calendar_coverage_unavailable" in packet["coverage"]["flags"]
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            assert session.scalar(select(func.count()).select_from(Preference)) == 1
        assert budget["affected_refs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_workload_contract_sizes_and_read_only_boundary(linked_plugin):
    _, settings, _, _ = linked_plugin
    tools = {tool.name: tool for tool in await PluginMCP(settings).list_tools()}
    for name in WORKLOAD_TOOL_PURPOSES:
        definition = tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)
        assert len(json.dumps(definition["inputSchema"], separators=(",", ":")).encode()) <= 4096
        assert len(json.dumps(definition, separators=(",", ":")).encode()) <= 6144
        assert not definition["inputSchema"].get("oneOf")
    async with work_client(linked_plugin) as call:
        context = await call("docket_get_workload_context", {})
        assert context.get("ok"), context
        rejected = await call(
            "docket_set_workload_preferences",
            {
                "context_id": context["context_id"],
                "operation_id": "planning-readonly",
                "notifications_enabled": False,
            },
        )
        assert rejected["error"]["code"] == "agent_authority_denied"
