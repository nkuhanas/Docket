"""Native MCP transport over the isolated application test database for financial intake."""

import pytest
import test_native_plugin
from test_work_reports import work_client

linked_plugin = test_native_plugin.linked_plugin


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
async def test_attachment_tool_receipt_and_replay(linked_plugin, monkeypatch):
    import hashlib

    from docket.domain.public_refs import new_public_ref
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
            "business_use_percent": "60",
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
        monkeypatch.setattr(native, "download_file", lambda *args: blob)
        receipt = await call("docket_submit_financial_evidence", args)
        assert receipt.get("source_accepted"), receipt
        assert receipt["original_sha256"] == hashlib.sha256(blob).hexdigest()
        (outcome,) = receipt["outcomes"]
        assert outcome["disposition"] == "new_expense_recorded", receipt
        assert (
            outcome["funding"] == "unresolved"
            and outcome["declarations"]["business_use_percent"] == "60"
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
        expense = manual["outcomes"][0]["expense_ref"]
        ctx = await call("docket_get_financial_context", {"intake_ref": source})
        candidate = ctx["outcomes"][0]
        assert candidate["declarations"]["business_use_percent"] == "100"
        assert candidate.get("expense_ref") is None and candidate.get("activity_ref") is None
        await command(
            "docket_update_financial_candidate",
            ctx,
            "adopt-known",
            target_ref=candidate["candidate_ref"],
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
    with session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(Expense)) == 1
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
