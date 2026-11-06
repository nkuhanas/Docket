"""Native invoice journeys use flat contracts and conserved work/expense claims."""

import csv
import io
import json

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_financial_documents import export_document
from test_work_lineage import change
from test_work_reports import work_client

from docket.domain.public_refs import new_public_ref
from docket.models import (
    DocumentAllocation,
    FinancialDocument,
    FinancialDocumentObservation,
    InvoiceNumber,
    Operation,
    WorkClaim,
)
from docket.plugin.server import PluginMCP
from docket.schemas.invoices import INVOICE_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin


async def fixture_work(call, suffix, minutes=120, configured=True):
    created = await change(
        call,
        "docket_create_engagement",
        [],
        suffix + "-eng",
        label="Synthetic contracting",
        kind="contract",
    )
    eng = next(ref for ref in created["affected_refs"] if ref.startswith("eng_"))
    if configured:
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            suffix + "-rate",
            engagement_ref=eng,
            effective_from="2020-01-01",
            rate="80",
            currency="USD",
            billable=True,
        )
    logged = await change(
        call,
        "docket_log_work_session",
        [eng],
        suffix + "-work",
        description="Synthetic GPU 🧪 qualification",
        engagement_ref=eng,
        duration_minutes=minutes,
        work_date="2026-11-03",
        reported_compensation_class="billable",
    )
    return eng, logged["work_records"][0]["ref"]


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_invoice_partial_overtime_shared_timesheet_expense_and_number_retry(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        eng, work = await fixture_work(call, "invoice-partial")
        await change(
            call,
            "docket_set_reporting_rules",
            [eng],
            "invoice-overtime",
            engagement_ref=eng,
            effective_from="2020-01-01",
            overtime_after_hours=1,
            overtime_multiplier="1.5",
            overtime_scope="day",
            group_with_payer=False,
        )
        sheet = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "invoice-sheet",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
        )
        sheet_ref = sheet["documents"][0]["ref"]
        await change(
            call,
            "docket_update_timesheet",
            [sheet_ref],
            "invoice-sheet-submitted",
            timesheet_ref=sheet_ref,
            status="submitted",
        )
        first = await change(
            call,
            "docket_prepare_invoice",
            [eng],
            "invoice-first",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            max_work_minutes=60,
            expense_refs=[],
        )
        assert first["documents"][0]["gross_estimates"][0]["amount"] == "80.00"
        invoice = first["documents"][0]["ref"]
        context = await call("docket_get_work_context", {"refs": [invoice]})
        arguments = {
            "context_id": context["context_id"],
            "operation_id": "invoice-first-issue",
            "invoice_ref": invoice,
            "status": "issued",
        }
        issued = await call("docket_update_invoice", arguments)
        assert issued.get("disposition") == "committed", issued
        assert issued["documents"][0]["number"] == "INV-000001"
        replay = await call("docket_update_invoice", arguments)
        assert replay.pop("replayed") and replay == issued
        expense = await change(
            call,
            "docket_record_expense",
            [eng],
            "invoice-expense",
            amount="10",
            currency="USD",
            description="Synthetic reimbursable hardware",
            engagement_ref=eng,
            incurred_on="2026-11-03",
            reimbursable=True,
        )
        fee = expense["expenses"][0]["ref"]
        second = await change(
            call,
            "docket_prepare_invoice",
            [eng],
            "invoice-second",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            max_work_minutes=60,
        )
        assert second["documents"][0]["gross_estimates"][0]["amount"] == "130.00"
        second_ref = second["documents"][0]["ref"]
        finalized = await change(
            call,
            "docket_update_invoice",
            [second_ref],
            "invoice-second-issue",
            invoice_ref=second_ref,
            status="issued",
        )
        assert finalized["documents"][0]["number"] == "INV-000002"
        data = json.loads(await export_document(call, second_ref, "json"))
        assert len(data["lines"]) == 2
        assert (
            next(row for row in data["lines"] if row["record_type"] == "labor")[
                "gross_line_estimate"
            ]
            == "120.00"
        )
        assert (
            next(row for row in data["lines"] if row["record_type"] == "expense")["expense_ref"]
            == fee
        )
        rows = list(csv.DictReader(io.StringIO(await export_document(call, second_ref, "csv"))))
        assert {row["record_type"] for row in rows} == {"summary", "labor", "expense"}
        text = await export_document(call, second_ref, "markdown")
        assert "INV-000002" in text and "130.00 USD" in text
        packet = await call("docket_get_work_context", {"refs": [work]})
        state = packet["objects"][0]["state"]
        assert state["claim_quantities"]["components"][0]["invoiced_us"] == 120 * 60_000_000
        assert state["claim_quantities"]["components"][0]["uninvoiced_us"] == 0
        await change(
            call,
            "docket_record_expense",
            [eng],
            "invoice-old-expense",
            amount="1",
            currency="USD",
            description="Synthetic earlier uninvoiced cost",
            engagement_ref=eng,
            incurred_on="2026-09-01",
            reimbursable=True,
        )
        remaining = await change(
            call,
            "docket_prepare_invoice",
            [eng],
            "invoice-default-history",
            engagement_ref=eng,
        )
        assert remaining["documents"][0]["date_from"] == "2026-09-01"
        assert remaining["documents"][0]["gross_estimates"][0]["amount"] == "1.00"
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkClaim)) == 1
            assert session.scalar(select(func.count()).select_from(InvoiceNumber)) == 2
            assert session.scalar(select(func.count()).select_from(DocumentAllocation)) == 4
            assert session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_invoice_preview_basis_cancellation_and_missing_terms_do_not_block_work(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        eng, work = await fixture_work(call, "invoice-unknown", 180, False)
        previews = [
            await change(
                call,
                "docket_prepare_invoice",
                [eng],
                f"invoice-preview-{i}",
                engagement_ref=eng,
                date_from="2026-11-02",
                date_to="2026-11-09",
                timezone="UTC",
                preview=True,
            )
            for i in (1, 2)
        ]
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(FinancialDocument)) == 0
            assert session.scalar(select(func.count()).select_from(WorkClaim)) == 0
        committed = await call("docket_apply_change", {"change_id": previews[0]["change_id"]})
        assert committed.get("disposition") == "committed", committed
        ref = committed["documents"][0]["ref"]
        stale = await call("docket_apply_change", {"change_id": previews[1]["change_id"]})
        assert stale["error"]["code"] == "invoice_claim_basis_changed", stale
        context = await call("docket_get_work_context", {"refs": [ref]})
        blocked = await call(
            "docket_update_invoice",
            {
                "context_id": context["context_id"],
                "operation_id": "invoice-unknown-issue",
                "invoice_ref": ref,
                "status": "issued",
            },
        )
        assert blocked["error"]["code"] == "invoice_needs_reconciliation"
        await change(
            call,
            "docket_update_invoice",
            [ref],
            "invoice-cancel",
            invoice_ref=ref,
            status="cancelled",
        )
        await change(
            call,
            "docket_update_work_session",
            [work],
            "invoice-unblocked-work",
            work_ref=work,
            description="Synthetic work correction",
        )
        await change(
            call,
            "docket_set_pay_terms",
            [eng],
            "invoice-new-rate",
            engagement_ref=eng,
            effective_from="2020-01-01",
            rate="80",
            currency="USD",
            billable=True,
        )
        expense = await change(
            call,
            "docket_record_expense",
            [eng],
            "invoice-receipt-gap",
            amount="10",
            currency="USD",
            description="Synthetic receipt gap",
            engagement_ref=eng,
            reimbursable=True,
            receipt_required=True,
            receipt_ref=new_public_ref("src"),
        )
        fee = expense["expenses"][0]["ref"]
        draft = await change(
            call,
            "docket_prepare_invoice",
            [eng, fee],
            "invoice-with-gap",
            engagement_ref=eng,
            expense_refs=[fee],
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
        )
        context = await call("docket_get_work_context", {"refs": [draft["documents"][0]["ref"]]})
        blocked = await call(
            "docket_update_invoice",
            {
                "context_id": context["context_id"],
                "operation_id": "invoice-gap-issue",
                "invoice_ref": draft["documents"][0]["ref"],
                "status": "issued",
            },
        )
        assert "expense_required_receipt_unavailable" in blocked["error"]["details"]["flags"]
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(InvoiceNumber)) == 0
            assert session.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_invoice_refresh_keeps_history_and_delivery_is_separate(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        eng, work = await fixture_work(call, "invoice-refresh", 90)
        draft = await change(
            call,
            "docket_prepare_invoice",
            [eng],
            "invoice-refresh-draft",
            engagement_ref=eng,
            expense_refs=[],
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            payment_terms="Net 15",
            due_on="2026-11-30",
            notes="Synthetic original terms",
        )
        ref = draft["documents"][0]["ref"]
        await change(
            call,
            "docket_update_work_session",
            [work],
            "invoice-correct-duration",
            work_ref=work,
            duration_minutes=120,
        )
        refreshed = await change(
            call,
            "docket_refresh_invoice",
            [ref],
            "invoice-refreshed",
            invoice_ref=ref,
            notes="Synthetic corrected note",
        )
        assert refreshed["documents"][0]["gross_estimates"][0]["amount"] == "160.00"
        issued = await change(
            call,
            "docket_update_invoice",
            [ref],
            "invoice-refreshed-issue",
            invoice_ref=ref,
            status="issued",
        )
        assert issued["documents"][0]["number"] == "INV-000001"
        before = await export_document(call, ref, "json")
        data = json.loads(before)
        assert (
            data["header"]["payment_terms"] == "Net 15"
            and data["header"]["notes"] == "Synthetic corrected note"
        )
        delivered = await change(
            call,
            "docket_update_invoice",
            [ref],
            "invoice-delivered",
            invoice_ref=ref,
            status="delivered",
            note="Operator reports manual delivery",
        )
        assert delivered["documents"][0]["status"] == "issued"
        assert len(delivered["documents"][0]["delivery_observations"]) == 1
        await change(
            call,
            "docket_update_work_session",
            [work],
            "invoice-after-issue-correction",
            work_ref=work,
            duration_minutes=130,
        )
        assert await export_document(call, ref, "json") == before
        state = await call("docket_read_document", {"ref": ref})
        assert "work_revision_changed" in state["document"]["reconciliation_flags"]
        context = await call("docket_get_work_context", {"refs": [ref]})
        denied = await call(
            "docket_refresh_invoice",
            {
                "context_id": context["context_id"],
                "operation_id": "invoice-forbidden-refresh",
                "invoice_ref": ref,
            },
        )
        assert denied["error"]["code"] == "invoice_snapshot_final"
        with session_factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(FinancialDocumentObservation)) == 1
            )
            assert session.scalar(select(func.count()).select_from(InvoiceNumber)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_invoice_contracts_and_read_only_denial(linked_plugin):
    _, settings, _, _ = linked_plugin
    tools = {tool.name: tool for tool in await PluginMCP(settings).list_tools()}
    for name in INVOICE_TOOL_PURPOSES:
        definition = tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)
        assert len(json.dumps(definition["inputSchema"], separators=(",", ":")).encode()) <= 4096
        assert len(json.dumps(definition, separators=(",", ":")).encode()) <= 6144
        assert not definition["inputSchema"].get("oneOf")
    async with work_client(linked_plugin) as call:
        context = await call("docket_get_work_context", {})
        denied = await call(
            "docket_prepare_invoice",
            {
                "context_id": context["context_id"],
                "operation_id": "invoice-readonly",
                "engagement_ref": new_public_ref("eng"),
            },
        )
        assert denied["error"]["code"] == "agent_authority_denied"
