"""Document allocations reuse shared claims and preserve partial source quantities."""

from fractions import Fraction

import pytest
import test_native_plugin
from sqlalchemy import func, select
from test_work_lineage import change
from test_work_reports import work_client

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    DocumentAllocation,
    DocumentAllocationEvent,
    FinancialDocument,
    FinancialDocumentRevision,
    WorkClaim,
)
from docket.services.document_allocations import DocumentAllocationService

linked_plugin = test_native_plugin.linked_plugin


def draft(session, source):
    revision = session.scalar(
        select(FinancialDocumentRevision)
        .where(FinancialDocumentRevision.document_ref == source.document_ref)
        .order_by(FinancialDocumentRevision.revision.desc())
    )
    head = session.scalar(
        select(FinancialDocument).where(FinancialDocument.ref_id == source.document_ref)
    )
    doc = FinancialDocument(
        ref_id=new_public_ref("fdoc"),
        operator_ref=head.operator_ref,
        kind="invoice",
        engagement_ref=head.engagement_ref,
        component_key=head.component_key,
        version=1,
        status="draft",
        created_by_changeset_ref=revision.created_by_changeset_ref,
    )
    session.add(doc)
    session.flush()
    return doc, revision


def retained_revision(session, doc, source, refs, status="draft"):
    revision = FinancialDocumentRevision(
        document_ref=doc.ref_id,
        revision=doc.version,
        status=status,
        evaluation_ref=source.evaluation_ref,
        date_from=source.date_from,
        date_to=source.date_to,
        timezone=source.timezone,
        snapshot_json={**source.snapshot_json, "allocation_refs": refs},
        created_by_changeset_ref=source.created_by_changeset_ref,
        basis_refs=source.basis_refs,
    )
    session.add(revision)
    session.flush()
    return revision


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_plugin", [{"write": True}], indirect=True)
async def test_document_allocations_share_claims_release_reservations_and_keep_exact_prices(
    session_factory, linked_plugin
):
    async with work_client(linked_plugin) as call:
        eng = (
            await change(
                call, "docket_create_engagement", [], "allocation-eng", label="Synthetic client"
            )
        )["affected_refs"][0]
        await change(
            call,
            "docket_log_work_session",
            [eng],
            "allocation-work",
            description="Synthetic billable work",
            duration_minutes=120,
            work_date="2026-11-03",
            engagement_ref=eng,
            reported_compensation_class="billable",
        )
        report = await change(
            call,
            "docket_prepare_timesheet",
            [eng],
            "allocation-sheet",
            engagement_ref=eng,
            date_from="2026-11-02",
            date_to="2026-11-09",
        )
        sheet = report["documents"][0]["ref"]
        await change(
            call,
            "docket_update_timesheet",
            [sheet],
            "allocation-submitted",
            timesheet_ref=sheet,
            status="submitted",
        )
    _, settings, _, _ = linked_plugin
    actor = AgentPrincipal(
        "synthetic-allocation",
        settings.canonical_operator_ref,
        "interactive",
        frozenset({"read", "commit"}),
    )
    token = agent_call_context.set(AgentCallContext(actor))
    try:
        with session_factory.begin() as session:
            claim = session.scalar(select(WorkClaim))
            doc, original = draft(session, claim)
            source_key = f"work:{claim.origin_ref}:{claim.component_key}"
            quote = {
                "kind": "work",
                "source_start": claim.start_us,
                "line_offset_us": 0,
                "basis": {
                    "raw_us": 120 * 60_000_000,
                    "reportable_us": 120 * 60_000_000,
                    "overtime_us": 60 * 60_000_000,
                    "rate": "80",
                    "overtime_multiplier": "1.5",
                },
            }
            service = DocumentAllocationService(session)
            first = service.allocate(
                doc.ref_id,
                work_claim_ref=claim.ref_id,
                source_key=source_key,
                start=claim.start_us,
                end=claim.end_us,
                currency="USD",
                quote=quote,
                changeset_ref=original.created_by_changeset_ref,
                maximum_units=60 * 60_000_000,
            )
            assert len(first) == 1 and service.amount(first[0]) == 80
            revision = retained_revision(session, doc, original, [row.ref_id for row in first])
            service.persist(first, revision)
            doc_ref, source_ref = doc.ref_id, claim.ref_id
        with session_factory.begin() as session:
            service = DocumentAllocationService(session)
            claim = session.get(WorkClaim, session.scalar(select(WorkClaim.id)))
            other, original = draft(session, claim)
            with pytest.raises(DocketError) as caught:
                service.allocate(
                    other.ref_id,
                    work_claim_ref=source_ref,
                    source_key=source_key,
                    start=claim.start_us,
                    end=claim.end_us,
                    currency="USD",
                    quote=quote,
                    changeset_ref=original.created_by_changeset_ref,
                )
            assert caught.value.code == "invoice_quantity_reserved"
            # Discard only this test transaction's unused draft, rather than a persisted record.
            session.rollback()
        with session_factory.begin() as session:
            doc = session.scalar(
                select(FinancialDocument).where(FinancialDocument.ref_id == doc_ref)
            )
            prior = session.scalar(
                select(FinancialDocumentRevision).where(
                    FinancialDocumentRevision.document_ref == doc_ref
                )
            )
            doc.version += 1
            doc.status = "issued"
            session.flush()
            revision = retained_revision(
                session, doc, prior, prior.snapshot_json["allocation_refs"], "issued"
            )
            service = DocumentAllocationService(session)
            service.transition(doc.ref_id, "issued", revision)
            assert service.number(revision, "SYNTHETIC-2026").number == "SYNTHETIC-2026-000001"
            assert service.number(revision, "SYNTHETIC-2026").number == "SYNTHETIC-2026-000001"
        with session_factory.begin() as session:
            claim = session.scalar(select(WorkClaim))
            doc, original = draft(session, claim)
            service = DocumentAllocationService(session)
            remainder = service.allocate(
                doc.ref_id,
                work_claim_ref=source_ref,
                source_key=source_key,
                start=claim.start_us,
                end=claim.end_us,
                currency="USD",
                quote=quote,
                changeset_ref=original.created_by_changeset_ref,
            )
            assert len(remainder) == 1 and service.amount(remainder[0]) == Fraction(120)
            assert remainder[0].start_units == str(60 * 60_000_000)
            revision = retained_revision(session, doc, original, [row.ref_id for row in remainder])
            service.persist(remainder, revision)
            doc.version += 1
            doc.status = "cancelled"
            session.flush()
            cancelled = retained_revision(
                session, doc, revision, revision.snapshot_json["allocation_refs"], "cancelled"
            )
            service.transition(doc.ref_id, "released", cancelled)
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkClaim)) == 1
            assert session.scalar(select(func.count()).select_from(DocumentAllocation)) == 2
            assert session.scalar(select(func.count()).select_from(DocumentAllocationEvent)) == 4
    finally:
        agent_call_context.reset(token)
