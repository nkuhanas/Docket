"""Native MCP transport over the isolated application test database for financial intake."""

import pytest
import test_native_plugin
from sqlalchemy import select
from test_work_reports import work_client

linked_plugin = test_native_plugin.linked_plugin


@pytest.mark.parametrize(
    ("reported", "effective"),
    [
        ({"business_use_percent": "0"}, {}),
        ({}, {}),
        ({"business_use_percent": "85", "write_off_intent": False}, {}),
        ({"write_off_intent": True}, {}),
        ({"evidence_role": "income_document", "business_use_percent": "100"}, {}),
        ({"evidence_role": "statement", "business_use_percent": "100"}, {}),
        (
            {"business_use_percent": "0.000001"},
            {"write_off_intent": True, "write_off_intent_origin": "positive_business_use_v1"},
        ),
    ],
)
def test_read_claim_policy_on_retained_manual_declarations(reported, effective):
    from docket.models import FinancialIntake
    from docket.services.financial_intake import FinancialIntakeService

    # The historical row contains only the operator's original inputs.
    row = FinancialIntake(request_ref="fixture", declarations=dict(reported), policy_snapshot={})
    assert FinancialIntakeService.declaration(row) == {**reported, **effective}
    assert row.declarations == reported


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_funding_lifecycle(linked_plugin):
    async with work_client(linked_plugin) as call:

        async def packet(**kwargs):
            result = await call("docket_get_funding_context", kwargs)
            assert result.get("ok"), result
            return result

        async def write(name, ctx, key, **fields):
            result = await call(
                name, dict(context_id=ctx["context_id"], operation_id=key, **fields)
            )
            assert result.get("disposition") == "committed", result
            return result

        ctx = await packet()
        await write(
            "docket_create_funding_account", ctx, "bank", label="Amex", account_type="credit"
        )
        await write(
            "docket_create_funding_account", ctx, "bank", label="Amex", account_type="credit"
        )
        ctx = await packet()
        (account,) = ctx["objects"]
        for n in ("first", "second"):
            await write(
                "docket_create_payment_instrument",
                ctx,
                n,
                label=n,
                account_ref=account["ref"],
                masked_suffix="1234",
                role="authorized_user",
            )
        ctx = await packet()
        cards = [x for x in ctx["objects"] if x["type"] == "payment_instrument"]
        assert len(cards) == 2 and cards[0]["ref"] != cards[1]["ref"]
        ref = cards[0]["ref"]
        await write(
            "docket_create_account_activity",
            ctx,
            "purchase",
            account_ref=account["ref"],
            instrument_ref=ref,
            posted_on="2026-11-01",
            amount="-120",
            currency="USD",
            description="Synthetic purchase",
            kind="purchase",
        )
        prior = (await packet(account_ref=account["ref"]))["objects"][-1]["instrument_revision_ref"]
        await write(
            "docket_update_payment_instrument", ctx, "retire", target_ref=ref, status="retired"
        )
        ctx = await packet(refs=[ref, prior])
        assert ctx["objects"][0]["state"]["status"] == "retired"
        assert ctx["objects"][1]["state"]["status"] == "active"
        invalid = await call(
            "docket_create_payment_instrument",
            dict(context_id=ctx["context_id"], operation_id="bad", label="4111 1111 1111 1111"),
        )
        assert not invalid.get("ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_attachment_tool_receipt_and_replay(linked_plugin, monkeypatch, session_factory):
    import hashlib

    from docket.domain.public_refs import new_public_ref
    from docket.models import Expense, FinancialIntake
    from docket.plugin import financial_intake as native

    blob = (
        b"From: pay@example.test\r\nTo: billing@example.test\r\nSubject: Receipt\r\n\r\n"
        b"Vendor: Example\r\nReceipt ID: X42\r\nTotal: USD 120.00\r\n"
    )
    async with work_client(linked_plugin) as call:
        context = await call("docket_get_funding_context", {})
        args = {
            "context_id": context["context_id"],
            "operation_id": "synthetic-file",
            "business_use_percent": 60,
            "reported_instrument": "Amex",
            "file": {
                "file_id": "file-demo",
                "download_url": "https://files.oaiusercontent.com/synthetic",
                "mime_type": "message/rfc822",
                "file_name": "receipt.eml",
            },
        }
        bound_file = args["file"]
        unbound = {**bound_file, "download_url": "sandbox:/synthetic-receipt"}
        failed = await call("docket_submit_financial_evidence", {**args, "file": unbound})
        assert failed["error"]["code"] == "file_attachment_not_bound"
        assert "source_ref" not in failed
        standalone = {
            "context_id": context["context_id"],
            "operation_id": "fallback",
            "amount": "120",
            "currency": "USD",
            "description": "Example",
        }
        for fields in (
            {},
            {"receipt_ref": new_public_ref("src"), "manual_entry_requested": True},
            {"operation_id": args["operation_id"], "manual_entry_requested": True},
        ):
            rejected = await call("docket_record_expense", {**standalone, **fields})
            assert rejected["error"]["code"] == "expense_original_required", rejected
        with session_factory() as session:
            assert session.scalar(select(Expense.ref_id)) is None
            assert session.scalar(select(FinancialIntake.source_ref)) is None
        monkeypatch.setattr(native, "download_file", lambda *args: blob)
        receipt = await call("docket_submit_financial_evidence", args)
        assert receipt.get("source_accepted"), receipt
        assert receipt["original_sha256"] == hashlib.sha256(blob).hexdigest()
        (outcome,) = receipt["outcomes"]
        assert outcome["disposition"] == "candidate_created", receipt
        assert outcome["review_state"] == "needs_review"
        with session_factory() as session:
            assert session.scalar(select(Expense.ref_id)) is None
        assert outcome["funding"] == "unresolved" and outcome["tax_review"] == "unreviewed"
        assert (
            receipt["reported_declarations"]
            == outcome["declarations"]
            == {
                "business_use_percent": "60",
                "reported_instrument": "Amex",
                "write_off_intent": True,
                "write_off_intent_origin": "positive_business_use_v1",
            }
        )

        def unexpected_fetch(*args):
            pytest.fail("A retained receipt must be replayed without fetching the file again")

        monkeypatch.setattr(native, "download_file", unexpected_fetch)
        refreshed = {**bound_file, "download_url": "https://files.oaiusercontent.com/refreshed"}
        again = await call("docket_submit_financial_evidence", {**args, "file": refreshed})
        assert (
            again["outcomes"] == receipt["outcomes"]
            and again["source_ref"] == receipt["source_ref"]
        )
        assert not (
            await call("docket_get_financial_context", {"intake_ref": new_public_ref("src")})
        ).get("ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_receipt_through_both_channels_statement_match_and_revocation(
    linked_plugin, session_factory, monkeypatch
):
    import hashlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from docket.config import get_settings
    from docket.internal_api.financial_collector import router
    from docket.models import Expense
    from docket.models.financial_intake import FinancialCollector
    from docket.plugin import financial_intake as native
    from docket.services.financial_intake import FinancialIntakeService
    from docket.services.funding import attachment_store

    settings = get_settings()
    key = "s" * 43
    grant_policy = {
        "mailbox": "billing@chacel.ai",
        "folder": "INBOX",
        "backfill_start": "2026-11-22",
        "declaration": {"business_use_percent": "100", "write_off_intent": True},
        "max_bytes": 100000,
    }
    with session_factory.begin() as session:
        grant = FinancialCollector(
            operator_ref=settings.canonical_operator_ref,
            identity="fixture",
            credential_digest=hashlib.sha256(key.encode()).hexdigest(),
            enabled=True,
            revision=1,
            policy=grant_policy,
        )
        session.add(grant)
    app = FastAPI()
    app.include_router(router)
    mail = TestClient(app)
    headers = {"Authorization": "Bearer " + key, "X-Policy-Revision": "1", "X-Delivery-ID": "8:9"}
    path = "/internal/v1/financial-collector/messages"
    original = (
        b"From: vendor@example.test\r\nTo: billing@chacel.ai\r\nSubject: Receipt\r\n\r\n"
        b"Vendor: Test Merchant\r\nReceipt ID: T1\r\nTotal: USD 12.50\r\n"
    )
    assert mail.post(path, content=original).status_code == 401
    r = mail.post(path, headers=headers, content=original)
    assert r.status_code == 200, r.text
    source = r.json()["source_ref"]
    assert r.json()["extraction"] == "pending"
    assert mail.post(path, headers=headers, content=original).json()["source_ref"] == source
    assert mail.post(path, headers=headers, content=original + b"A").status_code == 422
    with session_factory.begin() as session:
        assert attachment_store(session, settings).plaintext(source) == original
        FinancialIntakeService(session, settings).process(source)
    async with work_client(linked_plugin) as call:

        async def packet(**kw):
            return await call("docket_get_funding_context", kw)

        async def command(name, ctx, key, **kw):
            r = await call(name, dict(context_id=ctx["context_id"], operation_id=key, **kw))
            assert r.get("disposition") == "committed", r
            return r

        async def upload(ctx, key, blob, mime, **kw):
            monkeypatch.setattr(native, "download_file", lambda *_: blob)
            result = await call(
                "docket_submit_financial_evidence",
                dict(
                    context_id=ctx["context_id"],
                    operation_id=key,
                    file={
                        "file_id": key,
                        "download_url": "https://files.oaiusercontent.com/test",
                        "mime_type": mime,
                    },
                    **kw,
                ),
            )
            assert result.get("source_accepted"), result
            return result

        ctx = await packet()
        manual = await upload(
            ctx, "interactive", original, "message/rfc822", business_use_percent="0"
        )
        assert manual["outcomes"][0]["declarations"]["business_use_percent"] == "0"
        inbox = await call("docket_get_financial_context", {"inbox": "outstanding"})
        assert {r["source_ref"] for r in inbox["items"]} == {source, manual["source_ref"]}

        async def manual_context():
            return await call("docket_get_financial_context", {"intake_ref": manual["source_ref"]})

        ctx = await manual_context()
        candidate_ref = manual["outcomes"][0]["candidate_ref"]
        await command(
            "docket_update_financial_candidate",
            ctx,
            "question",
            target_ref=candidate_ref,
            decision="ask",
            note="What was the credit for?",
        )
        ctx = await manual_context()
        assert ctx["outcomes"][0]["review_state"] == "awaiting_operator"
        assert ctx["outcomes"][0]["recent_decisions"][0]["note"] == "What was the credit for?"
        await command(
            "docket_update_financial_candidate",
            ctx,
            "reply",
            target_ref=candidate_ref,
            decision="answer",
            note="A coupon.",
        )
        ctx = await manual_context()
        booking = dict(
            candidate_ref=candidate_ref,
            vendor="Test Merchant",
            description="Purchase",
            amount="12.50",
            currency="USD",
        )
        recorded = await command("docket_record_expense", ctx, "reviewed-purchase", **booking)
        expense = recorded["expenses"][0]["ref"]
        assert (await command("docket_record_expense", ctx, "reviewed-purchase", **booking))[
            "expenses"
        ][0]["ref"] == expense
        assert (await manual_context())["outcomes"][0]["review_state"] == "booked"
        ctx = await call("docket_get_financial_context", {"intake_ref": source})
        candidate = ctx["outcomes"][0]
        assert candidate["declarations"]["business_use_percent"] == "100"
        assert candidate.get("expense_ref") is None and candidate.get("activity_ref") is None
        ctx = await call("docket_get_financial_context", {"intake_ref": source, "refs": [expense]})
        await command(
            "docket_update_financial_candidate",
            ctx,
            "adopt-known",
            target_ref=candidate["candidate_ref"],
            existing_expense_ref=expense,
            note="Both documents carry merchant invoice T1.",
        )
        merged = await call("docket_get_financial_context", {"intake_ref": source})
        assert merged["outcomes"][0]["expense_ref"] == expense
        assert merged["outcomes"][0]["conflicting_declarations"]
        assert merged["outcomes"][0]["tax_review"] == "unreviewed"

        await command(
            "docket_create_funding_account", await packet(), "account", label="Fixture bank"
        )
        account = (await packet())["objects"][0]["ref"]
        csv = (
            b"posted_on,description,amount,currency,kind,external_id\n"
            b"2026-11-22,Store,-12.50,USD,purchase,p1\n"
            b"11/22/26,Malformed,-90,USD,unknown,p2\n"
            b"2026-11-22,Repayment,12.50,USD,transfer,p3\n"
        )
        statement = await upload(await packet(), "csv", csv, "text/csv")
        args = dict(
            account_ref=account,
            source_ref=statement["source_ref"],
            period_start="2026-11-01",
            period_end="2026-11-30",
        )
        ctx = await packet(refs=[account, statement["source_ref"]])
        await command("docket_create_statement_import", ctx, "import", **args)
        await command("docket_create_statement_import", ctx, "repeat-import", **args)
        activities = [
            a
            for a in (await packet(account_ref=account))["objects"]
            if a["ref"].startswith("actv_")
        ]
        assert len(activities) == 2
        purchase = next(a["ref"] for a in activities if a["state"]["kind"] == "purchase")
        ctx = await call("docket_get_financial_context", {"intake_ref": source, "refs": [purchase]})
        await command(
            "docket_update_financial_candidate",
            ctx,
            "match",
            target_ref=candidate["candidate_ref"],
            activity_ref=purchase,
        )
        final = await call("docket_get_financial_context", {"intake_ref": source})
        assert final["outcomes"][0]["activity_ref"] == purchase
        image = await upload(
            await packet(), "unknown-image", b"\x89PNG\r\n\x1a\nexample", "image/png"
        )
        assert image["outcomes"][0]["disposition"] == "evidence_only"
        assert not image["outcomes"][0]["declarations"]
        assert image["review_required"]
        cost = await command(
            "docket_record_expense",
            await call("docket_get_financial_context", {"intake_ref": image["source_ref"]}),
            "cost-from-original",
            candidate_ref=image["outcomes"][0]["candidate_ref"],
            vendor="Test Merchant",
            description="Supported cost",
            amount="10",
            currency="USD",
            receipt_ref=image["source_ref"],
            source_method="import",
        )
        cost_ref = cost["expenses"][0]["ref"]
        linked = await call("docket_get_financial_context", {"intake_ref": image["source_ref"]})
        assert linked["extraction"] == "partial" and not linked["review_required"]
        assert linked["outcomes"][0]["expense_ref"] == cost_ref
    with session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(Expense)) == 2
        session.scalar(select(FinancialCollector)).enabled = False
    assert mail.post(path, headers=headers, content=original).status_code == 403


def test_imap_peek_checkpoint_loss_and_epoch_recovery():
    import hashlib
    import sqlite3

    from docket.financial_collector import MAILBOX, scan

    blob = b"Subject: synthetic\r\n\r\nbody"

    class Mail:
        epoch = b"11"

        def response(self, name):
            assert name == "UIDVALIDITY"
            return name, [self.epoch]

        def uid(self, command, *args):
            if command == "SEARCH":
                assert args[:3] == ("SINCE", "22-Nov-2026", "UID")
                return "OK", [b"1"]
            assert command == "FETCH"
            if args == ("1", "(RFC822.SIZE)"):
                return "OK", [f"1 (UID 1 RFC822.SIZE {len(blob)})".encode()]
            assert args == ("1", "(BODY.PEEK[])")
            return "OK", [(b"1 (BODY[]", blob)]

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT)")
        policy = {
            "mailbox": MAILBOX,
            "folder": "INBOX",
            "source": "test",
            "backfill_start": "2026-11-22",
            "max_bytes": 512,
        }
        mail = Mail()
        seen = []

        def accept(key, data):
            seen.append(key)
            assert data == blob
            return {
                "source_ref": "ok",
                "source_accepted": True,
                "original_sha256": hashlib.sha256(data).hexdigest(),
            }

        with pytest.raises(ValueError):
            scan(mail, db, policy, lambda k, d: {})
        assert db.execute("SELECT value FROM state WHERE key='checkpoint'").fetchone() is None
        scan(mail, db, policy, accept)
        scan(mail, db, policy, accept)
        mail.epoch = b"12"
        scan(mail, db, policy, accept)
        assert seen == ["11:1", "12:1"]


def test_download_host_boundary_and_private_capability(monkeypatch, caplog):
    from docket.domain.errors import DocketError
    from docket.services import financial_files as files

    visited = []

    class Redirect:
        status = 302

        def getheader(self, name):
            return "https://outside.example.test/private?cap=synthetic-private"

    class Connection:
        def __init__(self, host, **options):
            visited.append(host)

        def request(self, method, path):
            pass

        def getresponse(self):
            return Redirect()

        def close(self):
            pass

    monkeypatch.setattr(files.http.client, "HTTPSConnection", Connection)
    hosts = frozenset({"*.oaiusercontent.com"})
    approved_url = "https://files.oaiusercontent.com/private?cap=synthetic-private"
    unapproved_url = "https://oaiusercontent.com.invalid/private?cap=synthetic-private"
    for url, expected in (
        ("file-demo", "file_attachment_not_bound"),
        (approved_url, "file_redirect_not_approved"),
        (unapproved_url, "file_host_not_approved"),
    ):
        with pytest.raises(DocketError) as rejected:
            files.download_file(url, hosts, 100)
        assert rejected.value.code == expected
        assert "synthetic-private" not in str(rejected.value.as_dict())
    assert visited == ["files.oaiusercontent.com"]
    monkeypatch.setattr(Redirect, "status", 403)
    with pytest.raises(DocketError) as unavailable:
        files.download_file(approved_url, hosts, 100)
    assert unavailable.value.code == "file_download_unavailable"
    assert "synthetic-private" not in str(unavailable.value.as_dict())

    def offline(self, method, path):
        raise files.socket.gaierror("synthetic-private")

    monkeypatch.setattr(Connection, "request", offline)
    with pytest.raises(DocketError) as dns:
        files.download_file(approved_url, hosts, 100)
    assert dns.value.code == "file_download_dns_failed"
    assert "synthetic-private" not in str(dns.value.as_dict())
    assert "synthetic-private" not in caplog.text


def test_host_files_without_optional_type_hints():
    from pathlib import Path

    from docket.services.financial_files import extract, media_type

    pdf = (
        Path(__file__).resolve().parents[2] / "evidence/financial-intake/synthetic-receipt.pdf"
    ).read_bytes()
    assert media_type(pdf, None, None) == "application/pdf"
    (purchase,) = extract(pdf, "application/pdf")
    assert purchase["vendor"] == "Example Fixture" and purchase["amount"] == "1.00"
    assert "document_id" not in purchase
    assert media_type(b"posted_on,amount,currency\n", None, None) == "text/csv"
    assert media_type(b"Subject: Test\r\n\r\ntest", None, None) == "message/rfc822"


def test_single_page_invoice_reads_columns_from_original_bytes():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from test_attachment_evidence import _pdf_bytes

    from docket.services.financial_files import extract, media_type

    writer = PdfWriter()
    page = writer.add_page(PdfReader(BytesIO(_pdf_bytes("Invoice"))).pages[0])
    fields = [
        (40, 720, "Invoice"),
        (40, 700, "Date of issue   June 18, 2026"),
        (40, 680, "Invoice number INV-19"),
        (40, 650, "Example Supply LLC"),
        (300, 650, "Bill to"),
        (300, 635, "Example Customer"),
        (300, 520, "Total"),
        (456, 520, "$1,200.00"),
        (300, 500, "Amount due"),
        (456, 500, "$1,200.00 USD"),
    ]
    stream = DecodedStreamObject()
    stream.set_data(
        "\n".join(f"BT /F1 12 Tf {x} {y} Td ({s}) Tj ET" for x, y, s in fields).encode("ascii")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = BytesIO()
    writer.write(pdf)
    raw = pdf.getvalue()
    (facts,) = extract(raw, media_type(raw, None, "attachment"))
    assert facts == {
        "anchor": "file/page/1",
        "method": "tabular_invoice_v1",
        "vendor": "Example Supply LLC",
        "currency": "USD",
        "amount": "1200.00",
        "incurred_on": "2026-06-18",
        "document_id": "INV-19",
    }


def test_tabular_invoice_leaves_ambiguous_or_non_purchase_evidence_for_review():
    from docket.services.financial_files import purchase, tabular_invoice

    source = (
        "Invoice\nDate of issue  June 18, 2026\nVendor LLC   Bill to\n"
        "Total $120\nAmount due $120.00 USD"
    )
    for layout in (
        source.replace("USD", "EUR"),
        source.replace(" USD", ""),
        source.replace("$120.00", "$20.00"),
        source + "\nTotal $105",
        source.replace("Vendor LLC   Bill to", "Bill to\nClient LLC"),
        source + "\nCredit note",
        source.replace("June 18", "June 31"),
    ):
        before = purchase(layout, "file/page/1")
        assert tabular_invoice(layout, before) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_mail_parts_visual_and_reopen(linked_plugin, session_factory, monkeypatch):
    import base64
    import hashlib
    from email.message import EmailMessage
    from io import BytesIO

    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import ImageContent
    from PIL import Image, ImageDraw

    from docket.models import FinancialCollector, PluginNotification
    from docket.services import financial_intake
    from docket.services.financial_intake import FinancialIntakeService

    app, settings, token, _principal = linked_plugin
    # The second page's blue marker exists only in the raster layer.
    first = Image.new("RGB", (400, 600), "white")
    second = first.copy()
    ImageDraw.Draw(second).rectangle((100, 180, 310, 440), fill="blue")
    pdf = BytesIO()
    first.save(pdf, format="PDF", save_all=True, append_images=[second])
    message = EmailMessage()
    message["Subject"] = "Documents"
    message.set_content("See attached.")
    message.add_attachment(pdf.getvalue(), maintype="application", subtype="pdf", filename="a.pdf")
    message.add_attachment(b"account period\n", maintype="text", subtype="plain", filename="b.txt")
    raw = message.as_bytes()

    monkeypatch.setattr(
        financial_intake,
        "classify",
        lambda doc, *_: {
            "a.pdf": "other",
            "b.txt": "statement",
            "": "uncertain",
        }[doc.name],
    )
    with session_factory.begin() as db:
        grant = FinancialCollector(
            identity="visual-test",
            operator_ref=settings.canonical_operator_ref,
            enabled=True,
            revision=1,
            credential_digest="a" * 64,
            policy={"declaration": {"business_use_percent": "100"}},
        )
        db.add(grant)
        db.flush()
        service = FinancialIntakeService(db, settings)
        src = service.admit(
            raw,
            delivery="visual",
            input_hash=hashlib.sha256(raw).hexdigest(),
            declared="message/rfc822",
            filename=None,
            declarations={},
            collector=grant,
        ).source_ref
        service.process(src)
        for _ in range(4):
            service.suggest_pending()
        assert (
            len(
                db.scalars(
                    select(PluginNotification).where(
                        PluginNotification.event_name == "docket.finance.attention",
                        PluginNotification.subject_ref == src,
                    )
                ).all()
            )
            == 1
        )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), headers={"Authorization": "Bearer " + token}
        ) as http,
        Client(
            streamable_http_client("https://docket.you/mcp/", http_client=http), mode="2026-07-28"
        ) as client,
    ):

        async def packet():
            return (
                await client.call_tool("docket_get_financial_context", {"intake_ref": src})
            ).structured_content

        ctx = await packet()
        parts = {r["facts"]["anchor"]: r for r in ctx["outcomes"]}
        candidate = parts["mime/2"]
        assert candidate["review_state"] == parts["mime/3"]["review_state"] == "dismissed"
        changed = await client.call_tool(
            "docket_update_financial_candidate",
            {
                "context_id": ctx["context_id"],
                "operation_id": "reopen-visual",
                "target_ref": candidate["candidate_ref"],
                "decision": "reopen",
                "note": "The retained PDF is a receipt.",
            },
        )
        assert changed.structured_content["disposition"] == "committed"
        assert (await packet())["outcomes"][1]["review_state"] == "needs_review"
        args = {"source_ref": src, "part": "mime/2", "page": 2}
        page = await client.call_tool("docket_read_financial_page", args)
        assert page.structured_content.get("pages") == 2, page.structured_content
        (image,) = [c for c in page.content if isinstance(c, ImageContent)]
        with Image.open(BytesIO(base64.b64decode(image.data))) as picture:
            r, g, b = picture.getpixel((picture.width // 2, picture.height // 2))
            assert b > 200 and max(r, g) < 50
        invalid = await client.call_tool("docket_read_financial_page", {**args, "page": 3})
        assert invalid.structured_content["error"]["code"] == "financial_page_limit"
        from docket.models import FinancialIntake

        with session_factory.begin() as db:
            db.get(FinancialIntake, src).operator_ref = "different-owner"
        denied = await client.call_tool("docket_read_financial_page", args)
        assert denied.structured_content["error"]["code"] == "financial_source_unavailable"
        assert not any(isinstance(c, ImageContent) for c in denied.content)
