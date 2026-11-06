"""Native planning settings and retained work queries on disposable PostgreSQL."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


async def check_workload(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.plugin.server import PluginMCP

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            value = (await PluginMCP(settings).call_tool(name, arguments)).structured_content
            if name == "docket_get_workload_context":
                assert value.get("ok"), value
                assert len(json.dumps(value, ensure_ascii=False).encode()) <= 8192
            return value
        finally:
            agent_call_context.reset(token)

    async def command(name, identity, project=None, **fields):
        packet = await call(
            "docket_get_workload_context", {"project_ref": project} if project else {}
        )
        value = await call(
            name, {"context_id": packet["context_id"], "operation_id": identity, **fields}
        )
        assert value.get("disposition") in {"prepared", "committed"}, value
        return value

    packet = await call("docket_get_workload_context", {})
    previews = []
    for index in (1, 2):
        preview = await call(
            "docket_set_workload_preferences",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-planning-create-{index}",
                "timezone": "UTC",
                "weekdays": [0, 1, 2, 3, 4],
                "available_start": "09:00",
                "available_end": "17:00",
                "protected_start": "23:00",
                "protected_end": "07:00",
                "preview": True,
            },
        )
        assert preview.get("disposition") == "prepared", preview
        previews.append(preview)
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=15)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(apply, previews))
    assert sum(row.get("disposition") == "committed" for row in created) == 1, created
    assert (
        next(row for row in created if row.get("ok") is False)["error"]["code"]
        == "preference_key_exists"
    ), created
    previews = [
        await command(
            "docket_set_workload_preferences",
            f"pg-planning-update-{i}",
            travel_buffer_minutes=i * 30,
            preview=True,
        )
        for i in (1, 2)
    ]
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        updated = list(pool.map(apply, previews))
    assert sum(row.get("disposition") == "committed" for row in updated) == 1, updated
    context = await call("docket_get_overview", {})
    created_project = await call(
        "docket_create_entity",
        {
            "context_id": context["context_id"],
            "operation_id": "pg-planning-project",
            "entity_kind": "project",
            "display_name": "Synthetic planning project",
        },
    )
    assert created_project.get("disposition") == "committed", created_project
    project = next(ref for ref in created_project["affected_refs"] if ref.startswith("ent_"))
    await command(
        "docket_set_project_budget",
        "pg-planning-budget",
        project=project,
        project_ref=project,
        weekly_hours="1",
        timezone="UTC",
    )
    context = await call("docket_get_work_context", {"refs": [project]})
    logged = await call(
        "docket_log_work_session",
        {
            "context_id": context["context_id"],
            "operation_id": "pg-planning-actual",
            "project_ref": project,
            "duration_minutes": 120,
            "work_date": "2026-11-03",
            "description": "Synthetic reported activity",
            "outcome": "Reported result",
            "reported_compensation_class": "unpaid",
        },
    )
    assert logged.get("disposition") == "committed", logged
    packet = await call(
        "docket_get_workload_context",
        {
            "date_from": "2026-11-02",
            "date_to": "2026-11-09",
            "timezone": "UTC",
            "project_ref": project,
        },
    )
    assert packet["work"]["totals"]["allocated_us"] == 2 * 3_600_000_000, packet
    assert packet["project_budgets"][0]["over_budget_week_count"] == 1, packet
    assert packet["outcomes"][0]["asserted_outcome"] == "Reported result", packet
    return {"native_workload_preference_races_and_project_reports": "passed"}
