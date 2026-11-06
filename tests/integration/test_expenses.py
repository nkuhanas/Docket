"""Native expense journeys keep reported costs, approvals and received money separate."""

import csv
import io
import json

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client

from docket.domain.public_refs import new_public_ref
from docket.models import Expense, ExpenseObservation, ExpenseRevision, Operation
from docket.plugin.server import PluginMCP
from docket.schemas.expenses import EXPENSE_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin


async def export_expenses(call, cursor, format):
    pieces = []
    args = {"cursor": cursor, "format": format, "limit": 1}
    for _ in range(100):
        page = await call("docket_query_expenses", args)
        assert page.get("ok"), page
        pieces.append(page["content"])
        if page["complete"]:
            return "".join(pieces)
        args = {"cursor": page["next_cursor"], "limit": 1}
    raise AssertionError("Expense export did not finish")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_expense_optional_evidence_external_identity_and_frozen_approval(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        created = await change(
            call,
            "docket_record_expense",
            [],
            "cost-missing-receipt",
            amount="120.000000001",
            currency="USD",
            description="Example hardware procurement",
            vendor="Example vendor",
            reimbursable=True,
            receipt_ref=new_public_ref("src"),
            receipt_required=True,
            external_source="example-import",
            external_id="example-cost-42",
        )
        expense = created["expenses"][0]
        ref = expense["ref"]
        assert expense["receipt_status"] == "unavailable"
        assert expense.get("incurred_on") is None
        assert "required_receipt_unavailable" in expense["flags"]
        repeated = await change(
            call,
            "docket_record_expense",
            [],
            "cost-second-import",
            amount="120.000000001",
            currency="USD",
            description="Example hardware procurement",
            vendor="Example vendor",
            reimbursable=True,
            receipt_required=True,
            external_source="example-import",
            external_id="example-cost-42",
        )
        assert repeated["expenses"][0]["ref"] == ref
        approved = await change(
            call,
            "docket_update_expense",
            [ref],
            "cost-approval",
            expense_ref=ref,
            submission_status="approved",
            claim_amount="100.000000001",
        )
        original = approved["expenses"][0]["revision_ref"]
        await change(
            call,
            "docket_update_expense",
            [ref],
            "cost-correction",
            expense_ref=ref,
            amount="150.000000001",
            incurred_on="2026-11-03",
        )
        packet = await call("docket_get_work_context", {"refs": [ref]})
        assert packet.get("ok"), packet
        state = packet["objects"][0]["state"]
        assert state["approved_amount"] == "100.000000001"
        assert state["approved_basis_ref"] == original
        assert "expense_changed_since_approval" in state["flags"]
        assert "docket_update_expense" in packet["objects"][0]["actions"]
        history = await call("docket_get_history_entry", {"ref": original, "view": "audit"})
        assert history.get("ok"), history
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Expense)) == 1
            assert session.scalar(select(func.count()).select_from(ExpenseRevision)) == 3
            assert session.scalar(select(func.count()).select_from(ExpenseObservation)) == 1
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
        context = await call("docket_get_work_context", {})
        collision = await call(
            "docket_record_expense",
            {
                "context_id": context["context_id"],
                "operation_id": "cost-collision",
                "amount": "999",
                "currency": "USD",
                "description": "Changed imported cost",
                "external_source": "example-import",
                "external_id": "example-cost-42",
            },
        )
        assert collision["error"]["code"] == "expense_external_identity_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_expense_exact_full_totals_stable_detail_and_unicode_exports(linked_plugin):
    description = 'Example laboratory, "hardware" — ' + "🧪" * 3900
    async with work_client(linked_plugin) as call:
        refs = []
        for index, (amount, currency, when) in enumerate(
            [
                ("0.000000001", "USD", "2026-11-03"),
                ("0.000000002", "USD", None),
                ("20.1", "EUR", "2026-11-04"),
                ("7", "JPY", "2026-11-04"),
            ]
        ):
            result = await change(
                call,
                "docket_record_expense",
                [],
                f"report-cost-{index}",
                amount=amount,
                currency=currency,
                incurred_on=when,
                description=description,
                reimbursable=True,
                submission_status="approved",
                claim_amount=amount,
            )
            refs.append(result["expenses"][0]["ref"])
        report = await call(
            "docket_query_expenses",
            {"date_from": "2026-11-01", "date_to": "2026-12-01", "limit": 1},
        )
        assert report.get("ok"), report
        assert report["expense_count"] == 4
        assert not report["totals_complete"] and not report["complete"]
        all_totals = list(report["totals"])
        page = report
        while not page["totals_complete"]:
            page = await call(
                "docket_query_expenses", {"cursor": page["totals_next_cursor"], "limit": 1}
            )
            assert page.get("ok"), page
            all_totals.extend(page["totals"])
        usd = next(row for row in all_totals if row["currency"] == "USD")
        assert usd["reported_cost"] == "0.000000003"
        assert usd["allocated_cost"] == "0.000000001"
        assert usd["undated_cost"] == "0.000000002"
        await change(
            call,
            "docket_update_expense",
            [refs[0]],
            "expense-after-report",
            expense_ref=refs[0],
            amount="45",
            currency="EUR",
        )
        raw = await export_expenses(call, report["export_cursor"], "json")
        decoded = json.loads(raw)
        assert len(decoded["entries"]) == 4 and len(decoded["totals"]) == 3
        assert next(row for row in decoded["entries"] if row["ref"] == refs[0])["currency"] == "USD"
        assert all(row["description"] == description for row in decoded["entries"])
        again = await export_expenses(call, report["export_cursor"], "json")
        assert raw == again
        rows = list(
            csv.DictReader(io.StringIO(await export_expenses(call, report["export_cursor"], "csv")))
        )
        assert len([row for row in rows if row["record_type"] == "expense"]) == 4
        latest = await call(
            "docket_query_expenses", {"date_from": "2026-11-01", "date_to": "2026-12-01"}
        )
        current_usd = next(row for row in latest["totals"] if row["currency"] == "USD")
        assert current_usd["reported_cost"] == "0.000000002"
        assert current_usd["approved_amount"] == "0.000000003"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_expense_actual_contracts_and_read_only_denial(linked_plugin):
    _, settings, _, _ = linked_plugin
    tools = {tool.name: tool for tool in await PluginMCP(settings).list_tools()}
    for name in EXPENSE_TOOL_PURPOSES:
        definition = tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)
        assert len(json.dumps(definition["inputSchema"], separators=(",", ":")).encode()) <= 4096
        assert len(json.dumps(definition, separators=(",", ":")).encode()) <= 6144
        assert not definition["inputSchema"].get("oneOf")
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_work_context", {})
        result = await call(
            "docket_record_expense",
            {
                "context_id": packet["context_id"],
                "operation_id": "read-only-cost",
                "amount": "1",
                "currency": "USD",
                "description": "Example cost",
            },
        )
        assert result["error"]["code"] == "agent_authority_denied"
