"""Map conserved claims to document purposes; source units and quotes never change."""

from fractions import Fraction
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.errors import DocketError
from docket.domain.financial_pricing import expense_units, price_quote, remaining_ranges
from docket.domain.public_refs import new_public_ref
from docket.models import (
    DocumentAllocation,
    DocumentAllocationEvent,
    Expense,
    ExpenseRevision,
    FinancialDocument,
    FinancialDocumentRevision,
    InvoiceNumber,
    InvoiceNumberSequence,
    WorkClaim,
    WorkQuantityOrigin,
)


class DocumentAllocationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def document(self, ref: str) -> FinancialDocument:
        row = self.session.scalar(
            select(FinancialDocument).where(
                FinancialDocument.ref_id == ref,
                FinancialDocument.operator_ref == require_principal("read").operator_ref,
            )
        )
        if row is None or row.kind != "invoice":
            raise DocketError(code="invoice_unavailable", message="Select an owned invoice.")
        return row

    def active(self, key: str) -> list[DocumentAllocation]:
        return list(
            self.session.scalars(
                select(DocumentAllocation)
                .where(
                    DocumentAllocation.operator_ref == require_principal("read").operator_ref,
                    DocumentAllocation.source_key == key,
                    DocumentAllocation.status != "released",
                )
                .order_by(DocumentAllocation.ref_id)
                .execution_options(populate_existing=True)
            )
        )

    def source(
        self, allocation: DocumentAllocation, pending_claim: WorkClaim | None = None
    ) -> tuple[int, int]:
        document = self.document(allocation.document_ref)
        if allocation.work_claim_ref:
            claim = pending_claim or self.session.scalar(
                select(WorkClaim).where(
                    WorkClaim.ref_id == allocation.work_claim_ref,
                    WorkClaim.operator_ref == document.operator_ref,
                )
            )
            if (
                claim is None
                or claim.status == "released"
                or (claim.status == "reserved" and claim.document_ref != document.ref_id)
            ):
                raise DocketError(
                    code="work_claim_unavailable", message="Use retained available work claims."
                )
            self.session.scalar(
                select(WorkQuantityOrigin)
                .where(WorkQuantityOrigin.ref_id == claim.origin_ref)
                .with_for_update()
            )
            if pending_claim is None:
                claim = self.session.scalar(
                    select(WorkClaim)
                    .where(
                        WorkClaim.ref_id == claim.ref_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                assert claim is not None
            if (
                claim.ref_id != allocation.work_claim_ref
                or claim.operator_ref != document.operator_ref
                or claim.engagement_ref != document.engagement_ref
                or claim.component_key != document.component_key
                or claim.status == "released"
                or (claim.status == "reserved" and claim.document_ref != document.ref_id)
            ):
                raise DocketError(code="work_claim_unavailable", message="Use owned retained work.")
            expected = f"work:{claim.origin_ref}:{claim.component_key}"
            lo, hi = claim.start_us, claim.end_us
        else:
            expense = self.session.scalar(
                select(Expense)
                .where(
                    Expense.ref_id == allocation.expense_ref,
                    Expense.operator_ref == document.operator_ref,
                )
                .with_for_update()
            )
            revision = self.session.scalar(
                select(ExpenseRevision).where(
                    ExpenseRevision.ref_id == allocation.expense_revision_ref,
                    ExpenseRevision.expense_ref == allocation.expense_ref,
                )
            )
            if (
                expense is None
                or revision is None
                or expense.retracted
                or expense.version != revision.revision
            ):
                raise DocketError(
                    code="expense_claim_basis_changed", message="Refresh from the current expense."
                )
            if revision.engagement_ref not in {None, document.engagement_ref}:
                raise DocketError(
                    code="expense_claim_attribution_conflict",
                    message="Use the selected engagement's expense.",
                )
            if allocation.currency != revision.currency:
                raise DocketError(
                    code="expense_claim_currency_changed",
                    message="Use the retained expense currency.",
                )
            expected = "expense:" + expense.ref_id
            lo = 0
            hi = expense_units(revision.reimbursable_amount or revision.amount)
        if allocation.source_key != expected:
            raise DocketError(
                code="invalid_document_allocation",
                message="Allocation identity must match its retained source.",
            )
        return lo, hi

    def allocate(
        self,
        document_ref: str,
        *,
        work_claim_ref: str | None = None,
        expense_ref: str | None = None,
        expense_revision_ref: str | None = None,
        source_key: str,
        start: int,
        end: int,
        currency: str | None,
        quote: dict[str, Any],
        changeset_ref: str,
        decimals: int = 2,
        maximum_units: int | None = None,
        pending_claim: WorkClaim | None = None,
        replacing: bool = False,
    ) -> list[DocumentAllocation]:
        """Return new rows only; caller inserts them after their immutable draft revision."""
        require_principal("commit")
        document = self.document(document_ref)
        if document.status != "draft":
            raise DocketError(
                code="invoice_snapshot_final",
                message="Issued snapshots cannot reserve more source units.",
            )
        template = DocumentAllocation(
            document_ref=document.ref_id,
            operator_ref=document.operator_ref,
            work_claim_ref=work_claim_ref,
            expense_ref=expense_ref,
            expense_revision_ref=expense_revision_ref,
            source_key=source_key,
            currency=currency,
            quote_json=quote,
            money_decimals=decimals,
            status="reserved",
            version=1,
            created_by_changeset_ref=changeset_ref,
        )
        lo, hi = self.source(template, pending_claim)
        if quote.get("kind") not in {"work", "expense"} or (
            (quote.get("kind") == "work") != (work_claim_ref is not None)
        ):
            raise ValueError("Allocation quote must match its source kind")
        if not lo <= start < end <= hi:
            raise DocketError(
                code="invalid_document_allocation",
                message="Allocation exceeds its retained source quantity.",
            )
        occupied = self.active(source_key)
        if expense_ref and any(row.currency != currency for row in occupied):
            raise DocketError(
                code="expense_claim_currency_changed",
                message="Reconcile the earlier claim before changing its currency.",
            )
        if replacing:
            occupied = [
                row
                for row in occupied
                if not (row.document_ref == document_ref and row.status == "reserved")
            ]
        foreign_drafts = [
            row.document_ref
            for row in occupied
            if row.status == "reserved"
            and row.document_ref != document.ref_id
            and int(row.start_units) < end
            and int(row.end_units) > start
        ]
        if foreign_drafts:
            raise DocketError(
                code="invoice_quantity_reserved",
                message="Read or cancel the other draft before claiming these units.",
                details={"document_refs": sorted(set(foreign_drafts))[:5]},
            )
        # A draft refresh releases its old allocations first in the same transaction.
        available = remaining_ranges(
            start, end, [(int(row.start_units), int(row.end_units)) for row in occupied]
        )
        result = []
        for first, last in available:
            if maximum_units is not None:
                last = min(last, first + maximum_units)
                if last <= first:
                    break
            amount = price_quote(quote, first, last)
            row = DocumentAllocation(
                ref_id=new_public_ref("alloc"),
                document_ref=document.ref_id,
                operator_ref=document.operator_ref,
                work_claim_ref=work_claim_ref,
                expense_ref=expense_ref,
                expense_revision_ref=expense_revision_ref,
                source_key=source_key,
                start_units=str(first),
                end_units=str(last),
                currency=currency,
                money_decimals=decimals,
                quote_json=quote,
                gross_numerator=str(amount.numerator) if amount is not None else None,
                gross_denominator=str(amount.denominator) if amount is not None else None,
                status="reserved",
                version=1,
                created_by_changeset_ref=changeset_ref,
            )
            result.append(row)
            if maximum_units is not None:
                maximum_units -= last - first
        return result

    def event(self, row: DocumentAllocation, revision: FinancialDocumentRevision) -> None:
        if row.document_ref != revision.document_ref:
            raise ValueError("Allocation transition belongs to a different document")
        self.session.add(
            DocumentAllocationEvent(
                allocation_ref=row.ref_id,
                version=row.version,
                status=row.status,
                document_revision_ref=revision.ref_id,
                created_by_changeset_ref=revision.created_by_changeset_ref,
            )
        )

    def persist(self, rows: list[DocumentAllocation], revision: FinancialDocumentRevision) -> None:
        require_principal("commit")
        for row in rows:
            self.session.add(row)
            self.session.flush()
            self.event(row, revision)
        self.session.flush()

    def transition(
        self, document_ref: str, status: str, revision: FinancialDocumentRevision
    ) -> None:
        require_principal("commit")
        self.document(document_ref)
        if status not in {"issued", "released"}:
            raise ValueError("Allocation transition must issue or release its reserved units")
        rows = self.session.scalars(
            select(DocumentAllocation)
            .where(
                DocumentAllocation.document_ref == document_ref,
                DocumentAllocation.status == "reserved",
            )
            .order_by(DocumentAllocation.source_key, DocumentAllocation.ref_id)
            .with_for_update()
        ).all()
        for row in rows:
            row.status, row.version = status, row.version + 1
        self.session.flush()
        for row in rows:
            self.event(row, revision)
        self.session.flush()

    def number(self, revision: FinancialDocumentRevision, series: str) -> InvoiceNumber:
        require_principal("commit")
        document = self.document(revision.document_ref)
        if (
            revision.status != "issued"
            or document.status != "issued"
            or document.version != revision.revision
        ):
            raise ValueError("Invoice numbering requires the exact issued revision")
        owner = document.operator_ref
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "invoice-number:" + owner + ":" + series},
            )
        existing = self.session.get(InvoiceNumber, document.ref_id)
        if existing:
            return existing
        sequence = self.session.scalar(
            select(InvoiceNumberSequence)
            .where(
                InvoiceNumberSequence.operator_ref == owner,
                InvoiceNumberSequence.series == series,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if sequence is None:
            sequence = InvoiceNumberSequence(operator_ref=owner, series=series, next_ordinal=1)
            self.session.add(sequence)
            self.session.flush()
        ordinal = sequence.next_ordinal
        sequence.next_ordinal += 1
        row = InvoiceNumber(
            document_ref=document.ref_id,
            operator_ref=owner,
            series=series,
            ordinal=ordinal,
            number=f"{series}-{ordinal:06}",
            issued_revision_ref=revision.ref_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    @staticmethod
    def amount(row: DocumentAllocation) -> Fraction | None:
        return (
            Fraction(int(row.gross_numerator), int(row.gross_denominator))
            if (row.gross_numerator is not None and row.gross_denominator is not None)
            else None
        )
