"""Synthetic agent upload → canonical expense → statement matching → annual workpapers."""

from decimal import Decimal

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_settlement import financial_change
from test_work_lineage import change
from test_work_reports import work_client

from docket.models import (
    AccountActivity,
    AttentionCase,
    Engagement,
    FinancialReviewFact,
    FundingIdentity,
)

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_financial_year_end_to_end(session_factory, linked_plugin, monkeypatch):
    async with work_client(linked_plugin) as call:

        async def context(**kw):
            result = await call("docket_get_financial_year", {"tax_year": 2026, **kw})
            assert result.get("ok"), result
            return result

        async def command(name, key, refs=(), *, error=False, **fields):
            ctx = await context(refs=list(refs))
            args = dict(
                context_id=ctx["context_id"],
                operation_id=(name + "-" + key).replace(" ", "-"),
                **fields,
            )
            result = await call(name, args)
            if error:
                assert result.get("disposition") != "committed", result
                return
            assert result.get("disposition") == "committed", result
            with session_factory() as db:
                fact = db.scalar(
                    select(FinancialReviewFact.ref_id).where(
                        FinancialReviewFact.changeset_ref == result["changeset_ref"]
                    )
                )
            return fact

        async def upload(key, text, **declarations):
            from docket.plugin import financial_intake

            monkeypatch.setattr(financial_intake, "download_file", lambda *_: text.encode())
            return await call(
                "docket_submit_financial_evidence",
                dict(
                    context_id=(await context())["context_id"],
                    operation_id=key,
                    file={
                        "file_id": key,
                        "mime_type": "message/rfc822",
                        "download_url": "https://files.oaiusercontent.com/test",
                    },
                    **declarations,
                ),
            )

        async def posting(label, value, kind, acct, day="2026-01-03"):
            await command(
                "docket_create_account_activity",
                label,
                [acct],
                account_ref=acct,
                amount=value,
                kind=kind,
                currency="USD",
                posted_on=day,
                description=label,
            )
            with session_factory() as db:
                return db.scalar(
                    select(AccountActivity.ref_id).where(
                        AccountActivity.fields["description"].as_string() == label
                    )
                )

        for name in ("Card", "Checking"):
            await command("docket_create_funding_account", "acct-" + name, label=name)
        with session_factory() as db:
            card, checking = list(
                db.scalars(select(FundingIdentity.ref_id).order_by(FundingIdentity.created_at))
            )
        mail = (
            "From: fixture@example.test\r\nSubject: Receipt\r\n\r\n"
            "Vendor: Example Fixture\r\nReceipt ID: DOC-101\r\n"
            "Date: 2026-01-03\r\nTotal: USD 120.00\r\n"
        )
        uploaded = await upload("receipt", mail, business_use_percent="60")
        cost = uploaded["outcomes"][0]["expense_ref"]
        assert cost, uploaded
        first = await context(section="expenses")
        expense = first["rows"][0]
        assert Decimal(expense["declared_amount"]) == 72 and expense["tax_status"] == "unreviewed"
        assert Decimal(expense["intended_claim_amount"]) == 72
        assert expense["declarations"][0]["write_off_intent_origin"] == "positive_business_use_v1"
        assert Decimal(expense["funded"]) == 0
        old = await context()
        review = await call(
            "docket_create_financial_year_review",
            dict(context_id=old["context_id"], operation_id="review", report_ref=old["report_ref"]),
        )
        assert review.get("disposition") == "committed", review
        for label, val, acct in (("first tender", "-90", card), ("second tender", "-30", checking)):
            ref = await posting(label, val, "purchase", acct)
            await command(
                "docket_create_activity_match",
                "match-" + label,
                [ref, cost],
                activity_ref=ref,
                role="purchase",
                amount=val[1:],
                expense_ref=cost,
            )
            await command(
                "docket_create_activity_match",
                "extra-" + label,
                [ref, cost],
                activity_ref=ref,
                role="purchase",
                amount="0.01",
                expense_ref=cost,
                error=True,
            )
        out = await posting("repayment", "-120", "transfer", checking)
        into = await posting("incoming", "120", "transfer", card)
        await command(
            "docket_create_activity_match",
            "pair",
            [out, into, cost],
            activity_ref=out,
            counterpart_ref=into,
            role="transfer",
            amount="120",
            expense_ref=cost,
        )
        fields = dict(
            expense_ref=cost,
            tax_year=2026,
            jurisdiction="US",
            activity="contract",
            allocated_amount="120",
            reviewed_deduction="60",
            status="included",
            rationale="Synthetic reviewed use",
        )
        treatment = await command(
            "docket_create_expense_treatment", "review-cost", [cost], **fields
        )
        returned = await posting("merchant refund", "15", "refund", card)
        await command(
            "docket_create_activity_match",
            "refund",
            [cost, returned],
            activity_ref=returned,
            expense_ref=cost,
            role="refund",
            amount="15",
        )
        assert "treatment_review" in {r["kind"] for r in (await context())["rows"]}
        await change(
            call,
            "docket_update_expense",
            [cost],
            "cost-approved",
            expense_ref=cost,
            reimbursable=True,
            submission_status="approved",
            claim_amount="50",
        )
        payment = await financial_change(
            call,
            "docket_record_payment",
            [cost],
            "paid",
            amount="20",
            currency="USD",
            category="reimbursement",
            target_ref=cost,
            received_on="2026-02-10",
        )
        pay = payment["financial_records"][0]["payments"][0]["ref"]
        deposit = await posting("expense reimbursement", "20", "deposit", checking)
        await command(
            "docket_create_activity_match",
            "reimbursement",
            [cost, deposit, pay],
            expense_ref=cost,
            activity_ref=deposit,
            payment_ref=pay,
            role="reimbursement",
            amount="20",
        )
        assert Decimal((await context(section="expenses"))["rows"][0]["reimbursed"]) == 20
        await command(
            "docket_create_expense_treatment",
            "correct-review",
            [cost, treatment],
            prior_ref=treatment,
            **fields,
        )
        assert Decimal((await context(section="expenses"))["rows"][0]["funded"]) == 120
        assert (await context(section="expenses", report_ref=first["report_ref"]))["rows"] == first[
            "rows"
        ]
        for label, val, kind, day in (
            ("estimated", "-600", "estimated", "2027-01-03"),
            ("refund", "100", "refund", "2026-06-04"),
        ):
            observation = await posting(label, val, "unknown", checking, day)
            await command(
                "docket_create_tax_allocation",
                "tax-" + label,
                [observation],
                activity_ref=observation,
                tax_year=2026,
                jurisdiction="US",
                tax_type="income",
                kind=kind,
                amount=val.lstrip("-"),
            )

        engagement = await change(
            call, "docket_create_engagement", [], "payer", label="Synthetic employer"
        )
        with session_factory() as db:
            eng = db.scalar(
                select(Engagement.ref_id).where(
                    Engagement.created_by_changeset_ref == engagement["changeset_ref"]
                )
            )
        form = await upload(
            "income-doc",
            (
                "From: test@example.test\r\nSubject: Payroll\r\n\r\n"
                "Taxable wages: USD 1900\r\nFederal income tax withheld: USD 400"
            ),
            evidence_role="income_document",
        )
        assert form["outcomes"][0]["facts"]["taxable_wages"] == "1900"
        assert "gross" not in form["outcomes"][0]["facts"]
        source = form["source_ref"]
        basis = dict(
            engagement_ref=eng,
            tax_year=2026,
            jurisdiction="US",
            currency="USD",
            source_ref=source,
            period_start="2026-01-01",
            period_end="2026-12-31",
            gross="2000",
            withheld="400",
            net="1600",
        )
        stub = await command(
            "docket_create_income_evidence",
            "stub",
            [eng, source],
            document_kind="paystub",
            coverage="period",
            **basis,
        )
        annual = await command(
            "docket_create_income_evidence",
            "form",
            [eng, source],
            document_kind="W-2",
            coverage="annual",
            **basis,
        )
        totals = await context(section="totals")
        tax = next(r for r in totals["rows"] if r["tax_type"] == "income")
        assert Decimal(tax["gross_tax_paid"]) == 1000 and Decimal(tax["net_tax_paid"]) == 900
        await command(
            "docket_create_income_evidence",
            "correct-form",
            [eng, source, annual],
            prior_ref=annual,
            document_kind="W-2",
            coverage="annual",
            **{**basis, "gross": "2100"},
        )
        decisions = await context()
        assert "annual_difference" in {r["kind"] for r in decisions["rows"]}
        for key in ("refresh", "refresh-again"):
            args = dict(
                context_id=decisions["context_id"],
                operation_id=key,
                report_ref=decisions["report_ref"],
            )
            result = await call("docket_create_financial_year_review", args)
            assert result.get("disposition") == "committed", result
            with session_factory() as db:
                count = db.scalar(select(func.count()).select_from(AttentionCase))
            if key == "refresh":
                expected = count
            else:
                assert count == expected
        assert stub and "withholding" in (await context(section="totals", format="csv"))["csv"]

        opt_out_mail = mail.replace("DOC-101", "DOC-102")
        opted_out = await upload(
            "opt-out", opt_out_mail, business_use_percent=60, write_off_intent=False
        )
        ref = opted_out["outcomes"][0]["expense_ref"]
        captured = await context(section="expenses")
        row = next(r for r in captured["rows"] if r["expense_ref"] == ref)
        assert Decimal(row["declared_amount"]) == 72 and row["intended_claim_amount"] == "0"
        duplicate = await upload("also-reported", opt_out_mail, business_use_percent=60)
        assert duplicate["outcomes"][0]["expense_ref"] == ref
        assert not duplicate["outcomes"][0]["conflicting_declarations"]
        overridden = await context(section="expenses")
        assert next(r for r in overridden["rows"] if r["expense_ref"] == ref)[
            "intended_claim_amount"
        ] == "0"
        conflict = await upload(
            "explicit-claim", opt_out_mail, business_use_percent=60, write_off_intent=True
        )
        assert conflict["outcomes"][0]["conflicting_declarations"]
        live = await context(section="expenses")
        row = next(r for r in live["rows"] if r["expense_ref"] == ref)
        assert row.get("intended_claim_amount") is None
        assert (await context(section="expenses", report_ref=captured["report_ref"]))["rows"] == (
            captured["rows"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_statement_renewal_asset_review_and_protected_export(
    session_factory, linked_plugin, monkeypatch
):
    import io
    import tarfile

    from docket.financial_export_cli import write_members
    from docket.plugin import financial_intake

    async with work_client(linked_plugin) as call:

        async def packet(**kw):
            return await call("docket_get_financial_year", dict(tax_year=2026, **kw))

        async def write(name, key, refs=(), **kw):
            ctx = await packet(refs=list(refs))
            result = await call(name, dict(context_id=ctx["context_id"], operation_id=key, **kw))
            assert result.get("disposition") == "committed", result
            return result

        await write("docket_create_funding_account", "bank", label="Synthetic checking")
        acct = (await call("docket_get_funding_context", {}))["objects"][0]["ref"]
        statement = (
            b"posted_on,description,amount,currency,kind,external_id\n"
            b"2026-02-08,Hosting,-14,USD,purchase,host1\n"
        )
        monkeypatch.setattr(financial_intake, "download_file", lambda *_: statement)
        receipt = await call(
            "docket_submit_financial_evidence",
            dict(
                context_id=(await packet())["context_id"],
                operation_id="statement-file",
                file={
                    "file_id": "jan-feb",
                    "mime_type": "text/csv",
                    "download_url": "https://files.oaiusercontent.com/test",
                },
                evidence_role="statement",
            ),
        )
        source = receipt["source_ref"]
        await write(
            "docket_create_statement_import",
            "import",
            [acct, source],
            account_ref=acct,
            source_ref=source,
            period_start="2026-01-01",
            period_end="2026-02-28",
        )
        await write(
            "docket_create_recurring_expense",
            "expect",
            [acct],
            account_ref=acct,
            merchant="Hosting",
            first_expected_on="2026-01-31",
            cadence="monthly",
            amount="12",
            currency="USD",
        )
        renewal = await packet(section="recurrence")
        states = {r["due"]: r["status"] for r in renewal["rows"] if "due" in r}
        assert states["2026-01-31"] == "missing" and states["2026-02-28"] == "price_changed"
        assert states["2026-03-31"] == "coverage_unknown"
        assert not (await packet(section="expenses"))["rows"]

        cost = (
            await change(
                call,
                "docket_record_expense",
                [],
                "equipment",
                amount="100",
                currency="USD",
                description="Equipment",
                incurred_on="2026-01-08",
            )
        )["expenses"][0]["ref"]
        fields = dict(
            expense_ref=cost,
            tax_year=2026,
            jurisdiction="US",
            allocated_amount="75",
            activity="asset",
            status="asset_review",
            rationale="Review asset basis",
        )
        added = await write("docket_create_expense_treatment", "asset", [cost], **fields)
        with session_factory() as db:
            old = db.scalar(
                select(FinancialReviewFact.ref_id).where(
                    FinancialReviewFact.changeset_ref == added["changeset_ref"]
                )
            )
        ctx = await packet(refs=[cost])
        over = await call(
            "docket_create_expense_treatment",
            dict(
                context_id=ctx["context_id"],
                operation_id="over",
                **{**fields, "allocated_amount": "30", "activity": "employee"},
            ),
        )
        assert over.get("disposition") != "committed"
        await write(
            "docket_create_expense_treatment",
            "employee",
            [cost],
            **{
                **fields,
                "allocated_amount": "25",
                "activity": "employee",
                "status": "excluded",
                "reviewed_deduction": "0",
            },
        )
        await write(
            "docket_create_financial_withdrawal",
            "withdraw",
            [old],
            prior_ref=old,
            reason="Incorrect allocation",
        )
        ctx = await packet(refs=[old, cost])
        stale = await call(
            "docket_create_expense_treatment",
            dict(context_id=ctx["context_id"], operation_id="stale", prior_ref=old, **fields),
        )
        assert stale.get("disposition") != "committed"
        report = await packet(section="evidence")
        _, settings, _, _ = linked_plugin
        with session_factory() as db:
            stream = io.BytesIO()
            write_members(db, settings, report["report_ref"], [source], stream)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                assert archive.extractfile("originals/" + source).read() == statement
            with pytest.raises(ValueError, match="references"):
                write_members(db, settings, report["report_ref"], ["other"], io.BytesIO())


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": False}], indirect=True)
async def test_financial_review_tool_budgets_and_read_authority(linked_plugin):
    import json

    from docket.plugin.server import PluginMCP

    tools = {tool.name: tool for tool in await PluginMCP(linked_plugin[1]).list_tools()}
    names = [
        "docket_get_financial_year",
        "docket_create_activity_match",
        "docket_create_recurring_expense",
        "docket_create_expense_treatment",
        "docket_create_tax_allocation",
        "docket_create_income_evidence",
        "docket_create_financial_withdrawal",
        "docket_create_financial_year_review",
    ]
    for name in names:
        definition = tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)
        assert len(json.dumps(definition["inputSchema"]).encode()) <= 4096, name
        assert len(json.dumps(definition).encode()) <= 6144, name
    async with work_client(linked_plugin) as call:
        packet = await call("docket_get_financial_year", dict(tax_year=2026))
        result = await call(
            "docket_create_financial_year_review",
            dict(
                context_id=packet["context_id"],
                operation_id="read-only-review",
                report_ref=packet["report_ref"],
            ),
        )
        assert result["error"]["code"] == "agent_authority_denied"
