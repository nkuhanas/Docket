"""Prove duplicate acceptance and funding revision protection on disposable PostgreSQL."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from docket.agent_auth import AgentCallContext, agent_call_context
from docket.models.financial_intake import FinancialCollector, FinancialIntake
from docket.models.funding import FundingRevision
from docket.plugin.server import PluginMCP
from docket.services.financial_intake import FinancialIntakeService
from docket.services.funding import attachment_store


async def check_financial_intake(settings, factory, principal):
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        server = PluginMCP(settings)
        ctx = (await server.call_tool("docket_get_funding_context", {})).structured_content
        args = {
            "context_id": ctx["context_id"],
            "operation_id": "funding-migration-smoke",
            "label": "Synthetic funding account",
        }
        r = (await server.call_tool("docket_create_funding_account", args)).structured_content
        assert r.get("disposition") == "committed", r
        assert (await server.call_tool("docket_create_funding_account", args)).structured_content[
            "replayed"
        ]
    finally:
        agent_call_context.reset(token)
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(FundingRevision)) == 1
        grant = FinancialCollector(
            identity="synthetic-mail",
            operator_ref=principal.operator_ref,
            enabled=True,
            revision=1,
            credential_digest=hashlib.sha256(b"synthetic-only").hexdigest(),
            policy={"declaration": {"business_use_percent": "100", "write_off_intent": True}},
        )
        session.add(grant)
        session.flush()
        ref = grant.ref_id
    try:
        with factory.begin() as session:
            session.execute(update(FundingRevision).values(fields={}))
    except DBAPIError:
        pass
    else:
        raise AssertionError("Database allowed a funding history rewrite")

    blob = (b"From: synthetic@example.test\r\nSubject: fixture\r\n\r\n"
            b"Vendor: Fixture\r\nTotal: USD 1.00")
    barrier = Barrier(2)

    def deliver(_):
        with factory.begin() as session:
            grant = session.get(FinancialCollector, ref)
            barrier.wait(timeout=15)
            row = FinancialIntakeService(session, settings).admit(
                blob,
                delivery="1:1",
                input_hash=hashlib.sha256(blob).hexdigest(),
                declared="message/rfc822",
                filename=None,
                declarations={},
                collector=grant,
            )
            return row.source_ref

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.map(deliver, (1, 2))
    assert left == right
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(FinancialIntake)) == 1
        assert attachment_store(session, settings).plaintext(left) == blob
    return {"financial_original_concurrent_replay": "passed", "funding_revision_guard": "passed"}
