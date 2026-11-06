"""Settlement races and retained-history checks on the release's disposable PG."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError


async def check_settlement(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.domain.public_refs import new_public_ref
    from docket.models import (
        FinancialEntry,
        FinancialReportSnapshot,
        FinancialTransaction,
        Payment,
        PaymentEvent,
    )
    from docket.plugin.server import PluginMCP

    async def call(name, arguments, actor=principal):
        token = agent_call_context.set(AgentCallContext(actor))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    async def change(name, refs, identity, **fields):
        context_tool = (
            "docket_get_financial_context"
            if name
            in {
                "docket_record_payment",
                "docket_apply_payment",
                "docket_adjust_financial_claim",
                "docket_reverse_financial_record",
            }
            else "docket_get_work_context"
        )
        packet = await call(context_tool, {"refs": refs})
        result = await call(
            name, {"context_id": packet["context_id"], "operation_id": identity, **fields}
        )
        assert result.get("disposition") in {"committed", "prepared"}, result
        return result

    async def export(refs):
        page = await call("docket_get_financial_context", {"refs": refs, "format": "json"})
        parts = []
        for _ in range(100):
            assert page.get("ok"), page
            parts.append(page["content"])
            if page["complete"]:
                return json.loads("".join(parts))
            page = await call("docket_get_financial_context", {"cursor": page["next_cursor"]})
        raise AssertionError("Settlement export did not terminate")

    eng_result = await change(
        "docket_create_engagement",
        [],
        "pg-settle-eng",
        label="Synthetic settlement",
        kind="contract",
    )
    eng = next(ref for ref in eng_result["affected_refs"] if ref.startswith("eng_"))
    await change(
        "docket_set_pay_terms",
        [eng],
        "pg-settle-rate",
        engagement_ref=eng,
        effective_from="2020-01-01",
        rate="100",
        currency="USD",
        billable=True,
    )
    await change(
        "docket_log_work_session",
        [eng],
        "pg-settle-work",
        engagement_ref=eng,
        duration_minutes=60,
        work_date="2026-11-03",
        description="Synthetic performed work",
    )
    draft = await change(
        "docket_prepare_invoice",
        [eng],
        "pg-settle-invoice",
        engagement_ref=eng,
        date_from="2026-11-02",
        date_to="2026-11-09",
        timezone="UTC",
        expense_refs=[],
    )
    invoice = draft["documents"][0]["ref"]
    await change(
        "docket_update_invoice", [invoice], "pg-settle-issue", invoice_ref=invoice, status="issued"
    )
    packet = await call("docket_get_financial_context", {})
    barrier = Barrier(2)

    def import_receipt(index):
        barrier.wait(timeout=15)
        return asyncio.run(
            call(
                "docket_record_payment",
                {
                    "context_id": packet["context_id"],
                    "operation_id": f"pg-settle-import-{index}",
                    "amount": "100",
                    "currency": "USD",
                    "external_source": "synthetic-pg",
                    "external_id": "receipt-1",
                    "engagement_ref": eng,
                },
            )
        )

    # An overview with no selected engagement cannot authorize a link by label.
    # Use a context which actually observed this owned engagement.
    packet = await call("docket_get_work_context", {"refs": [eng]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(import_receipt, (1, 2)))
    assert all(row.get("disposition") == "committed" for row in receipts), receipts
    payment = receipts[0]["financial_records"][0]["payments"][0]["ref"]
    assert receipts[1]["financial_records"][0]["payments"][0]["ref"] == payment
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Payment).where(Payment.engagement_ref == eng)
            )
            == 1
        )
    previews = [
        await change(
            "docket_apply_payment",
            [payment, invoice],
            f"pg-settle-preview-{i}",
            payment_ref=payment,
            target_ref=invoice,
            amount="60",
            preview=True,
        )
        for i in (1, 2)
    ]
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=15)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        applied = list(pool.map(apply, previews))
    assert sum(row.get("disposition") == "committed" for row in applied) == 1, applied
    assert any(row.get("ok") is False for row in applied), applied
    await change(
        "docket_apply_payment",
        [payment, invoice],
        "pg-settle-rest",
        payment_ref=payment,
        target_ref=invoice,
        amount="40",
    )
    credit = await change(
        "docket_adjust_financial_claim",
        [invoice],
        "pg-settle-credit",
        target_ref=invoice,
        direction="credit",
        amount="20",
        reason="Reported credit",
    )
    report = await export([invoice, payment])
    total = next(row for row in report["rows"] if row["record_type"] == "currency_total")
    assert total["cash_applied"] == "80" and total["unapplied"] == "20", total
    await change(
        "docket_reverse_financial_record",
        [payment],
        "pg-settle-receipt-reversal",
        ref=payment,
        amount="30",
        reason="Reported receipt correction",
    )
    report = await export([invoice, payment])
    total = next(row for row in report["rows"] if row["record_type"] == "currency_total")
    assert (
        total["received"] == "70" and total["cash_applied"] == "70" and total["outstanding"] == "10"
    ), total
    with factory() as session:
        head = session.scalar(select(Payment).where(Payment.ref_id == payment))
        version = head.version
        original = session.scalar(
            select(FinancialEntry).where(
                FinancialEntry.payment_ref == payment, FinancialEntry.kind == "receipt"
            )
        )
        original_fields = {
            column.name: getattr(original, column.name)
            for column in FinancialEntry.__table__.columns
        }
        txn = session.scalar(
            select(FinancialTransaction).where(
                FinancialTransaction.ref_id == credit["financial_records"][0]["ref"]
            )
        )
        txn_fields = {
            column.name: getattr(txn, column.name)
            for column in FinancialTransaction.__table__.columns
        }
        transaction_ref = txn.ref_id

    def rejected(statement, message):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig), exc
        else:
            raise AssertionError("PostgreSQL accepted invalid retained settlement")

    for statement, message in (
        (
            update(Payment)
            .where(Payment.ref_id == payment)
            .values(amount="999", version=version + 1),
            "immutable",
        ),
        (update(Payment).where(Payment.ref_id == payment).values(version=version + 2), "immutable"),
        (
            update(Payment).where(Payment.ref_id == payment).values(version=version + 1),
            "exact retained event",
        ),
        (delete(Payment).where(Payment.ref_id == payment), "immutable"),
        (delete(FinancialEntry).where(FinancialEntry.payment_ref == payment), "immutable"),
        (
            update(FinancialTransaction)
            .where(FinancialTransaction.ref_id == transaction_ref)
            .values(kind="charge"),
            "immutable",
        ),
        (delete(PaymentEvent).where(PaymentEvent.payment_ref == payment), "immutable"),
        (
            delete(FinancialReportSnapshot).where(
                FinancialReportSnapshot.ref_id == report["report_ref"]
            ),
            "immutable",
        ),
    ):
        rejected(statement, message)
    # An immutable transaction header also seals entry membership, rather than
    # permitting later append-only rows to alter an old financial transaction.
    import uuid

    appended = {**original_fields, "id": uuid.uuid4(), "ref_id": new_public_ref("fent")}
    rejected(FinancialEntry.__table__.insert().values(**appended), "membership is immutable")
    for amount, currency, message in (
        ("1000", "USD", "Reversal exceeds"),
        ("1", "EUR", "same-currency"),
    ):
        entry_ref, txn_ref = new_public_ref("fent"), new_public_ref("ftxn")
        try:
            with factory.begin() as session:
                session.execute(
                    FinancialTransaction.__table__.insert().values(
                        **{
                            **txn_fields,
                            "id": uuid.uuid4(),
                            "ref_id": txn_ref,
                            "kind": "reversal",
                            "payment_ref": None,
                            "payment_version": None,
                            "outcome_json": {"entries": [entry_ref]},
                        }
                    )
                )
                session.execute(
                    FinancialEntry.__table__.insert().values(
                        **{
                            **original_fields,
                            "id": uuid.uuid4(),
                            "ref_id": entry_ref,
                            "transaction_ref": txn_ref,
                            "kind": "reversal",
                            "reverses_ref": original_fields["ref_id"],
                            "amount": amount,
                            "currency": currency,
                        }
                    )
                )
        except DBAPIError as exc:
            assert message in str(exc.orig), exc
        else:
            raise AssertionError("PostgreSQL accepted an oversized or cross-currency reversal")
    packet = await call("docket_get_financial_context", {"refs": [payment], "limit": 1})
    other = AgentPrincipal(
        "synthetic-other-finance", principal.operator_ref, "interactive", frozenset({"read"})
    )
    private = await call("docket_get_financial_context", {"cursor": packet["export_cursor"]}, other)
    assert private["error"]["code"] == "financial_report_unavailable", private
    other_owner = AgentPrincipal(
        "synthetic-other-finance-owner", "synthetic-other-owner", "interactive", frozenset({"read"})
    )
    private_owner = await call("docket_get_financial_context", {"refs": [payment]}, other_owner)
    assert private_owner["error"]["code"] == "agent_authority_denied", private_owner
    return {
        "native_settlement_races_and_conservation": "passed",
        "settlement_history_guards": "passed",
    }
