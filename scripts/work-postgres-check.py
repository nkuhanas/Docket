"""Synthetic work migration, canonical commands, immutability and race evidence."""

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

ROOT = Path(__file__).resolve().parents[1]


async def check_native_timers(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import WorkSession, WorkSessionRevision
    from docket.plugin.server import PluginMCP

    server = PluginMCP(settings)
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        packet = (await server.call_tool("docket_get_work_context", {})).structured_content
    finally:
        agent_call_context.reset(token)
    barrier = Barrier(2)

    def run_call(name, arguments, *, synchronize=True):
        async def invoke():
            context = agent_call_context.set(AgentCallContext(principal))
            try:
                endpoint = PluginMCP(settings)
                if synchronize:
                    barrier.wait(timeout=10)
                return (await endpoint.call_tool(name, arguments)).structured_content
            finally:
                agent_call_context.reset(context)

        return asyncio.run(invoke())

    start_arguments = [
        {
            "context_id": packet["context_id"],
            "operation_id": f"timer-start-race-{index}",
            "description": f"Synthetic timer start {index}",
        }
        for index in (1, 2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda args: run_call("docket_start_work_session", args), start_arguments)
        )
    assert sum(result.get("disposition") == "committed" for result in results) == 1, results
    rejected = next(result for result in results if result.get("ok") is False)
    assert rejected["error"]["code"] == "primary_timer_exists", rejected
    winner_index = next(
        index for index, result in enumerate(results) if result.get("disposition") == "committed"
    )
    winner = results[winner_index]
    old_ref = winner["work_records"][0]["ref"]
    assert rejected["error"]["details"]["work_ref"] == old_ref
    cold_read = """
import asyncio, sys
from docket.agent_auth import AgentPrincipal, AgentCallContext, agent_call_context
from docket.config import get_settings
from docket.database import configure_database
from docket.plugin.server import PluginMCP
settings = get_settings()
configure_database(settings.database_url)
permissions = frozenset({'read', 'stage', 'commit', 'resolve_conflict'})
principal = AgentPrincipal(sys.argv[1], sys.argv[2], 'interactive', permissions)
async def check():
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        result = (await PluginMCP(settings).call_tool(
            'docket_get_work_context', {'refs':[sys.argv[3]]})).structured_content
        assert result['ok'] and any(
            item['ref'] == sys.argv[3] and item['state']['status'] == 'running'
            for item in result['objects'])
    finally:
        agent_call_context.reset(token)
asyncio.run(check())
"""
    cold = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", cold_read, principal.principal_ref, principal.operator_ref, old_ref],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if cold.returncode != 0:
        raise AssertionError("Fresh-process timer read failed: " + cold.stderr[-1500:])
    replay = await asyncio.to_thread(
        run_call,
        "docket_start_work_session",
        start_arguments[winner_index],
        synchronize=False,
    )
    assert replay.pop("replayed") and replay == winner
    # A fresh MCP service instance observes the retained primary timer.
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        fresh = PluginMCP(settings)
        packet = (
            await fresh.call_tool("docket_get_work_context", {"refs": [old_ref]})
        ).structured_content
        assert any(
            item["ref"] == old_ref and item["state"]["status"] == "running"
            for item in packet["objects"]
        )
        previews = []
        for index in (1, 2):
            preview = (
                await fresh.call_tool(
                    "docket_switch_work_session",
                    {
                        "context_id": packet["context_id"],
                        "operation_id": f"timer-switch-race-{index}",
                        "from_work_ref": old_ref,
                        "description": f"Synthetic switched timer {index}",
                        "preview": True,
                    },
                )
            ).structured_content
            assert preview.get("disposition") == "prepared", preview
            previews.append(preview)
    finally:
        agent_call_context.reset(token)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        switched = list(
            pool.map(
                lambda preview: run_call(
                    "docket_apply_change",
                    {
                        "change_id": preview["change_id"],
                    },
                ),
                previews,
            )
        )
    assert sum(result.get("disposition") == "committed" for result in switched) == 1, switched
    rejected = next(result for result in switched if result.get("ok") is False)
    assert rejected["error"]["code"] in {"changeset_validation_failed", "work_version_conflict"}, (
        rejected
    )
    winner = next(result for result in switched if result.get("disposition") == "committed")
    old, new = winner["work_records"]
    assert old["ref"] == old_ref and old["status"] == "recorded" and new["status"] == "running"
    with factory() as session:
        active = list(
            session.scalars(
                select(WorkSession).where(WorkSession.lifecycle.in_(["running", "paused"]))
            )
        )
        assert len(active) == 1 and active[0].ref_id == new["ref"]
        old_revision = session.scalar(
            select(WorkSessionRevision).where(
                WorkSessionRevision.work_ref == old_ref,
                WorkSessionRevision.revision == 2,
            )
        )
        new_revision = session.scalar(
            select(WorkSessionRevision).where(
                WorkSessionRevision.work_ref == new["ref"],
                WorkSessionRevision.revision == 1,
            )
        )
        assert old_revision.end_at == new_revision.start_at
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        packet = (
            await fresh.call_tool("docket_get_work_context", {"refs": [new["ref"]]})
        ).structured_content
        stopped = (
            await fresh.call_tool(
                "docket_stop_work_session",
                {
                    "context_id": packet["context_id"],
                    "operation_id": "timer-race-clean-stop",
                    "work_ref": new["ref"],
                },
            )
        ).structured_content
        assert stopped.get("disposition") == "committed", stopped
    finally:
        agent_call_context.reset(token)


async def check_native_lineage(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import (
        WorkLineageEdge,
        WorkQuantityAllocation,
        WorkQuantityBasis,
        WorkQuantityOrigin,
        WorkSession,
        WorkSessionRevision,
    )
    from docket.plugin.server import PluginMCP

    minute = 60_000_000

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    packet = await call("docket_get_work_context", {})
    logged = await call(
        "docket_log_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-lineage-log",
            "description": "Synthetic lineage",
            "duration_minutes": 90,
            "work_date": "2001-01-02",
        },
    )
    assert logged.get("disposition") == "committed", logged
    work_ref = logged["work_records"][0]["ref"]
    historical = await call(
        "docket_query_work_sessions",
        {
            "date_from": "2001-01-01",
            "date_to": "2001-01-08",
        },
    )
    for index, minutes in enumerate((120, 60, 120)):
        packet = await call("docket_get_work_context", {"refs": [work_ref]})
        changed = await call(
            "docket_update_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-lineage-correct-{index}",
                "work_ref": work_ref,
                "duration_minutes": minutes,
            },
        )
        assert changed.get("disposition") == "committed", changed
    packet = await call("docket_get_work_context", {"refs": [work_ref]})
    previews = []
    for index, durations in enumerate(((45, 75), (60, 60))):
        preview = await call(
            "docket_split_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-lineage-split-{index}",
                "work_ref": work_ref,
                "preview": True,
                "parts": [{"duration_minutes": value} for value in durations],
            },
        )
        assert preview.get("disposition") == "prepared", preview
        previews.append(preview)
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, previews))
    assert sum(r.get("disposition") == "committed" for r in results) == 1, results
    winner = next(r for r in results if r.get("disposition") == "committed")
    children = [r for r in winner["work_records"] if not r["superseded"]]
    assert len(children) == 2
    with factory() as session:
        origin = session.scalar(
            select(WorkQuantityOrigin).where(WorkQuantityOrigin.source_work_ref == work_ref)
        )
        origin_ref = origin.ref_id
        assert origin.next_offset_us == 120 * minute
    packet = await call("docket_get_work_context", {"refs": [r["ref"] for r in children]})

    def grow(child):
        barrier.wait(timeout=10)
        return asyncio.run(
            call(
                "docket_update_work_session",
                {
                    "context_id": packet["context_id"],
                    "operation_id": "pg-grow-" + child["ref"],
                    "work_ref": child["ref"],
                    "duration_minutes": child["duration_us"] // minute + 15,
                },
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(grow, children))
    assert all(r.get("disposition") == "committed" for r in results), results
    with factory() as session:
        origin = session.scalar(
            select(WorkQuantityOrigin).where(WorkQuantityOrigin.ref_id == origin_ref)
        )
        assert origin.next_offset_us == 150 * minute
        revision_refs = (
            select(WorkSessionRevision.ref_id)
            .join(
                WorkSession,
                (WorkSession.ref_id == WorkSessionRevision.work_ref)
                & (WorkSession.version == WorkSessionRevision.revision),
            )
            .where(
                WorkSession.ref_id.in_([r["ref"] for r in children]),
            )
        )
        allocations = session.scalars(
            select(WorkQuantityAllocation)
            .where(
                WorkQuantityAllocation.revision_ref.in_(revision_refs),
            )
            .order_by(WorkQuantityAllocation.start_us)
        ).all()
        assert sum(a.end_us - a.start_us for a in allocations) == 150 * minute
        assert all(a.end_us <= b.start_us for a, b in pairwise(allocations))
        basis_ref = allocations[0].revision_ref
        edge_id = session.scalar(select(WorkLineageEdge.id))
    for statement, message in (
        (
            update(WorkQuantityOrigin)
            .where(WorkQuantityOrigin.ref_id == origin_ref)
            .values(next_offset_us=0),
            "append-only",
        ),
        (
            update(WorkQuantityBasis)
            .where(WorkQuantityBasis.revision_ref == basis_ref)
            .values(sealed=False),
            "append-only",
        ),
        (
            update(WorkQuantityAllocation)
            .where(WorkQuantityAllocation.revision_ref == basis_ref)
            .values(start_us=0),
            "append-only",
        ),
        (delete(WorkLineageEdge).where(WorkLineageEdge.id == edge_id), "append-only"),
        (
            WorkQuantityAllocation.__table__.insert().values(
                revision_ref=basis_ref,
                ordinal=999,
                origin_ref=origin_ref,
                start_us=0,
                end_us=1,
                needs_review=False,
            ),
            "sealed",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL permitted rewriting retained work lineage")
    packet = await call("docket_get_work_context", {"refs": [r["ref"] for r in children]})
    merged = await call(
        "docket_merge_work_sessions",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-lineage-merge",
            "work_refs": [r["ref"] for r in children],
        },
    )
    assert merged.get("disposition") == "committed", merged
    assert (
        next(r for r in merged["work_records"] if not r["superseded"])["duration_us"]
        == 150 * minute
    )
    current = await call(
        "docket_query_work_sessions", {"date_from": "2001-01-01", "date_to": "2001-01-08"}
    )
    retained = await call("docket_query_work_sessions", {"cursor": historical["report_cursor"]})
    assert current["totals"]["known_reported_us"] == 150 * minute
    assert retained["totals"]["known_reported_us"] == 90 * minute


async def check():
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.config import get_settings
    from docket.database import configure_database, get_session_factory
    from docket.models import (
        AuthenticatedRequest,
        Engagement,
        EngagementRevision,
        WorkLineageEdge,
        WorkQuantityAllocation,
        WorkQuantityBasis,
        WorkQuantityOrigin,
        WorkReportMember,
        WorkReportSnapshot,
        WorkSegment,
        WorkSession,
        WorkSessionRevision,
    )
    from docket.plugin.server import PluginMCP

    settings = get_settings()
    url = make_url(settings.database_url)
    assert settings.environment.value == "test" and not settings.external_writes_enabled
    assert url.get_backend_name() == "postgresql"
    assert url.database in {"docket_work_test", "docket_work_smoke"}
    assert url.host in {"127.0.0.1", "localhost", "postgres"}
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    engine = configure_database(settings.database_url)
    factory = get_session_factory()
    tables = (
        Engagement,
        EngagementRevision,
        WorkSession,
        WorkSessionRevision,
        WorkSegment,
        WorkReportSnapshot,
        WorkReportMember,
        WorkQuantityOrigin,
        WorkQuantityBasis,
        WorkQuantityAllocation,
        WorkLineageEdge,
    )
    for model in tables:
        columns = {
            column["name"]: column for column in inspect(engine).get_columns(model.__tablename__)
        }
        assert set(columns) == set(model.__table__.columns.keys())
        for column in model.__table__.columns:
            assert columns[column.name]["nullable"] == column.nullable
            assert columns[column.name]["type"]._type_affinity == column.type._type_affinity
        with engine.connect() as connection:
            assert not connection.execute(select(model).limit(1)).first()
    command.downgrade(config, "20261104e3f4")
    assert all(model.__tablename__ not in inspect(engine).get_table_names() for model in tables)
    command.upgrade(config, "head")

    principal = AgentPrincipal(
        "synthetic-work-storage",
        settings.canonical_operator_ref,
        "interactive",
        frozenset({"read", "stage", "commit", "resolve_conflict"}),
    )
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        server = PluginMCP(settings)
        overview = (await server.call_tool("docket_get_overview", {})).structured_content
        created = (
            await server.call_tool(
                "docket_create_item",
                {
                    "context_id": overview["context_id"],
                    "operation_id": "work-storage-fixture",
                    "title": "Synthetic storage fixture",
                    "kind": "test.work_storage",
                },
            )
        ).structured_content
        assert created.get("disposition") == "committed", created
        packet = (await server.call_tool("docket_get_work_context", {})).structured_content
        logged_arguments = {
            "context_id": packet["context_id"],
            "operation_id": "canonical-work-fixture",
            "description": "Synthetic performed work",
            "duration_minutes": 150,
            "work_date": "2026-11-05",
            "reported_compensation_class": "billable",
            "attribution_label": "Synthetic unconfigured arrangement",
        }
        logged = (
            await server.call_tool("docket_log_work_session", logged_arguments)
        ).structured_content
        assert logged.get("disposition") == "committed", logged
        replay = (
            await server.call_tool("docket_log_work_session", logged_arguments)
        ).structured_content
        assert replay["replayed"] and replay["changeset_ref"] == logged["changeset_ref"]
        with factory() as session:
            canonical_ref = session.scalar(select(WorkSession.ref_id))
        packet = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [canonical_ref],
                },
            )
        ).structured_content
        assert packet["objects"][0]["state"]["duration_us"] == 9_000_000_000
        previews = []
        for index in (1, 2):
            proposal = (
                await server.call_tool(
                    "docket_update_work_session",
                    {
                        "context_id": packet["context_id"],
                        "operation_id": f"racing-work-{index}",
                        "work_ref": canonical_ref,
                        "outcome": f"Synthetic correction {index}",
                        "preview": True,
                    },
                )
            ).structured_content
            assert proposal.get("change_id"), proposal
            previews.append(proposal["change_id"])
        report = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "date_from": "2026-11-02",
                    "date_to": "2026-11-09",
                    "limit": 1,
                },
            )
        ).structured_content
        assert report.get("ok"), report
        assert report["totals"]["known_reported_us"] == 9_000_000_000
        report_cursor = report["report_cursor"]
        from docket.models import Entity

        with factory.begin() as session:
            organization = Entity(
                entity_kind="organization",
                display_name="Synthetic report employer",
                normalized_name="synthetic report employer",
                created_by_changeset_ref=logged["changeset_ref"],
                basis_refs=[],
            )
            project = Entity(
                entity_kind="project",
                display_name="Synthetic report project",
                normalized_name="synthetic report project",
                created_by_changeset_ref=logged["changeset_ref"],
                basis_refs=[],
            )
            session.add_all([organization, project])
            session.flush()
            org_ref, project_ref = organization.ref_id, project.ref_id
        choices = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [org_ref, project_ref],
                },
            )
        ).structured_content
        arrangement = (
            await server.call_tool(
                "docket_create_engagement",
                {
                    "context_id": choices["context_id"],
                    "operation_id": "report-employer-fixture",
                    "label": "Synthetic report employer",
                    "organization_ref": org_ref,
                },
            )
        ).structured_content
        assert arrangement.get("disposition") == "committed", arrangement
        with factory() as session:
            eng_ref = session.scalar(select(Engagement.ref_id))
        choices = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [eng_ref, project_ref],
                },
            )
        ).structured_content
        intervals = [
            {
                "start": "2026-11-01T01:30:00-07:00",
                "end": "2026-11-01T01:15:00-08:00",
                "breaks": [
                    {"start": "2026-11-01T01:50:00-07:00", "end": "2026-11-01T01:00:00-08:00"}
                ],
            },
            {"start": "2026-11-01T01:40:00-07:00", "end": "2026-11-01T01:10:00-08:00"},
            {"start": "2026-11-01T23:30:00-08:00", "end": "2026-11-02T00:30:00-08:00"},
        ]
        for index, fields in enumerate(intervals):
            result = (
                await server.call_tool(
                    "docket_log_work_session",
                    {
                        "context_id": choices["context_id"],
                        "operation_id": f"report-interval-{index}",
                        "description": f"Synthetic reporting interval {index}",
                        "engagement_ref": eng_ref,
                        "project_ref": project_ref,
                        "timezone": "America/Los_Angeles",
                        **fields,
                    },
                )
            ).structured_content
            assert result.get("disposition") == "committed", result
        for group in ("day", "week", "organization", "project"):
            result = (
                await server.call_tool(
                    "docket_query_work_sessions",
                    {
                        "date_from": "2020-01-01",
                        "date_to": "2030-01-01",
                        "group_by": group,
                        "timezone": "America/Los_Angeles",
                        "organization_ref": org_ref,
                    },
                )
            ).structured_content
            assert result.get("ok"), result
            assert result["totals"]["timed_us"] == 125 * 60_000_000, result
            assert result["totals"]["unique_timed_us"] == 105 * 60_000_000, result
            assert result["totals"]["overlap_us"] == 20 * 60_000_000
            if group == "project":
                assert result["groups"][0]["bucket"] == project_ref
            if group == "organization":
                assert result["groups"][0]["bucket"] == org_ref
        retained_report = result
        zero = (
            await server.call_tool(
                "docket_log_work_session",
                {
                    "context_id": choices["context_id"],
                    "operation_id": "report-break-only-window",
                    "description": "Synthetic active time outside the selected break day",
                    "engagement_ref": eng_ref,
                    "start": "2026-10-30T23:00:00-07:00",
                    "end": "2026-11-02T01:00:00-08:00",
                    "breaks": [
                        {
                            "start": "2026-10-31T00:00:00-07:00",
                            "end": "2026-11-02T00:00:00-08:00",
                        }
                    ],
                },
            )
        ).structured_content
        assert zero.get("disposition") == "committed", zero
        zero = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "engagement_ref": eng_ref,
                    "date_from": "2026-10-31",
                    "date_to": "2026-11-01",
                    "group_by": "day",
                    "timezone": "America/Los_Angeles",
                },
            )
        ).structured_content
        assert zero.get("ok"), zero
        assert zero["totals"]["session_count"] == 1 and zero["totals"]["known_reported_us"] == 0
        assert zero["groups"][0]["bucket"] == "no_active_time"
        for export_format in ("json", "csv"):
            chunks = []
            arguments = {
                "cursor": retained_report["report_cursor"],
                "format": export_format,
                "limit": 1,
            }
            for _ in range(30):
                page = (
                    await server.call_tool("docket_query_work_sessions", arguments)
                ).structured_content
                assert page.get("ok") and page["totals"] == retained_report["totals"], page
                assert (
                    len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
                    <= 16384
                )
                chunks.append(page["content"])
                if not page.get("next_cursor"):
                    break
                arguments = {"cursor": page["next_cursor"], "limit": 1}
            else:
                raise AssertionError("PostgreSQL work export did not terminate")
            if export_format == "json":
                document = json.loads("".join(chunks))
                assert len(document["entries"]) == 3
            else:
                import csv
                import io

                rows = list(csv.DictReader(io.StringIO("".join(chunks))))
                assert len([row for row in rows if row["record_type"] == "work"]) == 3
    finally:
        agent_call_context.reset(context_token)

    correction_barrier = Barrier(2)

    def apply_correction(change_id):
        async def invoke():
            token = agent_call_context.set(AgentCallContext(principal))
            try:
                endpoint = PluginMCP(settings)
                correction_barrier.wait(timeout=10)
                return (
                    await endpoint.call_tool(
                        "docket_apply_change",
                        {
                            "change_id": change_id,
                        },
                    )
                ).structured_content
            finally:
                agent_call_context.reset(token)

        return asyncio.run(invoke())

    with ThreadPoolExecutor(max_workers=2) as pool:
        corrections = list(pool.map(apply_correction, previews))
    assert sum(result.get("disposition") == "committed" for result in corrections) == 1, corrections
    assert sum(result.get("ok") is False for result in corrections) == 1, corrections
    rejected = next(result for result in corrections if result.get("ok") is False)
    assert rejected["error"]["code"] in {
        "work_version_conflict",
        "changeset_validation_failed",
    }, rejected
    with factory() as session:
        head = session.scalar(select(WorkSession).where(WorkSession.ref_id == canonical_ref))
        assert head.version == 2
        revisions = list(
            session.scalars(
                select(WorkSessionRevision)
                .where(
                    WorkSessionRevision.work_ref == canonical_ref,
                )
                .order_by(WorkSessionRevision.revision)
            )
        )
        assert len(revisions) == 2
        assert revisions[0].outcome is None and revisions[1].outcome
        assert all(row.duration_us == 9_000_000_000 for row in revisions)
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        old = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "cursor": report_cursor,
                },
            )
        ).structured_content
        assert old["totals"] == report["totals"] and old["items"][0].get("outcome") is None
    finally:
        agent_call_context.reset(context_token)
    with factory() as session:
        snapshot = session.scalar(select(WorkReportSnapshot))
        member = session.scalar(
            select(WorkReportMember).where(WorkReportMember.report_id == snapshot.id)
        )
        snapshot_id, member_ref, revision_ref = snapshot.id, member.work_ref, member.revision_ref
    for statement in (
        update(WorkReportSnapshot)
        .where(WorkReportSnapshot.id == snapshot_id)
        .values(sealed_at=None),
        delete(WorkReportSnapshot).where(WorkReportSnapshot.id == snapshot_id),
        update(WorkReportMember)
        .where(WorkReportMember.report_id == snapshot_id)
        .values(revision_ref=revision_ref),
        delete(WorkReportMember).where(WorkReportMember.report_id == snapshot_id),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert "append-only" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL modified a retained report basis")
    try:
        with factory.begin() as session:
            session.execute(
                WorkReportMember.__table__.insert().values(
                    report_id=snapshot_id,
                    work_ref=member_ref,
                    revision_ref=revision_ref,
                )
            )
    except DBAPIError as exc:
        assert "open owned snapshot" in str(exc.orig)
    else:
        raise AssertionError("PostgreSQL appended to a sealed work report")
    await check_native_timers(settings, factory, principal)
    await check_native_lineage(settings, factory, principal)
    changeset_ref = created["changeset_ref"]
    with factory.begin() as session:
        request_ref = session.scalar(
            select(AuthenticatedRequest.ref_id).where(
                AuthenticatedRequest.committed_changeset_ref == changeset_ref,
            )
        )
        assert request_ref
        engagement = Engagement(
            operator_ref=principal.operator_ref, created_by_changeset_ref=changeset_ref
        )
        work = WorkSession(
            operator_ref=principal.operator_ref,
            lifecycle="recorded",
            created_by_changeset_ref=changeset_ref,
        )
        session.add_all([engagement, work])
        session.flush()
        terms = EngagementRevision(
            engagement_ref=engagement.ref_id,
            revision=1,
            label="Synthetic engagement",
            kind="unknown",
            basis_refs=[request_ref],
            created_by_changeset_ref=changeset_ref,
        )
        revision = WorkSessionRevision(
            work_ref=work.ref_id,
            revision=1,
            lifecycle="recorded",
            superseded=False,
            description="Synthetic work",
            time_precision="interval",
            duration_us=9_000_000_000,
            start_at=datetime(2026, 11, 5, 18, tzinfo=UTC),
            end_at=datetime(2026, 11, 5, 20, 30, tzinfo=UTC),
            start_offset_seconds=-28800,
            end_offset_seconds=-28800,
            timezone="America/Los_Angeles",
            work_date=date(2026, 11, 5),
            engagement_ref=engagement.ref_id,
            reported_compensation_class="billable",
            source_method="report",
            basis_refs=[request_ref],
            source_refs=[],
            created_by_changeset_ref=changeset_ref,
        )
        session.add_all([terms, revision])
        session.flush()
        segment = WorkSegment(
            revision_id=revision.id,
            ordinal=0,
            kind="active",
            start_at=revision.start_at,
            end_at=revision.end_at,
            start_offset_seconds=-28800,
            end_offset_seconds=-28800,
        )
        session.add(segment)
        session.flush()
        retained = [
            (EngagementRevision, terms.id),
            (WorkSessionRevision, revision.id),
            (WorkSegment, segment.id),
        ]
        heads = [(Engagement, engagement.id), (WorkSession, work.id)]
    for model, identity in retained:
        # Core SQL intentionally bypasses the ORM guards to prove PostgreSQL protection.
        for statement in (
            update(model).where(model.id == identity).values(id=identity),
            delete(model).where(model.id == identity),
        ):
            try:
                with factory.begin() as session:
                    session.execute(statement)
            except DBAPIError as exc:
                assert "append-only" in str(exc.orig)
            else:
                raise AssertionError("PostgreSQL allowed immutable work history to change")
    for already_sealed in (True, False):
        try:
            with factory.begin() as session:
                session.add(
                    WorkQuantityBasis(
                        revision_ref=revision.ref_id,
                        status="resolved",
                        sealed=already_sealed,
                        created_by_changeset_ref=changeset_ref,
                    )
                )
                session.flush()
                session.execute(
                    update(WorkQuantityBasis)
                    .where(
                        WorkQuantityBasis.revision_ref == revision.ref_id,
                    )
                    .values(sealed=True)
                )
        except DBAPIError as exc:
            expected = (
                "assembled before sealing" if already_sealed else "does not match its revision"
            )
            assert expected in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL sealed an incomplete quantity basis")
    for model, identity in heads:
        try:
            with factory.begin() as session:
                session.execute(delete(model).where(model.id == identity))
        except DBAPIError as exc:
            assert "append-only" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL deleted a retained work head")

    timer_barrier = Barrier(2)

    def start_timer(_index):
        try:
            with factory.begin() as session:
                session.connection()
                timer_barrier.wait(timeout=10)
                timer = WorkSession(
                    operator_ref=principal.operator_ref,
                    lifecycle="running",
                    created_by_changeset_ref=changeset_ref,
                )
                session.add(timer)
                session.flush()
                revision = WorkSessionRevision(
                    work_ref=timer.ref_id,
                    revision=1,
                    lifecycle="running",
                    superseded=False,
                    description="Synthetic timer",
                    time_precision="timer",
                    duration_us=0,
                    start_at=datetime.now(UTC),
                    start_offset_seconds=0,
                    timezone="America/Los_Angeles",
                    source_method="timer",
                    basis_refs=[request_ref],
                    source_refs=[],
                    created_by_changeset_ref=changeset_ref,
                )
                session.add(revision)
                session.flush()
                session.add(
                    WorkSegment(
                        revision_id=revision.id,
                        ordinal=0,
                        kind="active",
                        start_at=revision.start_at,
                        start_offset_seconds=0,
                    )
                )
            return "created"
        except IntegrityError as exc:
            assert "uq_work_primary_timer" in str(exc.orig)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start_timer, (1, 2))) == ["conflict", "created"]
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        estimated = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "date_from": "2020-01-01",
                    "date_to": "2030-01-01",
                },
            )
        ).structured_content
        assert estimated.get("ok"), estimated
        assert estimated["totals"]["open_timer_count"] == 1
        assert estimated["totals"]["estimate_us"] > 0
    finally:
        agent_call_context.reset(context_token)
    try:
        command.downgrade(config, "20261104e3f4")
    except RuntimeError as exc:
        assert "cannot discard retained" in str(exc)
    else:
        raise AssertionError("Downgrade discarded populated work history")
    engine.dispose()
    print(
        json.dumps(
            {
                "work_schema_roundtrip": "passed",
                "canonical_work_retry": "passed",
                "canonical_correction_race": "passed",
                "native_timer_start_and_switch_races": "passed",
                "timer_fresh_process_survival": "passed",
                "native_lineage_and_concurrent_quantity_growth": "passed",
                "quantity_and_lineage_immutability": "passed",
                "work_report_aggregates_and_exports": "passed",
                "retained_report_membership": "sealed",
                "revision_immutability": "passed",
                "single_primary_timer_race": "passed",
                "retained_downgrade": "refused",
            }
        )
    )


if __name__ == "__main__":
    # Change directory before importing Docket so even import-time settings cannot
    # read the workspace .env; only explicit synthetic validation settings apply.
    with TemporaryDirectory(prefix="docket-work-storage-") as directory:
        os.chdir(directory)
        asyncio.run(check())
