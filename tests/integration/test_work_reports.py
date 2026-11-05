"""First-attempt reporting/export journeys over retained work revisions."""

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx2
import pytest
import test_native_plugin
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from docket.models import (
    AuthenticatedRequest,
    Engagement,
    Operation,
    WorkReportMember,
    WorkReportSnapshot,
    WorkSegment,
    WorkSession,
    WorkSessionRevision,
)
from docket.models.base import utc_now
from docket.services.oauth import OAuthPrincipalService
from docket.services.work_reports import ReportCursor

linked_plugin = test_native_plugin.linked_plugin
MINUTE = 60_000_000


@asynccontextmanager
async def work_client(linked_plugin):
    app, _settings, token, _principal = linked_plugin
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), headers={"Authorization": "Bearer " + token}
        ) as http,
        Client(
            streamable_http_client("https://docket.you/mcp/", http_client=http), mode="legacy"
        ) as client,
    ):

        async def call(name, arguments):
            result = (await client.call_tool(name, arguments)).structured_content
            assert result is not None
            assert (
                len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 16384
            )
            return result

        yield call


async def export(call, report_cursor, format, limit=2):
    arguments = {"cursor": report_cursor, "format": format, "limit": limit}
    chunks = []
    for _ in range(100):
        page = await call("docket_query_work_sessions", arguments)
        assert page.get("ok"), page
        chunks.append(page["content"])
        if page.get("next_cursor") is None:
            assert page["complete"]
            return "".join(chunks), page
        arguments = {"cursor": page.get("next_cursor"), "limit": limit}
    raise AssertionError("Export did not terminate")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_report_full_totals_stable_pages_exports_and_revocation(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_work_context", {})
        for index, minutes in enumerate((750, 480, 390)):
            arranged = await call(
                "docket_create_engagement",
                {
                    "context_id": packet["context_id"],
                    "operation_id": f"report-engagement-{index}",
                    "label": f"Example employer {index}",
                },
            )
            assert arranged.get("disposition") == "committed", arranged
            with session_factory() as session:
                engagement = session.scalar(
                    select(Engagement.ref_id).where(
                        Engagement.created_by_changeset_ref == arranged["changeset_ref"],
                    )
                )
            context = await call("docket_get_work_context", {"refs": [engagement]})
            logged = await call(
                "docket_log_work_session",
                {
                    "context_id": context["context_id"],
                    "operation_id": f"report-work-{index}",
                    "description": f"Work for employer {index}",
                    "duration_minutes": minutes,
                    "engagement_ref": engagement,
                    "work_date": "2026-11-04",
                    "reported_compensation_class": "billable" if index == 0 else "paid_work",
                    "work_mode": "onsite",
                    "reported_location": "Lab, room 2\nEast wing",
                },
            )
            assert logged.get("disposition") == "committed", logged
        filters = {"date_from": "2026-11-02", "date_to": "2026-11-09", "limit": 1}
        first = await call("docket_query_work_sessions", filters)
        assert first.get("ok"), first
        assert first["totals"]["known_reported_us"] == 27 * 60 * MINUTE
        assert first["totals"]["known_reported_hours"] == "27.000000"
        assert first["totals"]["session_count"] == 3
        assert len(first["items"]) == len(first["groups"]) == 1
        target = first["items"][0]["ref"]
        selected = await call("docket_get_work_context", {"refs": [target]})
        changed = await call(
            "docket_update_work_session",
            {
                "context_id": selected["context_id"],
                "operation_id": "report-correction",
                "work_ref": target,
                "duration_minutes": 1000,
            },
        )
        assert changed.get("disposition") == "committed", changed
        items, groups, page = list(first["items"]), list(first["groups"]), first
        for _ in range(5):
            if not page.get("next_cursor"):
                break
            page = await call(
                "docket_query_work_sessions", {"cursor": page.get("next_cursor"), "limit": 1}
            )
            assert page["totals"] == first["totals"] and page["report"] == first["report"]
            items.extend(page["items"])
            groups.extend(page["groups"])
        assert page.get("next_cursor") is None and len(items) == len(groups) == 3
        assert len({item["ref"] for item in items}) == 3
        rendered, final = await export(call, first["report_cursor"], "json", limit=1)
        document = json.loads(rendered)
        assert document["totals"] == first["totals"] == final["totals"]
        assert sum(item["duration_us"] for item in document["entries"]) == 27 * 60 * MINUTE
        assert len(document["groups"]) == 3
        assert document["entries"][0]["work_mode"] == "onsite"
        rendered_csv, _ = await export(call, first["report_cursor"], "csv")
        rows = list(csv.DictReader(io.StringIO(rendered_csv)))
        assert rows[0]["record_type"] == "total" and rows[0]["known_reported_hours"] == "27.000000"
        work = [row for row in rows if row["record_type"] == "work"]
        assert len(work) == 3 and sum(int(row["duration_us"]) for row in work) == 27 * 60 * MINUTE
        assert {row["reported_compensation_class"] for row in work} == {"billable", "paid_work"}
        assert all(row["reported_location"] == "Lab, room 2\nEast wing" for row in work)
        mismatch = await call(
            "docket_query_work_sessions",
            {
                "cursor": first["report_cursor"],
                "date_to": "2026-12-01",
            },
        )
        assert mismatch["error"]["code"] == "work_report_filter_changed"
        latest = await call("docket_query_work_sessions", filters)
        assert latest["totals"]["known_reported_us"] != first["totals"]["known_reported_us"]
        with session_factory.begin() as session:
            foreign = WorkReportSnapshot(
                principal_ref="foreign",
                operator_ref="foreign",
                parameters_json={},
                as_of=utc_now(),
                sealed_at=utc_now(),
            )
            session.add(foreign)
            session.flush()
            foreign_cursor = ReportCursor(report_ref=foreign.ref_id).encode()
        forbidden = await call("docket_query_work_sessions", {"cursor": foreign_cursor})
        assert forbidden["error"]["code"] == "work_report_unavailable"
        app, settings, _token, principal = linked_plugin
        with session_factory.begin() as session:
            OAuthPrincipalService(session, settings).approve("report-reader")
        read_token = app.state.test_issue_token(sub="report-reader", scope="docket.read")
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app),
                headers={
                    "Authorization": "Bearer " + read_token,
                },
            ) as reader_http,
            Client(
                streamable_http_client("https://docket.you/mcp/", http_client=reader_http),
                mode="legacy",
            ) as reader,
        ):
            read_only = (
                await reader.call_tool("docket_query_work_sessions", filters)
            ).structured_content
            assert read_only["ok"] and read_only["totals"] == latest["totals"]
            denied_cursor = (
                await reader.call_tool(
                    "docket_query_work_sessions",
                    {
                        "cursor": first["report_cursor"],
                    },
                )
            ).structured_content
            assert denied_cursor["error"]["code"] == "work_report_unavailable"
        with session_factory.begin() as session:
            OAuthPrincipalService(session, settings).revoke(principal)
        # Middleware rejects the revoked token before a retained cursor can grant access.
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
            rejected = await http.post(
                "https://docket.you/mcp/",
                headers={
                    "Authorization": "Bearer " + _token,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "docket_query_work_sessions",
                        "arguments": {"cursor": first["report_cursor"]},
                    },
                },
            )
            assert rejected.status_code == 401
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            assert session.scalar(select(func.count()).select_from(WorkReportMember)) >= 6


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_day_week_coverage_overlap_and_unallocated_quantities(linked_plugin):
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_work_context", {})
        records = [
            {
                "start": "2026-11-01T01:30:00-07:00",
                "end": "2026-11-01T01:15:00-08:00",
                "breaks": [
                    {"start": "2026-11-01T01:50:00-07:00", "end": "2026-11-01T01:00:00-08:00"}
                ],
            },
            {"start": "2026-11-01T01:40:00-07:00", "end": "2026-11-01T01:10:00-08:00"},
            {"start": "2026-11-01T23:30:00-08:00", "end": "2026-11-02T00:30:00-08:00"},
            {"duration_minutes": 600, "period_start": "2026-10-26", "period_end": "2026-11-09"},
            {"duration_minutes": 180},
            {"work_date": "2026-11-02"},
        ]
        for index, fields in enumerate(records):
            logged = await call(
                "docket_log_work_session",
                {
                    "context_id": packet["context_id"],
                    "operation_id": f"coverage-{index}",
                    "description": f"Coverage example {index}",
                    "timezone": "America/Los_Angeles",
                    **fields,
                },
            )
            assert logged.get("disposition") == "committed", logged
        filters = {
            "date_from": "2026-11-01",
            "date_to": "2026-11-03",
            "timezone": "America/Los_Angeles",
        }
        result = await call("docket_query_work_sessions", {**filters, "group_by": "day"})
        assert result.get("ok"), result
        total = result["totals"]
        assert total["timed_us"] == 125 * MINUTE
        assert total["unique_timed_us"] == 105 * MINUTE and total["overlap_us"] == 20 * MINUTE
        assert total["known_reported_us"] == 905 * MINUTE
        assert total["allocated_us"] == 125 * MINUTE and total["unallocated_us"] == 780 * MINUTE
        assert total["unknown_quantity_count"] == 1 and total["unplaced_count"] == 1
        days = {group["bucket"]: group for group in result["groups"]}
        assert days["2026-11-01"]["timed_us"] == 95 * MINUTE
        assert days["2026-11-02"]["timed_us"] == 30 * MINUTE
        assert days["unallocated"]["duration_only_us"] == 600 * MINUTE
        assert days["unplaced"]["duration_only_us"] == 180 * MINUTE
        weekly = await call("docket_query_work_sessions", {**filters, "group_by": "week"})
        assert weekly["totals"] == total
        weeks = {group["bucket"]: group for group in weekly["groups"]}
        assert weeks["2026-10-26"]["timed_us"] == 95 * MINUTE
        assert weeks["2026-11-02"]["timed_us"] == 30 * MINUTE
        placed = await call("docket_query_work_sessions", {**filters, "include_unplaced": False})
        assert placed["totals"]["known_reported_us"] == 725 * MINUTE
        arranged = await call(
            "docket_create_engagement",
            {
                "context_id": packet["context_id"],
                "operation_id": "break-only-engagement",
                "label": "Break-only coverage fixture",
            },
        )
        assert arranged.get("disposition") == "committed", arranged
        arrangement = await call("docket_get_work_context", {"search": "Break-only"})
        engagement = arrangement["objects"][0]["ref"]
        logged = await call(
            "docket_log_work_session",
            {
                "context_id": arrangement["context_id"],
                "operation_id": "long-break-work",
                "description": "Reported split interval",
                "engagement_ref": engagement,
                "start": "2026-10-30T23:00:00-07:00",
                "end": "2026-11-02T01:00:00-08:00",
                "breaks": [
                    {"start": "2026-10-31T00:00:00-07:00", "end": "2026-11-02T00:00:00-08:00"}
                ],
            },
        )
        assert logged.get("disposition") == "committed", logged
        empty_active = await call(
            "docket_query_work_sessions",
            {
                "date_from": "2026-11-01",
                "date_to": "2026-11-02",
                "group_by": "day",
                "engagement_ref": engagement,
            },
        )
        assert empty_active["totals"]["session_count"] == len(empty_active["items"]) == 1
        assert empty_active["totals"]["known_reported_us"] == 0
        assert empty_active["groups"][0]["bucket"] == "no_active_time"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_large_lossless_exports_and_running_estimates(session_factory, linked_plugin):
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_work_context", {})
        description = '😀"\n' * 1000
        outcome = "Unicode 漢字" * 400
        created = await call(
            "docket_log_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": "large-export-work",
                "description": description,
                "outcome": outcome,
                "duration_minutes": 60,
                "work_date": "2026-11-05",
                "reported_location": "😀" * 1000,
            },
        )
        assert created.get("disposition") == "committed", created
        report = await call(
            "docket_query_work_sessions",
            {
                "date_from": "2026-01-01",
                "date_to": "2027-01-01",
            },
        )
        assert report.get("ok"), report
        text, _ = await export(call, report["report_cursor"], "json", limit=1)
        entry = json.loads(text)["entries"][0]
        assert entry["description"] == description and entry["outcome"] == outcome
        text, _ = await export(call, report["report_cursor"], "csv", limit=1)
        entry = next(
            row for row in csv.DictReader(io.StringIO(text)) if row["record_type"] == "work"
        )
        assert entry["description"] == description and entry["outcome"] == outcome
        as_of = utc_now()
        with session_factory.begin() as session:
            request = session.scalar(
                select(AuthenticatedRequest).where(
                    AuthenticatedRequest.committed_changeset_ref == created["changeset_ref"],
                )
            )
            timer = WorkSession(
                operator_ref=request.operator_ref,
                lifecycle="running",
                created_by_changeset_ref=created["changeset_ref"],
            )
            session.add(timer)
            session.flush()
            revision = WorkSessionRevision(
                work_ref=timer.ref_id,
                revision=1,
                lifecycle="running",
                superseded=False,
                description="Synthetic open timer",
                time_precision="timer",
                start_at=as_of - timedelta(minutes=30),
                start_offset_seconds=0,
                duration_us=0,
                timezone="UTC",
                source_method="timer",
                basis_refs=[request.ref_id],
                source_refs=[],
                created_by_changeset_ref=created["changeset_ref"],
                created_at=as_of - timedelta(seconds=1),
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
        estimated = await call(
            "docket_query_work_sessions",
            {
                "date_from": "2026-01-01",
                "date_to": "2027-01-01",
                "as_of": as_of.isoformat(),
            },
        )
        assert estimated.get("ok"), estimated
        assert estimated["totals"]["known_reported_us"] == 60 * MINUTE
        assert estimated["totals"]["estimate_us"] == 30 * MINUTE
        assert estimated["totals"]["open_timer_count"] == 1
