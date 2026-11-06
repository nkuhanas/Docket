"""Invoice selection over the existing claim lineage, with frozen marginal prices."""

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.compensation import money, rounded_integer
from docket.domain.errors import DocketError
from docket.domain.financial_pricing import WorkPriceBasis, expense_units, price_quote
from docket.domain.public_refs import new_public_ref
from docket.models import (
    CompensationEvaluation,
    DocumentAllocation,
    ExpenseRevision,
    FinancialDocument,
    WorkClaim,
    WorkSessionRevision,
)
from docket.schemas.invoices import InvoiceSpec
from docket.services.expenses import ExpenseService
from docket.services.work_claims import ClaimPlan, WorkClaimService
from docket.services.work_quantities import WorkSpan, slice_spans

BLOCKING = {
    "open_timer",
    "quantity_unknown",
    "unplaced_time",
    "cross_period_quantity_unallocated",
    "overlapping_work",
    "quantity_lineage_needs_review",
    "outside_engagement_dates",
    "within_report_policy_change",
    "reporting_day_timezone_unresolved",
}


@dataclass
class InvoicePlan:
    claims: ClaimPlan
    allocations: list[DocumentAllocation] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    excluded_work_refs: set[str] = field(default_factory=set)
    payer_refs: set[str] = field(default_factory=set)

    def summary(self) -> dict[str, Any]:
        groups: dict[tuple[str, int], Fraction] = {}
        unresolved = 0
        worked = 0
        for allocation in self.allocations:
            if allocation.work_claim_ref:
                worked += int(allocation.end_units) - int(allocation.start_units)
            if allocation.currency and allocation.gross_numerator is not None:
                assert allocation.gross_denominator is not None
                key = (allocation.currency, allocation.money_decimals)
                groups[key] = groups.get(key, Fraction(0)) + Fraction(
                    int(allocation.gross_numerator), int(allocation.gross_denominator)
                )
            else:
                unresolved += 1
        return {
            "worked_us": worked,
            "unknown_quantity_count": len(self.excluded_work_refs),
            "unresolved_amount_count": unresolved,
            "gross_estimates": [
                {
                    "currency": currency,
                    "money_decimals": precision,
                    "amount": money(value, precision),
                    "exact_numerator": str(value.numerator),
                    "exact_denominator": str(value.denominator),
                }
                for (currency, precision), value in sorted(groups.items())
            ],
            "flags": sorted(self.flags),
            "allocation_count": len(self.allocations),
        }


class InvoiceClaimPlanner:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.claims = WorkClaimService(session)
        self.expenses = ExpenseService(session)

    def sources(
        self,
        evaluation: CompensationEvaluation,
        parameters: Any,
        document_ref: str,
        expense_refs: list[str],
        *,
        lock: bool = True,
    ) -> tuple[dict[str, list[WorkSpan]], list[WorkClaim], list[DocumentAllocation], str]:
        principal = require_principal("commit")
        lines = evaluation.result_json["contributions"]
        selected = [
            line
            for line in lines
            if parameters.work_refs is None or line["work_ref"] in parameters.work_refs
        ]
        refs = sorted({line["revision_ref"] for line in selected})
        spans = self.claims.source_spans(refs)
        old = list(
            self.session.scalars(
                select(WorkClaim).where(
                    WorkClaim.document_ref == document_ref,
                    WorkClaim.status == "reserved",
                    WorkClaim.operator_ref == principal.operator_ref,
                )
            )
        )
        origins = sorted(
            {span.origin_ref for rows in spans.values() for span in rows}
            | {row.origin_ref for row in old}
        )
        existing = self.claims.active(
            origins, evaluation.parameters_json["component_key"], lock=lock
        )
        old_allocations = list(
            self.session.scalars(
                select(DocumentAllocation).where(
                    DocumentAllocation.document_ref == document_ref,
                    DocumentAllocation.status == "reserved",
                    DocumentAllocation.operator_ref == principal.operator_ref,
                )
            )
        )
        keys = {f"work:{ref}:{evaluation.parameters_json['component_key']}" for ref in origins}
        keys |= {"expense:" + ref for ref in expense_refs}
        keys |= {row.source_key for row in old_allocations}
        allocations = list(
            self.session.scalars(
                select(DocumentAllocation)
                .where(
                    DocumentAllocation.operator_ref == principal.operator_ref,
                    DocumentAllocation.source_key.in_(keys),
                    DocumentAllocation.status != "released",
                )
                .order_by(DocumentAllocation.ref_id)
                .execution_options(populate_existing=True)
            )
        )
        fees = []
        for ref in sorted(
            set(expense_refs) | {row.expense_ref for row in old_allocations if row.expense_ref}
        ):
            head = self.expenses.head(ref, lock=lock)
            fees.append((head.ref_id, head.version, head.retracted))
        basis = sha256_json(
            {
                "claims": sorted((row.ref_id, row.version, row.status) for row in existing),
                "allocations": [(row.ref_id, row.version, row.status) for row in allocations],
                "expenses": fees,
            }
        )
        return spans, existing, allocations, basis

    def plan(
        self,
        document: FinancialDocument,
        evaluation: CompensationEvaluation,
        spec: InvoiceSpec,
        changeset_ref: str,
    ) -> InvoicePlan:
        spans, existing, allocated, basis = self.sources(
            evaluation, spec.parameters, document.ref_id, list(spec.expense_revisions)
        )
        if basis != spec.financial_basis:
            raise DocketError(
                code="invoice_claim_basis_changed",
                message="Claims changed since preparation. Refresh this proposal.",
            )
        owner = require_principal("commit").operator_ref
        claim_plan = ClaimPlan(
            document_ref=document.ref_id,
            replaced=[
                row
                for row in existing
                if row.document_ref == document.ref_id and row.status == "reserved"
            ],
        )
        result = InvoicePlan(claims=claim_plan)
        existing = [row for row in existing if row not in claim_plan.replaced]
        allocated = [
            row
            for row in allocated
            if not (row.document_ref == document.ref_id and row.status == "reserved")
        ]
        terms = {row["term_ref"]: row["terms"] for row in evaluation.result_json["basis"]}
        remaining = None
        if spec.parameters.max_work_minutes is not None:
            value = Fraction(spec.parameters.max_work_minutes) * 60_000_000
            if value.denominator != 1 or value <= 0:
                raise DocketError(
                    code="invoice_quantity_precision",
                    message="Use whole microseconds or more.",
                    details={"field": "max_work_minutes"},
                )
            remaining = int(value)
        lines = evaluation.result_json["contributions"]
        descriptions = {
            row.ref_id: row.description
            for row in self.session.scalars(
                select(WorkSessionRevision).where(
                    WorkSessionRevision.ref_id.in_({line["revision_ref"] for line in lines})
                )
            )
        }
        for index, line in enumerate(lines):
            if (
                spec.parameters.work_refs is not None
                and line["work_ref"] not in spec.parameters.work_refs
            ):
                continue
            if spec.parameters.currency and line.get("currency") not in {
                None,
                spec.parameters.currency,
            }:
                continue
            if (
                line.get("billable") is not True
                and line.get("reported_compensation_class") != "billable"
            ):
                continue
            raw = line.get("raw_us")
            if not raw or set(line.get("flags", [])) & {
                "open_timer",
                "quantity_unknown",
                "unplaced_time",
                "cross_period_quantity_unallocated",
            }:
                result.excluded_work_refs.add(line["work_ref"])
                continue
            if remaining == 0:
                break
            term = terms.get(line.get("pay_term_ref"), {})
            rules = terms.get(line.get("reporting_term_ref"), {})
            price = WorkPriceBasis(
                raw,
                line.get("reportable_us"),
                line.get("overtime_us"),
                term.get("rate") if term.get("basis") == "hourly" else None,
                rules.get("overtime_multiplier") or "1",
            )
            offset = evaluation.result_json["retained"][index]["offset_us"]
            parts = slice_spans(spans[line["revision_ref"]], offset, offset + raw)
            line_position = 0
            for span in parts:
                key = f"work:{span.origin_ref}:{document.component_key}"
                claims = [
                    row
                    for row in existing
                    if row.origin_ref == span.origin_ref
                    and row.start_us < span.end
                    and row.end_us > span.start
                ]
                invoices = [
                    row
                    for row in allocated
                    if row.source_key == key
                    and int(row.start_units) < span.end
                    and int(row.end_units) > span.start
                ]
                boundaries = sorted(
                    {
                        span.start,
                        span.end,
                        *(max(span.start, row.start_us) for row in claims),
                        *(min(span.end, row.end_us) for row in claims),
                        *(max(span.start, int(row.start_units)) for row in invoices),
                        *(min(span.end, int(row.end_units)) for row in invoices),
                    }
                )
                for first, last in pairwise(boundaries):
                    old_invoice = next(
                        (
                            row
                            for row in invoices
                            if int(row.start_units) <= first < int(row.end_units)
                        ),
                        None,
                    )
                    if old_invoice:
                        if old_invoice.status == "reserved":
                            raise DocketError(
                                code="invoice_quantity_reserved",
                                message="Read or cancel the other draft.",
                                details={"document_ref": old_invoice.document_ref},
                            )
                        continue
                    if remaining == 0:
                        break
                    if remaining is not None:
                        last = min(last, first + remaining)
                    claim = next(
                        (row for row in claims if row.start_us <= first < row.end_us), None
                    )
                    if claim and claim.status == "reserved":
                        raise DocketError(
                            code="work_quantity_already_reserved",
                            message="Read or cancel the other draft.",
                            details={"document_ref": claim.document_ref},
                        )
                    if claim and claim.engagement_ref != document.engagement_ref:
                        raise DocketError(
                            code="claimed_work_attribution_changed",
                            message="Reconcile the earlier claim before changing its payer.",
                            details={"claim_ref": claim.ref_id},
                        )
                    first_position = line_position + first - span.start
                    last_position = line_position + last - span.start
                    amount = price.price(first_position, last_position)
                    reportable = (
                        (
                            rounded_integer(Fraction(price.reportable_us * last_position, raw))
                            - rounded_integer(Fraction(price.reportable_us * first_position, raw))
                        )
                        if price.reportable_us is not None
                        else None
                    )
                    if claim is None:
                        claim = WorkClaim(
                            ref_id=new_public_ref("clm"),
                            operator_ref=owner,
                            origin_ref=span.origin_ref,
                            component_key=document.component_key,
                            engagement_ref=document.engagement_ref,
                            start_us=first,
                            end_us=last,
                            source_revision_ref=line["revision_ref"],
                            evaluation_ref=evaluation.ref_id,
                            evaluation_line=index,
                            document_ref=document.ref_id,
                            reportable_us=reportable,
                            pay_term_ref=line.get("pay_term_ref"),
                            currency=line.get("currency"),
                            gross_numerator=str(amount.numerator) if amount is not None else None,
                            gross_denominator=str(amount.denominator)
                            if amount is not None
                            else None,
                            status="reserved",
                            version=1,
                            created_by_changeset_ref=changeset_ref,
                        )
                        claim_plan.new.append(claim)
                    else:
                        claim_plan.shared_refs.add(claim.ref_id)
                    flags = set(line.get("flags", [])) & BLOCKING
                    if span.needs_review:
                        flags.add("quantity_lineage_needs_review")
                    if line.get("billable") is not True:
                        flags.add("billability_unresolved")
                    if amount is None:
                        flags.add("monetary_basis_unresolved")
                    quote = {
                        "kind": "work",
                        "calculation_version": "invoice-allocation-v1",
                        "evaluation_ref": evaluation.ref_id,
                        "evaluation_line": index,
                        "source_start": span.start,
                        "line_offset_us": line_position,
                        "basis": asdict(price),
                        "work_ref": line["work_ref"],
                        "revision_ref": line["revision_ref"],
                        "billable": line.get("billable"),
                        "flags": sorted(flags),
                        "reportable_us": reportable,
                    }
                    allocation = self.allocation(
                        document,
                        key,
                        first,
                        last,
                        quote,
                        line.get("currency"),
                        spec.parameters.money_decimals
                        if spec.parameters.money_decimals is not None
                        else (
                            term.get("money_decimals")
                            if term.get("money_decimals") is not None
                            else 2
                        ),
                        changeset_ref,
                        work_claim_ref=claim.ref_id,
                    )
                    result.allocations.append(allocation)
                    if term.get("payer_ref"):
                        result.payer_refs.add(term["payer_ref"])
                    result.lines.append(
                        {
                            "record_type": "labor",
                            "allocation_ref": allocation.ref_id,
                            **line,
                            "raw_us": last - first,
                            "reportable_us": reportable,
                            "description": descriptions[line["revision_ref"]],
                            "quote": quote,
                            "exact_gross": {
                                "numerator": str(amount.numerator),
                                "denominator": str(amount.denominator),
                            }
                            if amount is not None
                            else None,
                            "money_decimals": allocation.money_decimals,
                            "gross_line_estimate": money(amount, allocation.money_decimals)
                            if amount is not None
                            else None,
                        }
                    )
                    result.flags |= flags
                    if remaining is not None:
                        remaining -= last - first
                line_position += span.quantity
        for ref, revision_ref in spec.expense_revisions.items():
            head = self.expenses.head(ref, lock=True)
            revision = self.expenses.revision(head)
            if revision.ref_id != revision_ref or head.retracted:
                raise DocketError(
                    code="expense_claim_basis_changed",
                    message="Refresh from the current expense.",
                    details={"expense_ref": ref},
                )
            if revision.engagement_ref not in {None, document.engagement_ref}:
                raise DocketError(
                    code="expense_claim_attribution_conflict",
                    message="Use this engagement's expense.",
                    details={"expense_ref": ref},
                )
            if spec.parameters.currency and revision.currency != spec.parameters.currency:
                continue
            self.expense(
                result, document, revision, allocated, changeset_ref, spec.parameters.money_decimals
            )
        if not result.allocations:
            result.flags.add("no_available_claims")
        if len({row.currency for row in result.allocations}) != 1:
            result.flags.add("invoice_currency_unresolved")
        if len({row.money_decimals for row in result.allocations}) > 1:
            result.flags.add("invoice_precision_unresolved")
        if len(result.payer_refs) > 1:
            result.flags.add("invoice_payer_unresolved")
        return result

    @staticmethod
    def allocation(
        document: FinancialDocument,
        key: str,
        first: int,
        last: int,
        quote: dict[str, Any],
        currency: str | None,
        decimals: int,
        changeset_ref: str,
        **source: Any,
    ) -> DocumentAllocation:
        amount = price_quote(quote, first, last)
        return DocumentAllocation(
            ref_id=new_public_ref("alloc"),
            operator_ref=document.operator_ref,
            document_ref=document.ref_id,
            source_key=key,
            start_units=str(first),
            end_units=str(last),
            quote_json=quote,
            currency=currency,
            money_decimals=decimals,
            gross_numerator=str(amount.numerator) if amount is not None else None,
            gross_denominator=str(amount.denominator) if amount is not None else None,
            status="reserved",
            version=1,
            created_by_changeset_ref=changeset_ref,
            **source,
        )

    def expense(
        self,
        result: InvoicePlan,
        document: FinancialDocument,
        revision: ExpenseRevision,
        allocated: list[DocumentAllocation],
        changeset_ref: str,
        precision: int | None,
    ) -> None:
        key = "expense:" + revision.expense_ref
        rows = [row for row in allocated if row.source_key == key]
        if any(row.status == "reserved" for row in rows):
            raise DocketError(
                code="invoice_quantity_reserved", message="Read or cancel the other expense draft."
            )
        if any(row.currency != revision.currency for row in rows):
            raise DocketError(
                code="expense_claim_currency_changed",
                message="Reconcile the earlier claim before changing its currency.",
            )
        hi = expense_units(
            revision.reimbursable_amount
            if revision.reimbursable_amount is not None
            else revision.amount
        )
        from docket.domain.financial_pricing import remaining_ranges

        known_precision = {
            row.money_decimals for row in result.allocations if row.currency == revision.currency
        }
        decimals = (
            precision
            if precision is not None
            else next(iter(known_precision))
            if len(known_precision) == 1
            else 2
        )
        observation = self.expenses.observation(revision.expense_ref)
        for first, last in remaining_ranges(
            0, hi, [(int(row.start_units), int(row.end_units)) for row in rows]
        ):
            flags = []
            if observation and observation.status == "rejected":
                flags.append("expense_reported_rejection")
            if revision.reimbursable is not True:
                flags.append("expense_eligibility_unresolved")
            if revision.receipt_required and revision.receipt_status != "available":
                flags.append("expense_required_receipt_unavailable")
            quote = {
                "kind": "expense",
                "calculation_version": "invoice-allocation-v1",
                "expense_ref": revision.expense_ref,
                "revision_ref": revision.ref_id,
                "flags": flags,
                "reimbursable": revision.reimbursable,
            }
            allocation = self.allocation(
                document,
                key,
                first,
                last,
                quote,
                revision.currency,
                decimals,
                changeset_ref,
                expense_ref=revision.expense_ref,
                expense_revision_ref=revision.ref_id,
            )
            result.allocations.append(allocation)
            if revision.payer_ref:
                result.payer_refs.add(revision.payer_ref)
            result.lines.append(
                {
                    "record_type": "expense",
                    "allocation_ref": allocation.ref_id,
                    "expense_ref": revision.expense_ref,
                    "revision_ref": revision.ref_id,
                    "day": revision.incurred_on.isoformat() if revision.incurred_on else None,
                    "description": revision.description,
                    "currency": revision.currency,
                    "amount": str(Decimal(last - first) / Decimal(1_000_000_000)),
                    "gross_line_estimate": money(Fraction(last - first, 1_000_000_000), decimals),
                    "quote": quote,
                }
            )
            result.flags.update(flags)
