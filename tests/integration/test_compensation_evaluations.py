"""First-call MCP evaluation, frozen continuation and no financial/provider effects."""

import csv
import io
import json

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client

from docket.domain.public_refs import new_public_ref
from docket.models import CompensationEvaluation, Engagement, Operation, WorkSessionRevision
from docket.plugin.server import PluginMCP

linked_plugin = test_native_plugin.linked_plugin


async def exported(call, cursor, mode):
    parts = []
    arguments = {"cursor": cursor, "format": mode}
    for _ in range(100):
        page = await call("docket_get_compensation", arguments)
        assert page.get("ok"), page
        parts.append(page["content"])
        if page["complete"]:
            return "".join(parts)
        arguments = {"cursor": page["next_cursor"]}
    raise AssertionError("Compensation export did not terminate")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_native_compensation_freezes_rates_work_history_and_export_basis(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        created = await change(
            call,
            "docket_create_engagement",
            [],
            "evaluation-eng",
            label="Example calculation engagement",
            kind="contract",
        )
        with session_factory() as session:
            eng = session.scalar(
                select(Engagement.ref_id).where(
                    Engagement.created_by_changeset_ref == created["changeset_ref"]
                )
            )
        for index, day in enumerate(("2026-11-03", "2026-11-04")):
            logged = await change(
                call,
                "docket_log_work_session",
                [eng],
                f"evaluation-work-{index}",
                description="Example reported work",
                engagement_ref=eng,
                duration_minutes=90,
                work_date=day,
                timezone="UTC",
                reported_compensation_class="billable",
            )
            if index == 0:
                first_work = logged["work_records"][0]["ref"]
        await change(
            call,
            "docket_log_work_session",
            [eng],
            "evaluation-incomplete",
            description="Example unquantified work",
            engagement_ref=eng,
            work_date="2026-11-04",
            timezone="UTC",
        )
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "evaluation-pay-base",
            engagement_ref=eng,
            effective_from="2026-11-01",
            timezone="UTC",
            rate="80",
            currency="USD",
            compensable=True,
            billable=True,
        )
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "evaluation-pay-future",
            engagement_ref=eng,
            effective_from="2026-11-04",
            rate="100",
        )
        query = {
            "engagement_ref": eng,
            "date_from": "2026-11-02",
            "date_to": "2026-11-09",
            "timezone": "UTC",
            "limit": 1,
        }
        first = await call("docket_get_compensation", query)
        assert first.get("ok"), first
        assert first["summary"]["gross_estimates"] == [
            {"currency": "USD", "amount": "270.00", "money_decimals": 2}
        ]
        assert first["summary"]["unknown_quantity_count"] == 1
        assert first["summary"]["unresolved_gross_count"] == 1
        assert len(first["contributions"]) == 1 and first.get("next_cursor")
        await change(
            call,
            "docket_update_work_session",
            [first_work],
            "evaluation-correct-work",
            work_ref=first_work,
            duration_minutes=120,
        )
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "evaluation-correct-pay",
            engagement_ref=eng,
            effective_from="2026-11-01",
            effective_to="2026-11-04",
            rate="200",
        )
        continuation = await call("docket_get_compensation", {"cursor": first["next_cursor"]})
        assert continuation["summary"] == first["summary"]
        original = json.loads(await exported(call, first["export_cursor"], "json"))
        assert original["summary"] == first["summary"] and len(original["contributions"]) == 3
        assert {term["terms"].get("rate") for term in original["policy_basis"]} == {"80", "100"}
        csv_rows = list(
            csv.DictReader(io.StringIO(await exported(call, first["export_cursor"], "csv")))
        )
        assert len(csv_rows) == 3
        assert {row["evaluation_ref"] for row in csv_rows} == {first["evaluation_ref"]}
        current = await call("docket_get_compensation", query)
        assert current["summary"]["gross_estimates"][0]["amount"] == "550.00"
        historical = await call(
            "docket_get_compensation", {**query, "as_of": first["parameters"]["as_of"]}
        )
        assert historical["summary"] == first["summary"]
        mismatch = await call(
            "docket_get_compensation", {"cursor": first["export_cursor"], "date_from": "2026-11-03"}
        )
        assert mismatch["error"]["code"] == "compensation_cursor_filter_mismatch"
        with session_factory() as session:
            row = session.scalar(
                select(CompensationEvaluation).where(
                    CompensationEvaluation.ref_id == first["evaluation_ref"]
                )
            )
            assert row.calculation_version == "compensation-v1"
            assert len(row.result_json["retained"]) == 3
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            assert (
                session.scalar(
                    select(WorkSessionRevision.reported_compensation_class)
                    .where(WorkSessionRevision.work_ref == first_work)
                    .order_by(WorkSessionRevision.revision.desc())
                    .limit(1)
                )
                == "billable"
            )
            row.calculation_version = "changed"
            with pytest.raises(ValueError, match="immutable"):
                session.flush()
            session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_read_only_evaluation_and_compact_real_contract(linked_plugin):
    from docket.services.compensation_evaluations import EvaluationCursor

    _app, settings, _token, _principal = linked_plugin
    server = PluginMCP(settings)
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["docket_get_compensation"].model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert len(json.dumps(tool["inputSchema"], separators=(",", ":")).encode()) <= 4096
    assert len(json.dumps(tool, separators=(",", ":")).encode()) <= 6144
    assert not tool["inputSchema"].get("oneOf")
    async with work_client(linked_plugin) as call:
        empty = await call("docket_get_compensation", {})
        assert empty.get("ok") and empty["contribution_count"] == 0, empty
        assert empty["summary"]["gross_estimates"] == []
        forged = EvaluationCursor(ref=new_public_ref("ceval")).encode()
        denied = await call("docket_get_compensation", {"cursor": forged})
        assert denied["error"]["code"] == "compensation_evaluation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_bounded_compensation_pages_and_multichunk_exports(linked_plugin):
    async with work_client(linked_plugin) as call:
        created = await change(
            call,
            "docket_create_engagement",
            [],
            "long-evaluation-eng",
            label="Synthetic long evaluation",
        )
        eng = next(ref for ref in created["affected_refs"] if ref.startswith("eng_"))
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "long-evaluation-rate",
            engagement_ref=eng,
            effective_from="2026-10-01",
            timezone="UTC",
            rate="80",
            currency="USD",
            billable=True,
        )
        await change(
            call,
            "docket_log_work_session",
            [eng],
            "long-evaluation-work",
            engagement_ref=eng,
            description="Synthetic export boundary interval",
            start="2026-10-01T00:00:00+00:00",
            end="2026-11-01T00:00:00+00:00",
            timezone="UTC",
        )
        page = await call(
            "docket_get_compensation",
            {
                "engagement_ref": eng,
                "date_from": "2026-10-01",
                "date_to": "2026-11-01",
                "timezone": "UTC",
                "limit": 100,
            },
        )
        assert page.get("ok"), page
        assert page["summary"]["gross_estimates"][0]["amount"] == "59520.00"
        assert page["contribution_count"] == 31 and not page["complete"]
        count = len(page["contributions"])
        continued = await call("docket_get_compensation", {"cursor": page["next_cursor"]})
        assert count + len(continued["contributions"]) == 31 and continued["complete"]
        content = await exported(call, page["export_cursor"], "json")
        assert len(content.encode()) > 16384
        decoded = json.loads(content)
        assert len(decoded["contributions"]) == 31 and decoded["summary"] == page["summary"]
        rows = list(csv.DictReader(io.StringIO(await exported(call, page["export_cursor"], "csv"))))
        assert len(rows) == 31 and {row["as_of"] for row in rows} == {page["parameters"]["as_of"]}
