"""Flat native money commands reconcile shared claims without provider writes."""

import csv
import io
import json

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_invoices import fixture_work
from test_work_lineage import change
from test_work_reports import work_client

from docket.domain.public_refs import new_public_ref
from docket.models import (
    FinancialDocumentRevision,
    FinancialEntry,
    Operation,
    Payment,
    PaymentEvent,
)
from docket.plugin.server import PluginMCP
from docket.schemas.settlement import SETTLEMENT_TOOL_PURPOSES

linked_plugin = test_native_plugin.linked_plugin


async def financial_change(call, name, refs, identity, **fields):
    context = await call("docket_get_financial_context", {"refs": refs})
    assert context.get("ok"), context
    result = await call(
        name, {"context_id": context["context_id"], "operation_id": identity, **fields}
    )
    assert result.get("disposition") in {"committed", "prepared"}, result
    return result


async def export_finance(call, refs, format="json"):
    page = await call("docket_get_financial_context", {"refs": refs, "format": format})
    chunks = []
    for _ in range(100):
        assert page.get("ok"), page
        chunks.append(page["content"])
        if page["complete"]:
            return "".join(chunks), page
        page = await call("docket_get_financial_context", {"cursor": page["next_cursor"]})
    raise AssertionError("Financial export did not finish")


async def posted_invoice(call, suffix, minutes=120):
    eng, work = await fixture_work(call, suffix, minutes)
    sheet = await change(
        call,
        "docket_prepare_timesheet",
        [eng],
        suffix + "-sheet",
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
        suffix + "-submit",
        timesheet_ref=sheet_ref,
        status="submitted",
    )
    draft = await change(
        call,
        "docket_prepare_invoice",
        [eng],
        suffix + "-draft",
        engagement_ref=eng,
        date_from="2026-11-02",
        date_to="2026-11-09",
        timezone="UTC",
        expense_refs=[],
        due_on="2026-11-04",
    )
    ref = draft["documents"][0]["ref"]
    await change(
        call, "docket_update_invoice", [ref], suffix + "-issue", invoice_ref=ref, status="issued"
    )
    return eng, work, sheet_ref, ref


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_shared_claim_partial_payment_credit_reversal_and_export(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        eng, _work, sheet, invoice = await posted_invoice(call, "settlement-shared")
        with session_factory() as session:
            original = list(
                session.scalars(
                    select(FinancialDocumentRevision).where(
                        FinancialDocumentRevision.document_ref == invoice
                    )
                )
            )
            issued_json = json.dumps(original[-1].snapshot_json, sort_keys=True)
        receipt = await financial_change(
            call,
            "docket_record_payment",
            [invoice],
            "payment-first",
            amount="100",
            currency="USD",
            target_ref=invoice,
            received_on="2026-11-04",
            external_source="synthetic",
            external_id="receipt-1",
            engagement_ref=eng,
        )
        payment = receipt["financial_records"][0]["payments"][0]["ref"]
        second = await financial_change(
            call,
            "docket_record_payment",
            [sheet],
            "payment-second",
            amount="100",
            currency="USD",
            target_ref=sheet,
        )
        assert second["financial_records"][0]["payments"][0]["unapplied"] == "40"
        report, _ = await export_finance(call, [sheet, invoice])
        rows = json.loads(report)["rows"]
        total = next(row for row in rows if row["record_type"] == "currency_total")
        assert total["gross_claim"] == "160" and total["cash_applied"] == "160"
        assert total["received"] == "200" and total["unapplied"] == "40"
        duplicate = await financial_change(
            call,
            "docket_record_payment",
            [invoice],
            "payment-import-retry",
            amount="100",
            currency="USD",
            target_ref=invoice,
            received_on="2026-11-04",
            external_source="synthetic",
            external_id="receipt-1",
            engagement_ref=eng,
        )
        assert duplicate["financial_records"][0]["ref"] == receipt["financial_records"][0]["ref"]
        assert duplicate["financial_records"][0]["deduplicated"]
        await financial_change(
            call,
            "docket_adjust_financial_claim",
            [invoice],
            "credit-forty",
            target_ref=invoice,
            direction="credit",
            amount="40",
            reason="Reported adjustment",
        )
        after, _ = await export_finance(call, [sheet, invoice])
        total = next(r for r in json.loads(after)["rows"] if r["record_type"] == "currency_total")
        assert total["credits"] == "40" and total["cash_applied"] == "120"
        assert total["received"] == "200" and total["unapplied"] == "80"
        await financial_change(
            call,
            "docket_reverse_financial_record",
            [payment],
            "receipt-reversal",
            ref=payment,
            amount="20",
            reason="Reported receipt correction",
        )
        current, _ = await export_finance(call, [sheet, invoice])
        assert any(r.get("kind") == "reversal" for r in json.loads(current)["rows"])
        csv_text, _ = await export_finance(call, [sheet, invoice], "csv")
        csv_total = next(
            r for r in csv.DictReader(io.StringIO(csv_text)) if r["record_type"] == "currency_total"
        )
        assert csv_total["received"] == "180"
        assert (
            next(r for r in json.loads(report)["rows"] if r["record_type"] == "currency_total")[
                "received"
            ]
            == "200"
        )
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Payment)) == 2
            assert session.scalar(select(func.count()).select_from(PaymentEvent)) >= 4
            assert session.scalar(select(func.count()).select_from(Operation)) == 0
            issued = list(
                session.scalars(
                    select(FinancialDocumentRevision).where(
                        FinancialDocumentRevision.document_ref == invoice
                    )
                )
            )[-1]
            assert json.dumps(issued.snapshot_json, sort_keys=True) == issued_json


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_net_receipt_keeps_gross_gap_and_explicit_deduction_is_separate(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        _eng, _work, sheet, _invoice = await posted_invoice(call, "settlement-net", 75)
        receipt = await financial_change(
            call,
            "docket_record_payment",
            [sheet],
            "net-receipt",
            amount="80",
            currency="USD",
            target_ref=sheet,
        )
        pay = receipt["financial_records"][0]["payments"][0]["ref"]
        report, _ = await export_finance(call, [sheet])
        total = next(r for r in json.loads(report)["rows"] if r["record_type"] == "currency_total")
        assert total["outstanding"] == "20" and total["deductions"] == "0"
        stale = await call("docket_get_financial_context", {"refs": [sheet, pay]})
        await financial_change(
            call,
            "docket_apply_payment",
            [sheet, pay],
            "known-withholding",
            payment_ref=pay,
            target_ref=sheet,
            deduction_amount="20",
            deduction_kind="withholding",
        )
        report, _ = await export_finance(call, [sheet])
        total = next(r for r in json.loads(report)["rows"] if r["record_type"] == "currency_total")
        assert (
            total["received"] == "80"
            and total["deductions"] == "20"
            and total["outstanding"] == "0"
        )
        outdated = await call(
            "docket_apply_payment",
            {
                "context_id": stale["context_id"],
                "operation_id": "net-stale-apply",
                "payment_ref": pay,
                "target_ref": sheet,
                "amount": "1",
            },
        )
        assert outdated["error"]["code"] == "financial_version_conflict", outdated
        with session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(FinancialEntry)
                    .where(FinancialEntry.kind == "deduction")
                )
                == 1
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_settlement_contracts_and_read_only_rejection(linked_plugin):
    _, settings, _, _ = linked_plugin
    tools = {tool.name: tool for tool in await PluginMCP(settings).list_tools()}
    for name in SETTLEMENT_TOOL_PURPOSES:
        definition = tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)
        assert len(json.dumps(definition["inputSchema"], separators=(",", ":")).encode()) <= 4096
        assert len(json.dumps(definition, separators=(",", ":")).encode()) <= 6144
        assert not definition["inputSchema"].get("oneOf")
    async with work_client(linked_plugin) as call:
        context = await call("docket_get_financial_context", {})
        denied = await call(
            "docket_record_payment",
            {
                "context_id": context["context_id"],
                "operation_id": "readonly-money",
                "amount": "1",
                "currency": "USD",
            },
        )
        assert denied["error"]["code"] == "agent_authority_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_reimbursement_nanounits_claim_correction_replacement_and_missing_archive(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        expense = await change(
            call,
            "docket_record_expense",
            [],
            "settlement-expense",
            amount="0.000000003",
            currency="USD",
            description="Synthetic expense",
            reimbursable=True,
            submission_status="approved",
            claim_amount="0.000000003",
        )
        fee = expense["expenses"][0]["ref"]
        received = await financial_change(
            call,
            "docket_record_payment",
            [fee],
            "reimbursement-first",
            amount="0.000000001",
            currency="USD",
            target_ref=fee,
            category="reimbursement",
            source_method="import",
            source_ref=new_public_ref("src"),
            external_source="synthetic-reimbursements",
            external_id="reimbursement-1",
        )
        pay = received["financial_records"][0]["payments"][0]["ref"]
        snapshot = await call("docket_get_financial_context", {"refs": [fee], "limit": 1})
        await change(
            call,
            "docket_update_expense",
            [fee],
            "reimbursement-cost-correction",
            expense_ref=fee,
            amount="0.000000005",
        )
        before, _ = await export_finance(call, [fee])
        rows = json.loads(before)["rows"]
        claim = next(row for row in rows if row["record_type"] == "claim")
        assert claim["gross_claim"] == "0.000000003"
        assert "expense_changed_since_claim_observation" in claim["flags"]
        await financial_change(
            call,
            "docket_reverse_financial_record",
            [pay],
            "reimbursement-reverse",
            ref=pay,
            reason="Correct reported receipt amount",
        )
        replacement = await financial_change(
            call,
            "docket_record_payment",
            [pay, fee],
            "reimbursement-replacement",
            amount="0.000000002",
            currency="USD",
            target_ref=fee,
            replaces_ref=pay,
            category="reimbursement",
            external_source="synthetic-reimbursements",
            external_id="reimbursement-1",
        )
        new_pay = replacement["financial_records"][0]["payments"][0]["ref"]
        assert new_pay != pay
        replay_import = await financial_change(
            call,
            "docket_record_payment",
            [fee],
            "reimbursement-reimport",
            amount="0.000000002",
            currency="USD",
            target_ref=fee,
            external_source="synthetic-reimbursements",
            external_id="reimbursement-1",
        )
        assert replay_import["financial_records"][0]["payments"][0]["ref"] == new_pay
        report, _ = await export_finance(call, [fee])
        total = next(r for r in json.loads(report)["rows"] if r["record_type"] == "currency_total")
        assert total["expense_claim_gross"] == "0.000000003" and total["work_claim_gross"] == "0"
        assert total["reported_reimbursement_received"] == "0.000000002"
        assert total["outstanding"] == "0.000000001"
        await financial_change(
            call,
            "docket_adjust_financial_claim",
            [fee],
            "reimbursement-credit",
            target_ref=fee,
            direction="credit",
            amount="0.000000001",
            reason="Reported expense credit",
        )
        context = await call("docket_get_financial_context", {"refs": [fee]})
        too_much = await call(
            "docket_adjust_financial_claim",
            {
                "context_id": context["context_id"],
                "operation_id": "reimbursement-credit-excess",
                "target_ref": fee,
                "direction": "credit",
                "amount": "0.000000003",
                "reason": "Invalid excess",
            },
        )
        assert too_much["error"]["code"] == "credit_exceeds_claim", too_much
        chunks = []
        page = await call("docket_get_financial_context", {"cursor": snapshot["export_cursor"]})
        while True:
            chunks.append(page["content"])
            if page["complete"]:
                break
            page = await call("docket_get_financial_context", {"cursor": page["next_cursor"]})
        old_total = next(
            r for r in json.loads("".join(chunks))["rows"] if r["record_type"] == "currency_total"
        )
        assert old_total["received"] == "0.000000001"
        with session_factory() as session:
            first = session.scalar(select(Payment).where(Payment.ref_id == pay))
            assert first.evidence_gap and first.source_ref is None
            assert session.scalar(select(func.count()).select_from(Payment)) == 2
