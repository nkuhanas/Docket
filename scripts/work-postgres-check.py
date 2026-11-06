"""Synthetic work migration, canonical commands, immutability and race evidence."""

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

ROOT = Path(__file__).resolve().parents[1]


async def check_native_timers(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import WorkSession, WorkSessionRevision
    from docket.plugin.server import PluginMCP

    server = PluginMCP(settings)
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        packet = (await server.call_tool("docket_get_work_context", {})).structured_content
    finally:
        agent_call_context.reset(token)
    barrier = Barrier(2)

    def run_call(name, arguments, *, synchronize=True):
        async def invoke():
            context = agent_call_context.set(AgentCallContext(principal))
            try:
                endpoint = PluginMCP(settings)
                if synchronize:
                    barrier.wait(timeout=10)
                return (await endpoint.call_tool(name, arguments)).structured_content
            finally:
                agent_call_context.reset(context)

        return asyncio.run(invoke())

    start_arguments = [
        {
            "context_id": packet["context_id"],
            "operation_id": f"timer-start-race-{index}",
            "description": f"Synthetic timer start {index}",
        }
        for index in (1, 2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda args: run_call("docket_start_work_session", args), start_arguments)
        )
    assert sum(result.get("disposition") == "committed" for result in results) == 1, results
    rejected = next(result for result in results if result.get("ok") is False)
    assert rejected["error"]["code"] == "primary_timer_exists", rejected
    winner_index = next(
        index for index, result in enumerate(results) if result.get("disposition") == "committed"
    )
    winner = results[winner_index]
    old_ref = winner["work_records"][0]["ref"]
    assert rejected["error"]["details"]["work_ref"] == old_ref
    cold_read = """
import asyncio, sys
from docket.agent_auth import AgentPrincipal, AgentCallContext, agent_call_context
from docket.config import get_settings
from docket.database import configure_database
from docket.plugin.server import PluginMCP
settings = get_settings()
configure_database(settings.database_url)
permissions = frozenset({'read', 'stage', 'commit', 'resolve_conflict'})
principal = AgentPrincipal(sys.argv[1], sys.argv[2], 'interactive', permissions)
async def check():
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        result = (await PluginMCP(settings).call_tool(
            'docket_get_work_context', {'refs':[sys.argv[3]]})).structured_content
        assert result['ok'] and any(
            item['ref'] == sys.argv[3] and item['state']['status'] == 'running'
            for item in result['objects'])
    finally:
        agent_call_context.reset(token)
asyncio.run(check())
"""
    cold = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", cold_read, principal.principal_ref, principal.operator_ref, old_ref],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if cold.returncode != 0:
        raise AssertionError("Fresh-process timer read failed: " + cold.stderr[-1500:])
    replay = await asyncio.to_thread(
        run_call,
        "docket_start_work_session",
        start_arguments[winner_index],
        synchronize=False,
    )
    assert replay.pop("replayed") and replay == winner
    # A fresh MCP service instance observes the retained primary timer.
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        fresh = PluginMCP(settings)
        packet = (
            await fresh.call_tool("docket_get_work_context", {"refs": [old_ref]})
        ).structured_content
        assert any(
            item["ref"] == old_ref and item["state"]["status"] == "running"
            for item in packet["objects"]
        )
        previews = []
        for index in (1, 2):
            preview = (
                await fresh.call_tool(
                    "docket_switch_work_session",
                    {
                        "context_id": packet["context_id"],
                        "operation_id": f"timer-switch-race-{index}",
                        "from_work_ref": old_ref,
                        "description": f"Synthetic switched timer {index}",
                        "preview": True,
                    },
                )
            ).structured_content
            assert preview.get("disposition") == "prepared", preview
            previews.append(preview)
    finally:
        agent_call_context.reset(token)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        switched = list(
            pool.map(
                lambda preview: run_call(
                    "docket_apply_change",
                    {
                        "change_id": preview["change_id"],
                    },
                ),
                previews,
            )
        )
    assert sum(result.get("disposition") == "committed" for result in switched) == 1, switched
    rejected = next(result for result in switched if result.get("ok") is False)
    assert rejected["error"]["code"] in {"changeset_validation_failed", "work_version_conflict"}, (
        rejected
    )
    winner = next(result for result in switched if result.get("disposition") == "committed")
    old, new = winner["work_records"]
    assert old["ref"] == old_ref and old["status"] == "recorded" and new["status"] == "running"
    with factory() as session:
        active = list(
            session.scalars(
                select(WorkSession).where(WorkSession.lifecycle.in_(["running", "paused"]))
            )
        )
        assert len(active) == 1 and active[0].ref_id == new["ref"]
        old_revision = session.scalar(
            select(WorkSessionRevision).where(
                WorkSessionRevision.work_ref == old_ref,
                WorkSessionRevision.revision == 2,
            )
        )
        new_revision = session.scalar(
            select(WorkSessionRevision).where(
                WorkSessionRevision.work_ref == new["ref"],
                WorkSessionRevision.revision == 1,
            )
        )
        assert old_revision.end_at == new_revision.start_at
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        packet = (
            await fresh.call_tool("docket_get_work_context", {"refs": [new["ref"]]})
        ).structured_content
        stopped = (
            await fresh.call_tool(
                "docket_stop_work_session",
                {
                    "context_id": packet["context_id"],
                    "operation_id": "timer-race-clean-stop",
                    "work_ref": new["ref"],
                },
            )
        ).structured_content
        assert stopped.get("disposition") == "committed", stopped
    finally:
        agent_call_context.reset(token)


async def check_compensation_policies(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.models import (
        CompensationEvaluation,
        CompensationPolicy,
        CompensationPolicyRevision,
        CompensationRevisionTerm,
        CompensationTerm,
        ToolInvocation,
    )
    from docket.plugin.server import PluginMCP
    from docket.services.compensation_policies import CompensationPolicyService

    async def call(name, arguments, actor=principal):
        token = agent_call_context.set(AgentCallContext(actor))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    packet = await call("docket_get_work_context", {})
    created = await call(
        "docket_create_engagement",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-pay-engagement",
            "label": "Synthetic compensation engagement",
            "kind": "contract",
        },
    )
    assert created.get("disposition") == "committed", created
    eng_ref = next(ref for ref in created["affected_refs"] if ref.startswith("eng_"))
    for index, values in enumerate(
        (
            {
                "effective_from": "2026-10-01",
                "rate": "80.123456789",
                "currency": "USD",
                "billable": True,
            },
            {"effective_from": "2026-11-15", "rate": "90"},
            {"effective_from": "2026-11-10", "effective_to": "2026-11-12", "rate": "85"},
        )
    ):
        packet = await call("docket_get_work_context", {"refs": [eng_ref]})
        result = await call(
            "docket_set_pay_terms",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-pay-term-{index}",
                "engagement_ref": eng_ref,
                "timezone": "America/Los_Angeles",
                **values,
            },
        )
        assert result.get("disposition") == "committed", result
    packet = await call("docket_get_work_context", {"refs": [eng_ref]})
    result = await call(
        "docket_set_reporting_rules",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-pay-reporting",
            "engagement_ref": eng_ref,
            "effective_from": "2026-10-01",
            "period_days": 14,
            "anchor_date": "2026-10-05",
            "overtime_after_hours": 40,
            "overtime_multiplier": "1.5",
        },
    )
    assert result.get("disposition") == "committed", result
    with factory() as session:
        service = CompensationPolicyService(session)
        head = session.scalar(
            select(CompensationPolicy).where(
                CompensationPolicy.engagement_ref == eng_ref,
                CompensationPolicy.kind == "pay",
            )
        )
        head_ref = head.ref_id
        revision = service.revision(head)
        revision_ref = revision.ref_id
        terms = service.terms(revision_ref)
        assert [row.rate for row in terms] == ["80.123456789", "85", "80.123456789", "90"]
        assert all(row.currency == "USD" and row.billable for row in terms)
        term_ref = terms[0].ref_id
        original = session.scalar(
            select(CompensationTerm)
            .where(
                CompensationTerm.policy_ref == head_ref, CompensationTerm.derived_from_ref.is_(None)
            )
            .order_by(CompensationTerm.created_at)
            .limit(1)
        )
        assert original.end_at is None and original.rate == "80.123456789"
        original_ref = original.ref_id
        root_ref = revision.created_by_changeset_ref
        version = head.version
    packet = await call("docket_get_work_context", {"refs": [eng_ref]})
    changed = await call(
        "docket_set_pay_terms",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-pay-currency-only",
            "engagement_ref": eng_ref,
            "effective_from": "2026-11-01",
            "currency": "CAD",
        },
    )
    assert changed.get("disposition") == "committed", changed
    with factory() as session:
        service = CompensationPolicyService(session)
        head = session.scalar(
            select(CompensationPolicy).where(CompensationPolicy.ref_id == head_ref)
        )
        version = head.version
        current = service.terms(service.revision(head).ref_id)
        assert [t.rate for t in current] == [
            "80.123456789",
            "80.123456789",
            "85",
            "80.123456789",
            "90",
        ]
        assert [t.currency for t in current] == ["USD", "CAD", "CAD", "CAD", "CAD"]
    packet = await call("docket_get_work_context", {"refs": [head_ref]})
    assert packet.get("ok"), packet
    assert packet["objects"][0]["state"]["engagement_ref"] == eng_ref
    proposals = []
    for rate in (100, 110):
        proposal = await call(
            "docket_set_pay_terms",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-pay-race-{rate}",
                "engagement_ref": eng_ref,
                "effective_from": "2026-12-01",
                "rate": rate,
                "preview": True,
            },
        )
        assert proposal.get("disposition") == "prepared", proposal
        proposals.append(proposal)
    barrier = Barrier(2)

    def apply(proposal):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_apply_change", {"change_id": proposal["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, proposals))
    assert sum(row.get("disposition") == "committed" for row in results) == 1, results
    with factory() as session:
        assert (
            session.scalar(
                select(CompensationPolicy.version).where(CompensationPolicy.ref_id == head_ref)
            )
            == version + 1
        )
    for statement, message in (
        (
            update(CompensationTerm).where(CompensationTerm.ref_id == term_ref).values(rate="0"),
            "append-only",
        ),
        (
            delete(CompensationRevisionTerm).where(
                CompensationRevisionTerm.revision_ref == revision_ref
            ),
            "append-only",
        ),
        (
            update(CompensationPolicyRevision)
            .where(CompensationPolicyRevision.ref_id == revision_ref)
            .values(sealed=False),
            "append-only",
        ),
        (
            CompensationRevisionTerm.__table__.insert().values(
                revision_ref=revision_ref, ordinal=99, term_ref=original_ref
            ),
            "sealed",
        ),
        (
            update(CompensationPolicy)
            .where(CompensationPolicy.ref_id == head_ref)
            .values(operator_ref="other"),
            "immutable",
        ),
        (
            update(CompensationPolicy)
            .where(CompensationPolicy.ref_id == head_ref)
            .values(version=version + 2),
            "sealed revision",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig), str(exc.orig)
        else:
            raise AssertionError("PostgreSQL allowed invalid compensation history")
    try:
        with factory.begin() as session:
            head = session.scalar(
                select(CompensationPolicy).where(CompensationPolicy.ref_id == head_ref)
            )
            old_version = head.version
            session.execute(
                update(CompensationPolicy)
                .where(CompensationPolicy.ref_id == head_ref)
                .values(version=old_version + 1)
            )
            session.execute(
                update(CompensationPolicy)
                .where(CompensationPolicy.ref_id == head_ref)
                .values(version=old_version + 2)
            )
            revision = CompensationPolicyRevision(
                policy_ref=head_ref,
                revision=old_version + 2,
                created_by_changeset_ref=root_ref,
                sealed=False,
                basis_refs=[],
            )
            session.add(revision)
            session.flush()
            session.add(
                CompensationRevisionTerm(
                    revision_ref=revision.ref_id, ordinal=0, term_ref=original_ref
                )
            )
            session.flush()
            revision.sealed = True

    except DBAPIError as exc:
        assert "sealed revision" in str(exc.orig)
    else:
        raise AssertionError("PostgreSQL allowed skipped compensation revisions")
    try:
        with factory.begin() as session:
            original = session.scalar(
                select(CompensationTerm).where(CompensationTerm.ref_id == original_ref)
            )
            fields = {
                column.name: getattr(original, column.name)
                for column in CompensationTerm.__table__.columns
                if column.name not in {"id", "ref_id"}
            }
            fields.update(rate="-1", derived_from_ref=None)
            session.execute(CompensationTerm.__table__.insert().values(**fields))
    except DBAPIError as exc:
        assert "ck_compensation_decimal_rate" in str(exc.orig)
    else:
        raise AssertionError("PostgreSQL accepted an invalid monetary rate")
    try:
        with factory.begin() as session:
            head = session.scalar(
                select(CompensationPolicy).where(CompensationPolicy.ref_id == head_ref)
            )
            revision = CompensationPolicyRevision(
                policy_ref=head_ref,
                revision=head.version + 1,
                basis_refs=[],
                created_by_changeset_ref=root_ref,
                sealed=False,
            )
            session.add(revision)
            session.flush()
            session.add(
                CompensationRevisionTerm(
                    revision_ref=revision.ref_id, ordinal=0, term_ref=original_ref
                )
            )
            session.flush()
            session.add(
                CompensationRevisionTerm(revision_ref=revision.ref_id, ordinal=1, term_ref=term_ref)
            )
            session.flush()
    except DBAPIError as exc:
        assert "overlap" in str(exc.orig)
    else:
        raise AssertionError("PostgreSQL allowed overlapping effective compensation windows")
    other = AgentPrincipal(
        "synthetic-other-pay-reader", "synthetic-other-operator", "interactive", frozenset({"read"})
    )
    for name, arguments in (
        ("docket_get_work_context", {"refs": [head_ref]}),
        ("docket_get_history_entry", {"ref": revision_ref, "view": "audit"}),
    ):
        denied = await call(name, arguments, other)
        assert denied.get("ok") is False, denied
        assert "80.123456789" not in json.dumps(denied)
    packet = await call("docket_get_work_context", {"refs": [eng_ref]})
    logged = await call(
        "docket_log_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-compensation-work",
            "description": "Synthetic compensation calculation",
            "engagement_ref": eng_ref,
            "duration_minutes": 90,
            "work_date": "2026-11-04",
            "timezone": "America/Los_Angeles",
        },
    )
    assert logged.get("disposition") == "committed", logged
    evaluated = await call(
        "docket_get_compensation",
        {
            "engagement_ref": eng_ref,
            "date_from": "2026-11-02",
            "date_to": "2026-11-09",
            "timezone": "America/Los_Angeles",
        },
    )
    assert evaluated.get("ok"), evaluated
    assert evaluated["summary"]["gross_estimates"] == [
        {"currency": "CAD", "amount": "120.19", "money_decimals": 2}
    ], evaluated
    for reader in (
        other,
        AgentPrincipal(
            "synthetic-same-owner-other-reader",
            principal.operator_ref,
            "interactive",
            frozenset({"read"}),
        ),
    ):
        denied = await call(
            "docket_get_compensation", {"cursor": evaluated["export_cursor"]}, reader
        )
        assert denied.get("ok") is False and "120.19" not in json.dumps(denied), denied
    try:
        with factory.begin() as session:
            session.execute(
                update(CompensationEvaluation)
                .where(CompensationEvaluation.ref_id == evaluated["evaluation_ref"])
                .values(calculation_version="rewritten")
            )
    except DBAPIError as exc:
        assert "immutable" in str(exc.orig)
    else:
        raise AssertionError("PostgreSQL allowed rewriting a retained compensation calculation")
    with factory() as session:
        rows = [dict(row) for row in session.execute(select(ToolInvocation.__table__)).mappings()]
        assert "80.123456789" not in json.dumps(rows, default=str)


async def check_native_invoices(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import (
        FinancialDocumentObservation,
        FinancialDocumentRevision,
        InvoiceNumber,
        WorkClaim,
    )
    from docket.plugin.server import PluginMCP

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    async def change(name, refs, identity, **fields):
        packet = await call("docket_get_work_context", {"refs": refs})
        result = await call(
            name, {"context_id": packet["context_id"], "operation_id": identity, **fields}
        )
        assert result.get("disposition") in {"committed", "prepared"}, result
        return result

    engagement = await change(
        "docket_create_engagement",
        [],
        "pg-native-invoice-eng",
        label="Synthetic invoice client",
        kind="contract",
    )
    eng = next(ref for ref in engagement["affected_refs"] if ref.startswith("eng_"))
    await change(
        "docket_set_pay_terms",
        [eng],
        "pg-native-invoice-rate",
        engagement_ref=eng,
        effective_from="2020-01-01",
        rate="80",
        currency="USD",
        billable=True,
    )
    logged = await change(
        "docket_log_work_session",
        [eng],
        "pg-native-invoice-work",
        engagement_ref=eng,
        description="Synthetic native invoice work",
        duration_minutes=90,
        work_date="2026-11-03",
        reported_compensation_class="billable",
    )
    work = logged["work_records"][0]["ref"]
    previews = [
        await change(
            "docket_prepare_invoice",
            [eng],
            f"pg-native-invoice-preview-{i}",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
            timezone="UTC",
            expense_refs=[],
            preview=True,
            number_series="PG-NATIVE",
        )
        for i in (1, 2)
    ]
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, previews))
    assert sum(row.get("disposition") == "committed" for row in results) == 1, results
    failed = next(row for row in results if row.get("ok") is False)
    assert failed["error"]["code"] == "invoice_claim_basis_changed", failed
    winner = next(row for row in results if row.get("disposition") == "committed")
    ref = winner["documents"][0]["ref"]
    assert winner["documents"][0]["gross_estimates"][0]["amount"] == "120.00", winner
    await change(
        "docket_update_work_session",
        [work],
        "pg-native-invoice-grow",
        work_ref=work,
        duration_minutes=120,
    )
    refreshed = await change(
        "docket_refresh_invoice", [ref], "pg-native-invoice-refresh", invoice_ref=ref
    )
    assert refreshed["documents"][0]["gross_estimates"][0]["amount"] == "160.00", refreshed
    packet = await call("docket_get_work_context", {"refs": [ref]})
    arguments = {
        "context_id": packet["context_id"],
        "operation_id": "pg-native-invoice-issue",
        "invoice_ref": ref,
        "status": "issued",
    }
    issued = await call("docket_update_invoice", arguments)
    assert issued.get("disposition") == "committed", issued
    assert issued["documents"][0]["number"] == "PG-NATIVE-000001", issued
    replay = await call("docket_update_invoice", arguments)
    assert replay.pop("replayed") and replay == issued
    issued_revision = issued["documents"][0]["revision_ref"]
    with factory() as session:
        frozen = session.scalar(
            select(FinancialDocumentRevision.snapshot_json).where(
                FinancialDocumentRevision.ref_id == issued_revision
            )
        )
    delivery = await change(
        "docket_update_invoice",
        [ref],
        "pg-native-invoice-delivery",
        invoice_ref=ref,
        status="delivered",
        note="Synthetic reported manual delivery",
    )
    observation = next(value for value in delivery["affected_refs"] if value.startswith("fobs_"))
    audit = await call("docket_get_history_entry", {"ref": observation, "view": "audit"})
    assert audit.get("ok") and "financial_observation_json_chunk" in audit["entry"], audit
    await change(
        "docket_update_work_session",
        [work],
        "pg-native-invoice-grow-after-issue",
        work_ref=work,
        duration_minutes=150,
    )
    amendment = await change(
        "docket_prepare_invoice",
        [eng],
        "pg-native-invoice-new-quantity",
        engagement_ref=eng,
        date_from="2026-11-02",
        date_to="2026-11-09",
        timezone="UTC",
        expense_refs=[],
        number_series="PG-NATIVE",
        amends_ref=ref,
    )
    assert amendment["documents"][0]["worked_us"] == 30 * 60_000_000, amendment
    assert amendment["documents"][0]["gross_estimates"][0]["amount"] == "40.00", amendment
    await change(
        "docket_update_invoice",
        [amendment["documents"][0]["ref"]],
        "pg-native-invoice-cancel-remainder",
        invoice_ref=amendment["documents"][0]["ref"],
        status="cancelled",
    )
    with factory() as session:
        current = session.scalar(
            select(FinancialDocumentRevision.snapshot_json).where(
                FinancialDocumentRevision.ref_id == issued_revision
            )
        )
        assert current == frozen
        assert (
            session.scalar(select(InvoiceNumber.number).where(InvoiceNumber.document_ref == ref))
            == "PG-NATIVE-000001"
        )
        claim = session.scalar(
            select(WorkClaim).where(WorkClaim.document_ref == ref, WorkClaim.status == "issued")
        )
        assert claim.end_us - claim.start_us == 120 * 60_000_000
    # Compiling a preview must not take quantity/expense locks before canonical
    # work-head locks. A correction can finish while this read transaction is open.
    from docket.schemas.compensation import GetCompensation
    from docket.schemas.invoices import InvoiceFields
    from docket.services.compensation_evaluations import CompensationEvaluationService
    from docket.services.invoice_claims import InvoiceClaimPlanner

    compiler_actor = agent_call_context.set(AgentCallContext(principal))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with factory.begin() as session:
            evaluation = CompensationEvaluationService(session, settings).capture(
                GetCompensation(
                    engagement_ref=eng,
                    date_from=date(2026, 11, 2),
                    date_to=date(2026, 11, 9),
                    timezone="UTC",
                )
            )
            InvoiceClaimPlanner(session).sources(
                evaluation, InvoiceFields(expense_refs=[]), "fdoc_" + "0" * 26, [], lock=False
            )
            future = pool.submit(
                lambda: asyncio.run(
                    change(
                        "docket_update_work_session",
                        [work],
                        "pg-native-invoice-compile-lock-order",
                        work_ref=work,
                        duration_minutes=180,
                    )
                )
            )
            corrected = await asyncio.to_thread(future.result, 10)
            assert corrected.get("disposition") == "committed", corrected
    finally:
        agent_call_context.reset(compiler_actor)
        pool.shutdown(wait=True)
    for statement in (
        update(FinancialDocumentObservation)
        .where(FinancialDocumentObservation.ref_id == observation)
        .values(note="rewrite"),
        delete(FinancialDocumentObservation).where(
            FinancialDocumentObservation.ref_id == observation
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert "immutable" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL allowed rewriting reported invoice delivery")


async def check_document_allocations(settings, factory, principal):
    from fractions import Fraction

    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.domain.errors import DocketError
    from docket.domain.public_refs import new_public_ref
    from docket.models import (
        DocumentAllocation,
        DocumentAllocationEvent,
        FinancialDocument,
        FinancialDocumentRevision,
        InvoiceNumber,
        WorkClaim,
    )
    from docket.services.document_allocations import DocumentAllocationService

    with factory() as session:
        claim = session.scalar(
            select(WorkClaim).where(WorkClaim.status == "reported").order_by(WorkClaim.ref_id)
        )
        source_ref = claim.ref_id
        prior = session.scalar(
            select(FinancialDocumentRevision)
            .where(FinancialDocumentRevision.document_ref == claim.document_ref)
            .order_by(FinancialDocumentRevision.revision.desc())
        )
        parameters = dict(
            evaluation_ref=prior.evaluation_ref,
            date_from=prior.date_from,
            date_to=prior.date_to,
            timezone=prior.timezone,
            created_by_changeset_ref=prior.created_by_changeset_ref,
            basis_refs=prior.basis_refs,
        )
        eng, component, start, end = (
            claim.engagement_ref,
            claim.component_key,
            claim.start_us,
            claim.end_us,
        )
        source_key = f"work:{claim.origin_ref}:{component}"
    middle = start + (end - start) // 2
    quote = {
        "kind": "work",
        "source_start": start,
        "line_offset_us": 0,
        "billable": True,
        "basis": {
            "raw_us": end - start,
            "reportable_us": end - start,
            "overtime_us": end - middle,
            "rate": "80",
            "overtime_multiplier": "1.5",
        },
    }
    barrier = Barrier(2)

    def create(lo, hi, synchronize=False):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            with factory.begin() as session:
                doc = FinancialDocument(
                    ref_id=new_public_ref("fdoc"),
                    operator_ref=principal.operator_ref,
                    kind="invoice",
                    engagement_ref=eng,
                    component_key=component,
                    status="draft",
                    version=1,
                    created_by_changeset_ref=parameters["created_by_changeset_ref"],
                )
                session.add(doc)
                session.flush()
                if synchronize:
                    barrier.wait(timeout=10)
                service = DocumentAllocationService(session)
                rows = service.allocate(
                    doc.ref_id,
                    work_claim_ref=source_ref,
                    source_key=source_key,
                    start=lo,
                    end=hi,
                    currency="USD",
                    quote=quote,
                    changeset_ref=parameters["created_by_changeset_ref"],
                )
                revision = FinancialDocumentRevision(
                    document_ref=doc.ref_id,
                    revision=1,
                    status="draft",
                    snapshot_json={
                        "allocation_refs": [row.ref_id for row in rows],
                        "claim_refs": [source_ref],
                    },
                    **parameters,
                )
                session.add(revision)
                session.flush()
                service.persist(rows, revision)
                return doc.ref_id
        except DocketError as exc:
            assert exc.code == "invoice_quantity_reserved", exc.as_dict()
            return None
        finally:
            agent_call_context.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _: create(start, middle, True), (1, 2)))
    assert sum(ref is not None for ref in created) == 1, created
    first = next(ref for ref in created if ref)
    second = create(middle, end)
    assert second
    barrier = Barrier(2)

    def issue(ref):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            with factory.begin() as session:
                doc = session.scalar(
                    select(FinancialDocument)
                    .where(FinancialDocument.ref_id == ref)
                    .with_for_update()
                )
                prior = session.scalar(
                    select(FinancialDocumentRevision).where(
                        FinancialDocumentRevision.document_ref == ref
                    )
                )
                doc.version += 1
                doc.status = "issued"
                session.flush()
                revision = FinancialDocumentRevision(
                    document_ref=ref,
                    revision=doc.version,
                    status="issued",
                    snapshot_json=prior.snapshot_json,
                    **parameters,
                )
                session.add(revision)
                session.flush()
                service = DocumentAllocationService(session)
                service.transition(ref, "issued", revision)
                barrier.wait(timeout=10)
                numbered = service.number(revision, "SYNTHETIC-2026")
                assert service.number(revision, "SYNTHETIC-2026").number == numbered.number
                return numbered.number
        finally:
            agent_call_context.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = list(pool.map(issue, (first, second)))
    assert sorted(numbers) == ["SYNTHETIC-2026-000001", "SYNTHETIC-2026-000002"], numbers
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        third = create(start, end)
        assert third
        with factory() as session:
            rows = list(
                session.scalars(
                    select(DocumentAllocation).where(DocumentAllocation.source_key == source_key)
                )
            )
            assert len(rows) == 2 and all(row.status == "issued" for row in rows)
            service = DocumentAllocationService(session)
            assert (
                sum(service.amount(row) for row in rows)
                == Fraction(end - start, 3_600_000_000) * 100
            )
            assert (
                session.scalar(select(WorkClaim.status).where(WorkClaim.ref_id == source_ref))
                == "reported"
            )
            allocation_ref = rows[0].ref_id
    finally:
        agent_call_context.reset(token)
    for statement, message in (
        (
            update(DocumentAllocation)
            .where(DocumentAllocation.ref_id == allocation_ref)
            .values(gross_numerator="999"),
            "immutable",
        ),
        (
            delete(DocumentAllocationEvent).where(
                DocumentAllocationEvent.allocation_ref == allocation_ref
            ),
            "immutable",
        ),
        (
            update(InvoiceNumber)
            .where(InvoiceNumber.document_ref == first)
            .values(number="rewritten"),
            "immutable",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig), str(exc.orig)
        else:
            raise AssertionError(
                "PostgreSQL allowed rewriting retained invoice allocations or numbers"
            )


async def check_expenses(settings, factory, principal):
    from sqlalchemy import func, insert

    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.models import (
        Expense,
        ExpenseObservation,
        ExpenseReportMember,
        ExpenseReportSnapshot,
        ExpenseRevision,
    )
    from docket.plugin.server import PluginMCP

    async def call(name, arguments, actor=principal):
        token = agent_call_context.set(AgentCallContext(actor))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    packet = await call("docket_get_work_context", {})

    def imported(index):
        return {
            "context_id": packet["context_id"],
            "operation_id": f"pg-expense-import-{index}",
            "amount": "0.000000003",
            "currency": "USD",
            "description": "Synthetic expense",
            "external_source": "synthetic-import",
            "external_id": "example-transaction",
            "reimbursable": True,
        }

    barrier = Barrier(2)

    def run_import(index):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_record_expense", imported(index)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run_import, (1, 2)))
    assert all(row.get("disposition") == "committed" for row in results), results
    refs = {row["expenses"][0]["ref"] for row in results}
    assert len(refs) == 1
    ref = refs.pop()
    packet = await call("docket_get_work_context", {"refs": [ref]})
    approved = await call(
        "docket_update_expense",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-expense-approve",
            "expense_ref": ref,
            "submission_status": "approved",
            "claim_amount": "0.000000002",
        },
    )
    assert approved.get("disposition") == "committed", approved
    approved_ref = approved["expenses"][0]["revision_ref"]
    packet = await call("docket_get_work_context", {"refs": [ref]})
    previews = [
        await call(
            "docket_update_expense",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-expense-correct-{index}",
                "expense_ref": ref,
                "amount": "0.000000005",
                "preview": True,
            },
        )
        for index in (1, 2)
    ]
    assert all(row.get("disposition") == "prepared" for row in previews), previews
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, previews))
    assert sum(row.get("disposition") == "committed" for row in results) == 1, results
    packet = await call("docket_get_work_context", {"refs": [ref]})
    state = packet["objects"][0]["state"]
    assert state["approved_amount"] == "0.000000002" and state["approved_basis_ref"] == approved_ref
    assert "expense_changed_since_approval" in state["flags"]
    report = await call("docket_query_expenses", {})
    assert report.get("ok"), report
    assert report["totals"][0]["reported_cost"] == "0.000000005", report
    assert report["totals"][0]["approved_amount"] == "0.000000002", report
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Expense)) == 1
        head = session.scalar(select(Expense).where(Expense.ref_id == ref))
        revision = session.scalar(
            select(ExpenseRevision).where(
                ExpenseRevision.expense_ref == ref, ExpenseRevision.revision == head.version
            )
        )
        version, revision_ref = head.version, revision.ref_id
        snapshot = session.scalar(
            select(ExpenseReportSnapshot).where(
                ExpenseReportSnapshot.ref_id == report["report"]["ref"]
            )
        )
        snapshot_id = snapshot.id
    for statement, message in (
        (
            update(Expense).where(Expense.ref_id == ref).values(operator_ref="wrong-owner"),
            "immutable",
        ),
        (
            update(Expense).where(Expense.ref_id == ref).values(version=version + 1),
            "retained revision",
        ),
        (
            update(ExpenseRevision)
            .where(ExpenseRevision.ref_id == revision_ref)
            .values(amount="8"),
            "immutable",
        ),
        (delete(ExpenseObservation).where(ExpenseObservation.expense_ref == ref), "immutable"),
        (
            update(ExpenseReportSnapshot)
            .where(ExpenseReportSnapshot.id == snapshot_id)
            .values(totals_json=[]),
            "seal",
        ),
        (
            insert(ExpenseReportMember).values(
                report_id=snapshot_id, expense_ref=ref, revision_ref=revision_ref
            ),
            "unsealed",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig), str(exc.orig)
        else:
            raise AssertionError("PostgreSQL allowed invalid expense or retained report mutation")
    other = AgentPrincipal(
        "synthetic-other-expense", "synthetic-other-owner", "interactive", frozenset({"read"})
    )
    same_owner = AgentPrincipal(
        "synthetic-alternate-expense", principal.operator_ref, "interactive", frozenset({"read"})
    )
    for name, arguments, actor in (
        ("docket_get_work_context", {"refs": [ref]}, other),
        ("docket_get_history_entry", {"ref": approved_ref, "view": "audit"}, other),
        ("docket_query_expenses", {"cursor": report["export_cursor"]}, same_owner),
    ):
        denied = await call(name, arguments, actor)
        assert denied.get("ok") is False, denied
    denied = await call("docket_query_expenses", {}, other)
    assert denied.get("ok") is False, denied


async def check_financial_documents(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.models import (
        FinancialDocument,
        FinancialDocumentRevision,
        WorkClaim,
        WorkClaimEvent,
    )
    from docket.plugin.server import PluginMCP

    async def call(name, arguments, actor=principal):
        token = agent_call_context.set(AgentCallContext(actor))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    packet = await call("docket_get_work_context", {})
    created = await call(
        "docket_create_engagement",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-timesheet-engagement",
            "label": "Synthetic timesheet engagement",
            "kind": "employment",
        },
    )
    assert created.get("disposition") == "committed", created
    eng = next(ref for ref in created["affected_refs"] if ref.startswith("eng_"))
    packet = await call("docket_get_work_context", {"refs": [eng]})
    logged = await call(
        "docket_log_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-timesheet-work",
            "description": "Synthetic claim source",
            "engagement_ref": eng,
            "duration_minutes": 90,
            "work_date": "2026-11-03",
            "timezone": "UTC",
        },
    )
    assert logged.get("disposition") == "committed", logged
    work = logged["work_records"][0]["ref"]
    packet = await call("docket_get_work_context", {"refs": [eng]})
    proposals = []
    for index in range(2):
        prepared = await call(
            "docket_prepare_timesheet",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-timesheet-preview-{index}",
                "engagement_ref": eng,
                "date_from": "2026-11-02",
                "date_to": "2026-11-09",
                "timezone": "UTC",
                "preview": True,
            },
        )
        assert prepared.get("disposition") == "prepared", prepared
        proposals.append(prepared)
    with factory() as session:
        assert not session.scalar(select(WorkClaim)) and not session.scalar(
            select(FinancialDocument)
        )
    barrier = Barrier(2)

    def apply(proposal):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_apply_change", {"change_id": proposal["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply, proposals))
    assert sum(row.get("disposition") == "committed" for row in outcomes) == 1, outcomes
    winner = next(row for row in outcomes if row.get("disposition") == "committed")
    doc = next(ref for ref in winner["affected_refs"] if ref.startswith("fdoc_"))
    packet = await call("docket_get_work_context", {"refs": [doc]})
    submitted = await call(
        "docket_update_timesheet",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-timesheet-submit",
            "timesheet_ref": doc,
            "status": "submitted",
            "note": "Synthetic reported manual submission",
        },
    )
    assert submitted.get("disposition") == "committed", submitted
    with factory() as session:
        revision = session.scalar(
            select(FinancialDocumentRevision).where(
                FinancialDocumentRevision.document_ref == doc,
                FinancialDocumentRevision.status == "submitted",
            )
        )
        submitted_ref, snapshot = revision.ref_id, revision.snapshot_json
    packet = await call("docket_get_work_context", {"refs": [work]})
    corrected = await call(
        "docket_update_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-timesheet-correct",
            "work_ref": work,
            "duration_minutes": 120,
        },
    )
    assert corrected.get("disposition") == "committed", corrected
    packet = await call("docket_get_work_context", {"refs": [eng, doc]})
    amended = await call(
        "docket_prepare_timesheet",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-timesheet-amend",
            "engagement_ref": eng,
            "date_from": "2026-11-02",
            "date_to": "2026-11-09",
            "timezone": "UTC",
            "amends_ref": doc,
        },
    )
    assert amended.get("disposition") == "committed", amended
    amendment = next(ref for ref in amended["affected_refs"] if ref.startswith("fdoc_"))
    with factory() as session:
        claim = session.scalar(select(WorkClaim).where(WorkClaim.document_ref == amendment))
        assert claim.end_us - claim.start_us == 30 * 60_000_000
        claim_ref, claim_version = claim.ref_id, claim.version
        assert (
            session.scalar(
                select(FinancialDocumentRevision.snapshot_json).where(
                    FinancialDocumentRevision.ref_id == submitted_ref
                )
            )
            == snapshot
        )
    for statement, message in (
        (
            update(FinancialDocumentRevision)
            .where(FinancialDocumentRevision.ref_id == submitted_ref)
            .values(snapshot_json={}),
            "immutable",
        ),
        (update(WorkClaim).where(WorkClaim.ref_id == claim_ref).values(end_us=1), "immutable"),
        (delete(WorkClaimEvent).where(WorkClaimEvent.claim_ref == claim_ref), "immutable"),
        (
            update(WorkClaim)
            .where(WorkClaim.ref_id == claim_ref)
            .values(status="reported", version=claim_version + 1),
            "transition evidence",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL allowed rewriting or incomplete financial history")
    other = AgentPrincipal(
        "synthetic-other-finance", "synthetic-other-operator", "interactive", frozenset({"read"})
    )
    for name, arguments in (
        ("docket_read_document", {"ref": doc, "format": "json"}),
        ("docket_get_work_context", {"refs": [doc]}),
        ("docket_get_history_entry", {"ref": submitted_ref, "view": "audit"}),
    ):
        denied = await call(name, arguments, other)
        assert denied.get("ok") is False, denied
    alternate = AgentPrincipal(
        "synthetic-same-owner-finance", principal.operator_ref, "interactive", frozenset({"read"})
    )
    read = await call("docket_read_document", {"ref": submitted_ref}, alternate)
    assert read.get("ok") and read["document"]["status"] == "submitted", read
    packet = await call("docket_get_work_context", {"refs": [amendment]})
    cancelled = await call(
        "docket_update_timesheet",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-timesheet-cancel",
            "timesheet_ref": amendment,
            "status": "cancelled",
        },
    )
    assert cancelled.get("disposition") == "committed", cancelled


async def check_native_lineage(settings, factory, principal):
    from docket.agent_auth import AgentCallContext, agent_call_context
    from docket.models import (
        WorkLineageEdge,
        WorkQuantityAllocation,
        WorkQuantityBasis,
        WorkQuantityOrigin,
        WorkSession,
        WorkSessionRevision,
    )
    from docket.plugin.server import PluginMCP

    minute = 60_000_000

    async def call(name, arguments):
        token = agent_call_context.set(AgentCallContext(principal))
        try:
            return (await PluginMCP(settings).call_tool(name, arguments)).structured_content
        finally:
            agent_call_context.reset(token)

    packet = await call("docket_get_work_context", {})
    logged = await call(
        "docket_log_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-lineage-log",
            "description": "Synthetic lineage",
            "duration_minutes": 90,
            "work_date": "2001-01-02",
        },
    )
    assert logged.get("disposition") == "committed", logged
    work_ref = logged["work_records"][0]["ref"]
    historical = await call(
        "docket_query_work_sessions",
        {
            "date_from": "2001-01-01",
            "date_to": "2001-01-08",
        },
    )
    for index, minutes in enumerate((120, 60, 120)):
        packet = await call("docket_get_work_context", {"refs": [work_ref]})
        changed = await call(
            "docket_update_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-lineage-correct-{index}",
                "work_ref": work_ref,
                "duration_minutes": minutes,
            },
        )
        assert changed.get("disposition") == "committed", changed
    packet = await call("docket_get_work_context", {"refs": [work_ref]})
    previews = []
    for index, durations in enumerate(((45, 75), (60, 60))):
        preview = await call(
            "docket_split_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-lineage-split-{index}",
                "work_ref": work_ref,
                "preview": True,
                "parts": [{"duration_minutes": value} for value in durations],
            },
        )
        assert preview.get("disposition") == "prepared", preview
        previews.append(preview)
    barrier = Barrier(2)

    def apply(preview):
        barrier.wait(timeout=10)
        return asyncio.run(call("docket_apply_change", {"change_id": preview["change_id"]}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, previews))
    assert sum(r.get("disposition") == "committed" for r in results) == 1, results
    winner = next(r for r in results if r.get("disposition") == "committed")
    children = [r for r in winner["work_records"] if not r["superseded"]]
    assert len(children) == 2
    with factory() as session:
        origin = session.scalar(
            select(WorkQuantityOrigin).where(WorkQuantityOrigin.source_work_ref == work_ref)
        )
        origin_ref = origin.ref_id
        assert origin.next_offset_us == 120 * minute
    packet = await call("docket_get_work_context", {"refs": [r["ref"] for r in children]})

    def grow(child):
        barrier.wait(timeout=10)
        return asyncio.run(
            call(
                "docket_update_work_session",
                {
                    "context_id": packet["context_id"],
                    "operation_id": "pg-grow-" + child["ref"],
                    "work_ref": child["ref"],
                    "duration_minutes": child["duration_us"] // minute + 15,
                },
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(grow, children))
    assert all(r.get("disposition") == "committed" for r in results), results
    with factory() as session:
        origin = session.scalar(
            select(WorkQuantityOrigin).where(WorkQuantityOrigin.ref_id == origin_ref)
        )
        assert origin.next_offset_us == 150 * minute
        revision_refs = (
            select(WorkSessionRevision.ref_id)
            .join(
                WorkSession,
                (WorkSession.ref_id == WorkSessionRevision.work_ref)
                & (WorkSession.version == WorkSessionRevision.revision),
            )
            .where(
                WorkSession.ref_id.in_([r["ref"] for r in children]),
            )
        )
        allocations = session.scalars(
            select(WorkQuantityAllocation)
            .where(
                WorkQuantityAllocation.revision_ref.in_(revision_refs),
            )
            .order_by(WorkQuantityAllocation.start_us)
        ).all()
        assert sum(a.end_us - a.start_us for a in allocations) == 150 * minute
        assert all(a.end_us <= b.start_us for a, b in pairwise(allocations))
        basis_ref = allocations[0].revision_ref
        edge_id = session.scalar(select(WorkLineageEdge.id))
    for statement, message in (
        (
            update(WorkQuantityOrigin)
            .where(WorkQuantityOrigin.ref_id == origin_ref)
            .values(next_offset_us=0),
            "append-only",
        ),
        (
            update(WorkQuantityBasis)
            .where(WorkQuantityBasis.revision_ref == basis_ref)
            .values(sealed=False),
            "append-only",
        ),
        (
            update(WorkQuantityAllocation)
            .where(WorkQuantityAllocation.revision_ref == basis_ref)
            .values(start_us=0),
            "append-only",
        ),
        (delete(WorkLineageEdge).where(WorkLineageEdge.id == edge_id), "append-only"),
        (
            WorkQuantityAllocation.__table__.insert().values(
                revision_ref=basis_ref,
                ordinal=999,
                origin_ref=origin_ref,
                start_us=0,
                end_us=1,
                needs_review=False,
            ),
            "sealed",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL permitted rewriting retained work lineage")
    packet = await call("docket_get_work_context", {"refs": [r["ref"] for r in children]})
    merged = await call(
        "docket_merge_work_sessions",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-lineage-merge",
            "work_refs": [r["ref"] for r in children],
        },
    )
    assert merged.get("disposition") == "committed", merged
    assert (
        next(r for r in merged["work_records"] if not r["superseded"])["duration_us"]
        == 150 * minute
    )
    current = await call(
        "docket_query_work_sessions", {"date_from": "2001-01-01", "date_to": "2001-01-08"}
    )
    retained = await call("docket_query_work_sessions", {"cursor": historical["report_cursor"]})
    assert current["totals"]["known_reported_us"] == 150 * minute
    assert retained["totals"]["known_reported_us"] == 90 * minute

    refs = [merged["work_records"][0]["ref"]]
    for index, fields in enumerate(
        (
            {"work_date": "2001-01-03"},
            {
                "start": "2001-01-04T10:00:00+00:00",
                "end": "2001-01-04T10:30:00+00:00",
                "timezone": "UTC",
            },
        )
    ):
        packet = await call("docket_get_work_context", {})
        logged = await call(
            "docket_log_work_session",
            {
                "context_id": packet["context_id"],
                "operation_id": f"pg-composite-{index}",
                "description": f"Synthetic time component {index}",
                **fields,
            },
        )
        assert logged.get("disposition") == "committed", logged
        refs.append(logged["work_records"][0]["ref"])
    packet = await call("docket_get_work_context", {"refs": refs})
    composite = await call(
        "docket_merge_work_sessions",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-composite-merge",
            "work_refs": refs,
        },
    )
    assert composite.get("disposition") == "committed", composite
    record = composite["work_records"][0]
    packet = await call("docket_get_work_context", {})
    extra = await call(
        "docket_log_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-nested-unknown",
            "description": "Synthetic additional incomplete work",
            "work_date": "2001-01-05",
        },
    )
    assert extra.get("disposition") == "committed", extra
    extra_ref = extra["work_records"][0]["ref"]
    nested_refs = [record["ref"], extra_ref]
    packet = await call("docket_get_work_context", {"refs": nested_refs})
    nested = await call(
        "docket_merge_work_sessions",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-nested-merge",
            "work_refs": nested_refs,
        },
    )
    assert nested.get("disposition") == "committed", nested
    record = nested["work_records"][0]
    refs.append(extra_ref)
    assert record.get("duration_us") is None and record["has_unknown_quantity"]
    assert record["known_duration_us"] == 180 * minute
    grouped = await call(
        "docket_query_work_sessions",
        {
            "date_from": "2001-01-01",
            "date_to": "2001-01-08",
            "group_by": "day",
        },
    )
    assert grouped.get("ok"), grouped
    assert grouped["totals"]["known_reported_us"] == 180 * minute
    assert grouped["totals"]["unknown_quantity_count"] == 1
    narrow = await call(
        "docket_query_work_sessions",
        {
            "date_from": "2001-01-02",
            "date_to": "2001-01-03",
        },
    )
    assert narrow["totals"]["known_reported_us"] == 150 * minute
    assert narrow["totals"]["unknown_quantity_count"] == 0
    from docket.models import WorkTimeComponent

    with factory() as session:
        current_ref = session.scalar(
            select(WorkSessionRevision.ref_id)
            .join(
                WorkSession,
                (WorkSession.ref_id == WorkSessionRevision.work_ref)
                & (WorkSession.version == WorkSessionRevision.revision),
            )
            .where(WorkSession.ref_id == record["ref"])
        )
        source_ref = session.scalar(
            select(WorkTimeComponent.source_revision_ref).where(
                WorkTimeComponent.revision_ref == current_ref,
            )
        )
    for statement, message in (
        (
            delete(WorkTimeComponent).where(WorkTimeComponent.revision_ref == current_ref),
            "append-only",
        ),
        (
            WorkTimeComponent.__table__.insert().values(
                revision_ref=current_ref, ordinal=24, source_revision_ref=source_ref
            ),
            "sealed",
        ),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert message in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL allowed rewriting sealed time composition")
    packet = await call("docket_get_work_context", {"refs": [record["ref"]]})
    split = await call(
        "docket_split_work_session",
        {
            "context_id": packet["context_id"],
            "operation_id": "pg-composite-split",
            "work_ref": record["ref"],
            "parts": [{"source_work_ref": ref} for ref in refs],
        },
    )
    assert split.get("disposition") == "committed", split
    restored = [r for r in split["work_records"] if not r["superseded"]]
    assert len(restored) == 4
    assert sum(r.get("duration_us", 0) for r in restored) == 180 * minute


async def check():
    from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
    from docket.config import get_settings
    from docket.database import configure_database, get_session_factory
    from docket.models import (
        AuthenticatedRequest,
        CompensationEvaluation,
        CompensationPolicy,
        CompensationPolicyRevision,
        CompensationRevisionTerm,
        CompensationTerm,
        DocumentAllocation,
        DocumentAllocationEvent,
        Engagement,
        EngagementRevision,
        Expense,
        ExpenseObservation,
        ExpenseReportMember,
        ExpenseReportSnapshot,
        ExpenseRevision,
        FinancialDocument,
        FinancialDocumentObservation,
        FinancialDocumentRevision,
        InvoiceNumber,
        InvoiceNumberSequence,
        WorkClaim,
        WorkClaimEvent,
        WorkLineageEdge,
        WorkQuantityAllocation,
        WorkQuantityBasis,
        WorkQuantityOrigin,
        WorkReportMember,
        WorkReportSnapshot,
        WorkSegment,
        WorkSession,
        WorkSessionRevision,
        WorkTimeComponent,
    )
    from docket.plugin.server import PluginMCP

    settings = get_settings()
    url = make_url(settings.database_url)
    assert settings.environment.value == "test" and not settings.external_writes_enabled
    assert url.get_backend_name() == "postgresql"
    assert url.database in {"docket_work_test", "docket_work_smoke"}
    assert url.host in {"127.0.0.1", "localhost", "postgres"}
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    engine = configure_database(settings.database_url)
    factory = get_session_factory()
    from docket.models import (
        FinancialEntry,
        FinancialReportSnapshot,
        FinancialTransaction,
        OperationalReminder,
        Payment,
        PaymentEvent,
        WorkloadReconciliation,
    )

    tables = (
        OperationalReminder,
        WorkloadReconciliation,
        Payment,
        PaymentEvent,
        FinancialTransaction,
        FinancialEntry,
        FinancialReportSnapshot,
        Expense,
        ExpenseRevision,
        ExpenseObservation,
        ExpenseReportSnapshot,
        ExpenseReportMember,
        DocumentAllocation,
        DocumentAllocationEvent,
        InvoiceNumber,
        InvoiceNumberSequence,
        FinancialDocument,
        FinancialDocumentRevision,
        FinancialDocumentObservation,
        WorkClaim,
        WorkClaimEvent,
        CompensationEvaluation,
        CompensationPolicy,
        CompensationPolicyRevision,
        CompensationRevisionTerm,
        CompensationTerm,
        Engagement,
        EngagementRevision,
        WorkSession,
        WorkSessionRevision,
        WorkSegment,
        WorkReportSnapshot,
        WorkReportMember,
        WorkQuantityOrigin,
        WorkQuantityBasis,
        WorkQuantityAllocation,
        WorkLineageEdge,
        WorkTimeComponent,
    )
    for model in tables:
        columns = {
            column["name"]: column for column in inspect(engine).get_columns(model.__tablename__)
        }
        assert set(columns) == set(model.__table__.columns.keys())
        for column in model.__table__.columns:
            assert columns[column.name]["nullable"] == column.nullable
            assert columns[column.name]["type"]._type_affinity == column.type._type_affinity
        with engine.connect() as connection:
            assert not connection.execute(select(model).limit(1)).first()
    command.downgrade(config, "20261104e3f4")
    assert all(model.__tablename__ not in inspect(engine).get_table_names() for model in tables)
    command.upgrade(config, "head")

    principal = AgentPrincipal(
        "synthetic-work-storage",
        settings.canonical_operator_ref,
        "interactive",
        frozenset({"read", "stage", "commit", "resolve_conflict"}),
    )
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        server = PluginMCP(settings)
        overview = (await server.call_tool("docket_get_overview", {})).structured_content
        created = (
            await server.call_tool(
                "docket_create_item",
                {
                    "context_id": overview["context_id"],
                    "operation_id": "work-storage-fixture",
                    "title": "Synthetic storage fixture",
                    "kind": "test.work_storage",
                },
            )
        ).structured_content
        assert created.get("disposition") == "committed", created
        packet = (await server.call_tool("docket_get_work_context", {})).structured_content
        logged_arguments = {
            "context_id": packet["context_id"],
            "operation_id": "canonical-work-fixture",
            "description": "Synthetic performed work",
            "duration_minutes": 150,
            "work_date": "2026-11-05",
            "reported_compensation_class": "billable",
            "attribution_label": "Synthetic unconfigured arrangement",
        }
        logged = (
            await server.call_tool("docket_log_work_session", logged_arguments)
        ).structured_content
        assert logged.get("disposition") == "committed", logged
        replay = (
            await server.call_tool("docket_log_work_session", logged_arguments)
        ).structured_content
        assert replay["replayed"] and replay["changeset_ref"] == logged["changeset_ref"]
        with factory() as session:
            canonical_ref = session.scalar(select(WorkSession.ref_id))
        packet = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [canonical_ref],
                },
            )
        ).structured_content
        assert packet["objects"][0]["state"]["duration_us"] == 9_000_000_000
        previews = []
        for index in (1, 2):
            proposal = (
                await server.call_tool(
                    "docket_update_work_session",
                    {
                        "context_id": packet["context_id"],
                        "operation_id": f"racing-work-{index}",
                        "work_ref": canonical_ref,
                        "outcome": f"Synthetic correction {index}",
                        "preview": True,
                    },
                )
            ).structured_content
            assert proposal.get("change_id"), proposal
            previews.append(proposal["change_id"])
        report = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "date_from": "2026-11-02",
                    "date_to": "2026-11-09",
                    "limit": 1,
                },
            )
        ).structured_content
        assert report.get("ok"), report
        assert report["totals"]["known_reported_us"] == 9_000_000_000
        report_cursor = report["report_cursor"]
        from docket.models import Entity

        with factory.begin() as session:
            organization = Entity(
                entity_kind="organization",
                display_name="Synthetic report employer",
                normalized_name="synthetic report employer",
                created_by_changeset_ref=logged["changeset_ref"],
                basis_refs=[],
            )
            project = Entity(
                entity_kind="project",
                display_name="Synthetic report project",
                normalized_name="synthetic report project",
                created_by_changeset_ref=logged["changeset_ref"],
                basis_refs=[],
            )
            session.add_all([organization, project])
            session.flush()
            org_ref, project_ref = organization.ref_id, project.ref_id
        choices = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [org_ref, project_ref],
                },
            )
        ).structured_content
        arrangement = (
            await server.call_tool(
                "docket_create_engagement",
                {
                    "context_id": choices["context_id"],
                    "operation_id": "report-employer-fixture",
                    "label": "Synthetic report employer",
                    "organization_ref": org_ref,
                },
            )
        ).structured_content
        assert arrangement.get("disposition") == "committed", arrangement
        with factory() as session:
            eng_ref = session.scalar(select(Engagement.ref_id))
        choices = (
            await server.call_tool(
                "docket_get_work_context",
                {
                    "refs": [eng_ref, project_ref],
                },
            )
        ).structured_content
        intervals = [
            {
                "start": "2026-11-01T01:30:00-07:00",
                "end": "2026-11-01T01:15:00-08:00",
                "breaks": [
                    {"start": "2026-11-01T01:50:00-07:00", "end": "2026-11-01T01:00:00-08:00"}
                ],
            },
            {"start": "2026-11-01T01:40:00-07:00", "end": "2026-11-01T01:10:00-08:00"},
            {"start": "2026-11-01T23:30:00-08:00", "end": "2026-11-02T00:30:00-08:00"},
        ]
        for index, fields in enumerate(intervals):
            result = (
                await server.call_tool(
                    "docket_log_work_session",
                    {
                        "context_id": choices["context_id"],
                        "operation_id": f"report-interval-{index}",
                        "description": f"Synthetic reporting interval {index}",
                        "engagement_ref": eng_ref,
                        "project_ref": project_ref,
                        "timezone": "America/Los_Angeles",
                        **fields,
                    },
                )
            ).structured_content
            assert result.get("disposition") == "committed", result
        for group in ("day", "week", "organization", "project"):
            result = (
                await server.call_tool(
                    "docket_query_work_sessions",
                    {
                        "date_from": "2020-01-01",
                        "date_to": "2030-01-01",
                        "group_by": group,
                        "timezone": "America/Los_Angeles",
                        "organization_ref": org_ref,
                    },
                )
            ).structured_content
            assert result.get("ok"), result
            assert result["totals"]["timed_us"] == 125 * 60_000_000, result
            assert result["totals"]["unique_timed_us"] == 105 * 60_000_000, result
            assert result["totals"]["overlap_us"] == 20 * 60_000_000
            if group == "project":
                assert result["groups"][0]["bucket"] == project_ref
            if group == "organization":
                assert result["groups"][0]["bucket"] == org_ref
        retained_report = result
        zero = (
            await server.call_tool(
                "docket_log_work_session",
                {
                    "context_id": choices["context_id"],
                    "operation_id": "report-break-only-window",
                    "description": "Synthetic active time outside the selected break day",
                    "engagement_ref": eng_ref,
                    "start": "2026-10-30T23:00:00-07:00",
                    "end": "2026-11-02T01:00:00-08:00",
                    "breaks": [
                        {
                            "start": "2026-10-31T00:00:00-07:00",
                            "end": "2026-11-02T00:00:00-08:00",
                        }
                    ],
                },
            )
        ).structured_content
        assert zero.get("disposition") == "committed", zero
        zero = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "engagement_ref": eng_ref,
                    "date_from": "2026-10-31",
                    "date_to": "2026-11-01",
                    "group_by": "day",
                    "timezone": "America/Los_Angeles",
                },
            )
        ).structured_content
        assert zero.get("ok"), zero
        assert zero["totals"]["session_count"] == 1 and zero["totals"]["known_reported_us"] == 0
        assert zero["groups"][0]["bucket"] == "no_active_time"
        for export_format in ("json", "csv"):
            chunks = []
            arguments = {
                "cursor": retained_report["report_cursor"],
                "format": export_format,
                "limit": 1,
            }
            for _ in range(30):
                page = (
                    await server.call_tool("docket_query_work_sessions", arguments)
                ).structured_content
                assert page.get("ok") and page["totals"] == retained_report["totals"], page
                assert (
                    len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
                    <= 16384
                )
                chunks.append(page["content"])
                if not page.get("next_cursor"):
                    break
                arguments = {"cursor": page["next_cursor"], "limit": 1}
            else:
                raise AssertionError("PostgreSQL work export did not terminate")
            if export_format == "json":
                document = json.loads("".join(chunks))
                assert len(document["entries"]) == 3
            else:
                import csv
                import io

                rows = list(csv.DictReader(io.StringIO("".join(chunks))))
                assert len([row for row in rows if row["record_type"] == "work"]) == 3
    finally:
        agent_call_context.reset(context_token)

    correction_barrier = Barrier(2)

    def apply_correction(change_id):
        async def invoke():
            token = agent_call_context.set(AgentCallContext(principal))
            try:
                endpoint = PluginMCP(settings)
                correction_barrier.wait(timeout=10)
                return (
                    await endpoint.call_tool(
                        "docket_apply_change",
                        {
                            "change_id": change_id,
                        },
                    )
                ).structured_content
            finally:
                agent_call_context.reset(token)

        return asyncio.run(invoke())

    with ThreadPoolExecutor(max_workers=2) as pool:
        corrections = list(pool.map(apply_correction, previews))
    assert sum(result.get("disposition") == "committed" for result in corrections) == 1, corrections
    assert sum(result.get("ok") is False for result in corrections) == 1, corrections
    rejected = next(result for result in corrections if result.get("ok") is False)
    assert rejected["error"]["code"] in {
        "work_version_conflict",
        "changeset_validation_failed",
    }, rejected
    with factory() as session:
        head = session.scalar(select(WorkSession).where(WorkSession.ref_id == canonical_ref))
        assert head.version == 2
        revisions = list(
            session.scalars(
                select(WorkSessionRevision)
                .where(
                    WorkSessionRevision.work_ref == canonical_ref,
                )
                .order_by(WorkSessionRevision.revision)
            )
        )
        assert len(revisions) == 2
        assert revisions[0].outcome is None and revisions[1].outcome
        assert all(row.duration_us == 9_000_000_000 for row in revisions)
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        old = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "cursor": report_cursor,
                },
            )
        ).structured_content
        assert old["totals"] == report["totals"] and old["items"][0].get("outcome") is None
    finally:
        agent_call_context.reset(context_token)
    with factory() as session:
        snapshot = session.scalar(select(WorkReportSnapshot))
        member = session.scalar(
            select(WorkReportMember).where(WorkReportMember.report_id == snapshot.id)
        )
        snapshot_id, member_ref, revision_ref = snapshot.id, member.work_ref, member.revision_ref
    for statement in (
        update(WorkReportSnapshot)
        .where(WorkReportSnapshot.id == snapshot_id)
        .values(sealed_at=None),
        delete(WorkReportSnapshot).where(WorkReportSnapshot.id == snapshot_id),
        update(WorkReportMember)
        .where(WorkReportMember.report_id == snapshot_id)
        .values(revision_ref=revision_ref),
        delete(WorkReportMember).where(WorkReportMember.report_id == snapshot_id),
    ):
        try:
            with factory.begin() as session:
                session.execute(statement)
        except DBAPIError as exc:
            assert "append-only" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL modified a retained report basis")
    try:
        with factory.begin() as session:
            session.execute(
                WorkReportMember.__table__.insert().values(
                    report_id=snapshot_id,
                    work_ref=member_ref,
                    revision_ref=revision_ref,
                )
            )
    except DBAPIError as exc:
        assert "open owned snapshot" in str(exc.orig)
    else:
        raise AssertionError("PostgreSQL appended to a sealed work report")
    await check_native_timers(settings, factory, principal)
    await check_native_lineage(settings, factory, principal)
    await check_compensation_policies(settings, factory, principal)
    await check_financial_documents(settings, factory, principal)
    await check_expenses(settings, factory, principal)
    await check_document_allocations(settings, factory, principal)
    await check_native_invoices(settings, factory, principal)
    from settlement_postgres_check import check_settlement

    settlement_evidence = await check_settlement(settings, factory, principal)
    from workload_postgres_check import check_workload

    workload_evidence = await check_workload(settings, factory, principal)
    from followup_postgres_check import check_followup

    followup_evidence = await check_followup(settings, factory, principal)
    changeset_ref = created["changeset_ref"]
    with factory.begin() as session:
        request_ref = session.scalar(
            select(AuthenticatedRequest.ref_id).where(
                AuthenticatedRequest.committed_changeset_ref == changeset_ref,
            )
        )
        assert request_ref
        engagement = Engagement(
            operator_ref=principal.operator_ref, created_by_changeset_ref=changeset_ref
        )
        work = WorkSession(
            operator_ref=principal.operator_ref,
            lifecycle="recorded",
            created_by_changeset_ref=changeset_ref,
        )
        session.add_all([engagement, work])
        session.flush()
        terms = EngagementRevision(
            engagement_ref=engagement.ref_id,
            revision=1,
            label="Synthetic engagement",
            kind="unknown",
            basis_refs=[request_ref],
            created_by_changeset_ref=changeset_ref,
        )
        revision = WorkSessionRevision(
            work_ref=work.ref_id,
            revision=1,
            lifecycle="recorded",
            superseded=False,
            description="Synthetic work",
            time_precision="interval",
            duration_us=9_000_000_000,
            start_at=datetime(2026, 11, 5, 18, tzinfo=UTC),
            end_at=datetime(2026, 11, 5, 20, 30, tzinfo=UTC),
            start_offset_seconds=-28800,
            end_offset_seconds=-28800,
            timezone="America/Los_Angeles",
            work_date=date(2026, 11, 5),
            engagement_ref=engagement.ref_id,
            reported_compensation_class="billable",
            source_method="report",
            basis_refs=[request_ref],
            source_refs=[],
            created_by_changeset_ref=changeset_ref,
        )
        session.add_all([terms, revision])
        session.flush()
        segment = WorkSegment(
            revision_id=revision.id,
            ordinal=0,
            kind="active",
            start_at=revision.start_at,
            end_at=revision.end_at,
            start_offset_seconds=-28800,
            end_offset_seconds=-28800,
        )
        session.add(segment)
        session.flush()
        retained = [
            (EngagementRevision, terms.id),
            (WorkSessionRevision, revision.id),
            (WorkSegment, segment.id),
        ]
        heads = [(Engagement, engagement.id), (WorkSession, work.id)]
    for model, identity in retained:
        # Core SQL intentionally bypasses the ORM guards to prove PostgreSQL protection.
        for statement in (
            update(model).where(model.id == identity).values(id=identity),
            delete(model).where(model.id == identity),
        ):
            try:
                with factory.begin() as session:
                    session.execute(statement)
            except DBAPIError as exc:
                assert "append-only" in str(exc.orig)
            else:
                raise AssertionError("PostgreSQL allowed immutable work history to change")
    for already_sealed in (True, False):
        try:
            with factory.begin() as session:
                session.add(
                    WorkQuantityBasis(
                        revision_ref=revision.ref_id,
                        status="resolved",
                        sealed=already_sealed,
                        created_by_changeset_ref=changeset_ref,
                    )
                )
                session.flush()
                session.execute(
                    update(WorkQuantityBasis)
                    .where(
                        WorkQuantityBasis.revision_ref == revision.ref_id,
                    )
                    .values(sealed=True)
                )
        except DBAPIError as exc:
            expected = (
                "assembled before sealing" if already_sealed else "does not match its revision"
            )
            assert expected in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL sealed an incomplete quantity basis")
    for model, identity in heads:
        try:
            with factory.begin() as session:
                session.execute(delete(model).where(model.id == identity))
        except DBAPIError as exc:
            assert "append-only" in str(exc.orig)
        else:
            raise AssertionError("PostgreSQL deleted a retained work head")

    timer_barrier = Barrier(2)

    def start_timer(_index):
        try:
            with factory.begin() as session:
                session.connection()
                timer_barrier.wait(timeout=10)
                timer = WorkSession(
                    operator_ref=principal.operator_ref,
                    lifecycle="running",
                    created_by_changeset_ref=changeset_ref,
                )
                session.add(timer)
                session.flush()
                revision = WorkSessionRevision(
                    work_ref=timer.ref_id,
                    revision=1,
                    lifecycle="running",
                    superseded=False,
                    description="Synthetic timer",
                    time_precision="timer",
                    duration_us=0,
                    start_at=datetime.now(UTC),
                    start_offset_seconds=0,
                    timezone="America/Los_Angeles",
                    source_method="timer",
                    basis_refs=[request_ref],
                    source_refs=[],
                    created_by_changeset_ref=changeset_ref,
                )
                session.add(revision)
                session.flush()
                session.add(
                    WorkSegment(
                        revision_id=revision.id,
                        ordinal=0,
                        kind="active",
                        start_at=revision.start_at,
                        start_offset_seconds=0,
                    )
                )
            return "created"
        except IntegrityError as exc:
            assert "uq_work_primary_timer" in str(exc.orig)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start_timer, (1, 2))) == ["conflict", "created"]
    context_token = agent_call_context.set(AgentCallContext(principal))
    try:
        estimated = (
            await server.call_tool(
                "docket_query_work_sessions",
                {
                    "date_from": "2020-01-01",
                    "date_to": "2030-01-01",
                },
            )
        ).structured_content
        assert estimated.get("ok"), estimated
        assert estimated["totals"]["open_timer_count"] == 1
        assert estimated["totals"]["estimate_us"] > 0
    finally:
        agent_call_context.reset(context_token)
    try:
        command.downgrade(config, "20261104e3f4")
    except RuntimeError as exc:
        assert "cannot discard retained" in str(exc)
    else:
        raise AssertionError("Downgrade discarded populated work history")
    engine.dispose()
    print(
        json.dumps(
            {
                **settlement_evidence,
                **workload_evidence,
                **followup_evidence,
                "work_schema_roundtrip": "passed",
                "canonical_work_retry": "passed",
                "canonical_correction_race": "passed",
                "native_timer_start_and_switch_races": "passed",
                "timer_fresh_process_survival": "passed",
                "native_lineage_and_concurrent_quantity_growth": "passed",
                "quantity_and_lineage_immutability": "passed",
                "effective_compensation_policies_and_concurrency": "passed",
                "frozen_compensation_evaluation_and_owner_isolation": "passed",
                "timesheet_claim_race_and_conserved_amendment": "passed",
                "expense_import_race_exact_totals_and_frozen_approval": "passed",
                "expense_owner_and_report_immutability": "passed",
                "partial_invoice_allocation_race_and_numbering": "passed",
                "native_invoice_preview_race_refresh_issue_delivery_and_lineage": "passed",
                "compensation_owner_isolation_and_privacy": "passed",
                "composite_time_precision_and_sealing": "passed",
                "work_report_aggregates_and_exports": "passed",
                "retained_report_membership": "sealed",
                "revision_immutability": "passed",
                "single_primary_timer_race": "passed",
                "retained_downgrade": "refused",
            }
        )
    )


if __name__ == "__main__":
    # Change directory before importing Docket so even import-time settings cannot
    # read the workspace .env; only explicit synthetic validation settings apply.
    with TemporaryDirectory(prefix="docket-work-storage-") as directory:
        os.chdir(directory)
        asyncio.run(check())
