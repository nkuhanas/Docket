"""Real MCP lineage journeys: conserved reports, quantity identity and compact context."""

import json

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_reports import MINUTE, work_client

from docket.models import (
    Operation,
    WorkLineageEdge,
    WorkQuantityBasis,
    WorkQuantityOrigin,
    WorkSession,
)
from docket.services.work_attribution import WorkAttributionService
from docket.services.work_quantities import WorkQuantityService

linked_plugin = test_native_plugin.linked_plugin


async def change(call, name, refs, identity, **fields):
    context = await call("docket_get_work_context", {"refs": refs})
    result = await call(
        name, {"context_id": context["context_id"], "operation_id": identity, **fields}
    )
    assert result.get("disposition") in {"committed", "prepared"}, result
    return result


async def report(call):
    result = await call(
        "docket_query_work_sessions",
        {
            "date_from": "2026-11-02",
            "date_to": "2026-11-09",
            "timezone": "America/Los_Angeles",
        },
    )
    assert result.get("ok"), result
    return result


def spans_for(session, ref):
    service = WorkAttributionService(session)
    # Direct queries below use the synthetic fixture's owned head, not request auth.
    head = session.scalar(select(WorkSession).where(WorkSession.ref_id == ref))
    return WorkQuantityService(session).spans(service.work_revision(head).ref_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_duration_corrections_split_merge_keep_quantity_identity(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        logged = await change(
            call,
            "docket_log_work_session",
            [],
            "lineage-log",
            description="Example work",
            duration_minutes=90,
            work_date="2026-11-04",
            reported_compensation_class="billable",
        )
        work = logged["work_records"][0]["ref"]
        original_report = await report(call)
        with session_factory() as session:
            original = spans_for(session, work)
            origin = original[0].origin_ref
        for index, minutes in enumerate((120, 60, 120)):
            await change(
                call,
                "docket_update_work_session",
                [work],
                f"duration-{index}",
                work_ref=work,
                duration_minutes=minutes,
                description=f"Example {minutes}",
            )
        with session_factory() as session:
            current = spans_for(session, work)
            assert sum(s.quantity for s in current) == 120 * MINUTE
            assert {s.origin_ref for s in current} == {origin}
            assert session.scalar(select(WorkQuantityOrigin.next_offset_us)) == 120 * MINUTE
        preview = await change(
            call,
            "docket_split_work_session",
            [work],
            "split-preview",
            work_ref=work,
            preview=True,
            parts=[{"duration_minutes": 45}, {"duration_minutes": 75}],
        )
        applied = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert applied.get("disposition") == "committed", applied
        replay = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert replay.pop("replayed") and replay == applied
        children = [r["ref"] for r in applied["work_records"] if not r["superseded"]]
        assert len(children) == 2
        parent_context = await call("docket_get_work_context", {"refs": [work]})
        assert set(parent_context["objects"][0]["state"]["lineage"]["children"]) == set(children)
        assert "docket_update_work_session" not in parent_context["objects"][0]["actions"]
        child_context = await call("docket_get_work_context", {"refs": [children[0]]})
        assert child_context["objects"][0]["state"]["lineage"]["parents"] == [work]
        with session_factory() as session:
            left, right = [spans_for(session, ref) for ref in children]
            assert left[0].start == 0 and left[-1].end == 45 * MINUTE
            assert right[0].start == 45 * MINUTE and right[-1].end == 120 * MINUTE
            assert session.scalar(select(func.count()).select_from(WorkLineageEdge)) == 2
            assert session.scalar(select(WorkSession.superseded).where(WorkSession.ref_id == work))
        await change(
            call,
            "docket_update_work_session",
            [children[0]],
            "grow-child",
            work_ref=children[0],
            duration_minutes=60,
        )
        with session_factory() as session:
            growth = spans_for(session, children[0])[-1]
            assert growth.start == 120 * MINUTE and growth.end == 135 * MINUTE
            assert not growth.needs_review
        merged = await change(
            call, "docket_merge_work_sessions", children, "merge-duration", work_refs=children
        )
        leaf = next(r for r in merged["work_records"] if not r["superseded"])
        assert leaf["duration_us"] == 135 * MINUTE
        with session_factory() as session:
            spans = spans_for(session, leaf["ref"])
            assert sum(s.quantity for s in spans) == 135 * MINUTE
            assert {s.origin_ref for s in spans} == {origin}
            assert session.scalar(select(func.count()).select_from(WorkLineageEdge)) == 4
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            assert not session.scalars(
                select(WorkQuantityBasis).where(WorkQuantityBasis.sealed.is_(False))
            ).all()
        current = await report(call)
        assert current["totals"]["known_reported_us"] == 135 * MINUTE
        historical = await call(
            "docket_query_work_sessions", {"cursor": original_report["report_cursor"]}
        )
        assert historical["totals"]["known_reported_us"] == 90 * MINUTE
        # Retired units from a shrunk parent are not silently made available as
        # fresh, financially clear work when a split child subsequently grows.
        retired = await change(
            call,
            "docket_log_work_session",
            [],
            "retired-origin",
            description="Example retained quantity",
            duration_minutes=100,
            work_date="2002-01-02",
        )
        retired_ref = retired["work_records"][0]["ref"]
        await change(
            call,
            "docket_update_work_session",
            [retired_ref],
            "shrink-before-split",
            work_ref=retired_ref,
            duration_minutes=60,
        )
        split = await change(
            call,
            "docket_split_work_session",
            [retired_ref],
            "split-shrunk",
            work_ref=retired_ref,
            parts=[{"duration_minutes": 30}, {"duration_minutes": 30}],
        )
        child = next(r["ref"] for r in split["work_records"] if not r["superseded"])
        await change(
            call,
            "docket_update_work_session",
            [child],
            "grow-after-retirement",
            work_ref=child,
            duration_minutes=45,
        )
        with session_factory() as session:
            tail = spans_for(session, child)[-1]
            assert tail.start == 100 * MINUTE and tail.end == 115 * MINUTE
            assert tail.needs_review


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_timed_split_merge_preserves_breaks_and_stale_preview(session_factory, linked_plugin):
    async with work_client(linked_plugin) as call:
        logged = await change(
            call,
            "docket_log_work_session",
            [],
            "timed-parent",
            description="Example hardware validation",
            timezone="America/Los_Angeles",
            start="2026-11-04T10:00:00-08:00",
            end="2026-11-04T14:00:00-08:00",
            breaks=[{"start": "2026-11-04T11:00:00-08:00", "end": "2026-11-04T13:00:00-08:00"}],
        )
        work = logged["work_records"][0]["ref"]
        context = await call("docket_get_work_context", {"refs": [work]})
        bad = await call(
            "docket_split_work_session",
            {
                "context_id": context["context_id"],
                "operation_id": "invalid-split",
                "work_ref": work,
                "parts": [
                    {"start": "2026-11-04T10:00:00-08:00", "end": "2026-11-04T13:30:00-08:00"},
                    {"start": "2026-11-04T13:00:00-08:00", "end": "2026-11-04T14:00:00-08:00"},
                ],
            },
        )
        assert bad["error"]["code"] == "work_lineage_invalid"
        preview = await change(
            call,
            "docket_split_work_session",
            [work],
            "timed-split",
            work_ref=work,
            preview=True,
            parts=[
                {"start": "2026-11-04T10:00:00-08:00", "end": "2026-11-04T12:00:00-08:00"},
                {"start": "2026-11-04T12:00:00-08:00", "end": "2026-11-04T14:00:00-08:00"},
            ],
        )
        await change(
            call,
            "docket_update_work_session",
            [work],
            "before-split-correction",
            work_ref=work,
            outcome="Checked the device",
        )
        stale = await call("docket_apply_change", {"change_id": preview["change_id"]})
        assert stale.get("disposition") != "committed", stale
        split = await change(
            call,
            "docket_split_work_session",
            [work],
            "fresh-timed-split",
            work_ref=work,
            parts=[
                {"start": "2026-11-04T10:00:00-08:00", "end": "2026-11-04T12:00:00-08:00"},
                {"start": "2026-11-04T12:00:00-08:00", "end": "2026-11-04T14:00:00-08:00"},
            ],
        )
        refs = [r["ref"] for r in split["work_records"] if not r["superseded"]]
        merged = await change(
            call, "docket_merge_work_sessions", refs, "timed-merge", work_refs=list(reversed(refs))
        )
        leaf = next(r for r in merged["work_records"] if not r["superseded"])
        assert leaf["duration_us"] == 120 * MINUTE
        with session_factory() as session:
            head = session.scalar(select(WorkSession).where(WorkSession.ref_id == leaf["ref"]))
            value = WorkAttributionService(session).work_spec(head)
            assert [(s.kind, s.start_at.hour, s.end_at.hour) for s in value.time.segments] == [
                ("active", 18, 19),
                ("break", 19, 21),
                ("active", 21, 22),
            ]
            spans = spans_for(session, leaf["ref"])
            assert spans[0].start == 0 and spans[-1].end == 120 * MINUTE


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_maximum_merge_uses_compact_context_and_preserves_all_quantity(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        refs = []
        for index in range(25):
            logged = await change(
                call,
                "docket_log_work_session",
                [],
                f"mass-{index}",
                description="Example batch " + "d" * 3000,
                duration_minutes=5,
                work_date="2026-11-04",
            )
            refs.append(logged["work_records"][0]["ref"])
        packet = await call("docket_get_work_context", {"refs": refs})
        assert len(packet["objects"]) == 25
        assert len(json.dumps(packet, ensure_ascii=False).encode()) <= 8192
        merged = await call(
            "docket_merge_work_sessions",
            {
                "context_id": packet["context_id"],
                "operation_id": "maximum-merge",
                "work_refs": refs,
            },
        )
        assert merged.get("disposition") == "committed", merged
        assert not merged["work_records"][0]["superseded"]
        assert merged["work_records"][0]["duration_us"] == 125 * MINUTE
        with session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(WorkSession)
                    .where(WorkSession.superseded.is_(False))
                )
                == 1
            )
            assert session.scalar(select(func.count()).select_from(WorkLineageEdge)) == 25
        totals = await report(call)
        assert totals["totals"]["known_reported_us"] == 125 * MINUTE
