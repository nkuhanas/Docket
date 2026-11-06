"""Posted claim sources occur once across invoice, timesheet and expense views."""

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from itertools import pairwise
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.compensation import money, rounded_integer
from docket.domain.financial_pricing import (
    NANO,
    WorkPriceBasis,
    distribute_money,
    expense_units,
    price_quote,
)
from docket.domain.settlement_flow import SettlementPart, SettlementScope
from docket.models import (
    CompensationEvaluation,
    DocumentAllocation,
    Expense,
    ExpenseObservation,
    ExpenseRevision,
    FinancialDocument,
    FinancialDocumentRevision,
    WorkClaim,
)
from docket.services.compensation_policies import instant
from docket.services.work_claims import WorkClaimService
from docket.services.work_quantities import slice_spans


@dataclass
class ClaimQuote:
    ref: str
    target_ref: str
    basis_ref: str
    source_key: str
    start: int
    end: int
    currency: str
    amount_nano: int
    quote: dict[str, Any]
    priority: int
    created_at: datetime
    category: str

    def scope(self) -> SettlementScope:
        return SettlementScope(self.source_key, self.start, self.end, self.currency)

    def portion_nano(self, start: int, end: int) -> int:
        whole = price_quote(self.quote, self.start, self.end)

        def prefix(point: int) -> int:
            if point == self.start:
                return 0
            value = price_quote(self.quote, self.start, point)
            if whole is None or whole == 0 or value is None:
                return rounded_integer(
                    Fraction(self.amount_nano * (point - self.start), self.end - self.start)
                )
            return rounded_integer(self.amount_nano * value / whole)

        return prefix(end) - prefix(start)


@dataclass
class ClaimSources:
    quotes: list[ClaimQuote]
    targets: dict[str, dict[str, Any]]
    flags: set[str]

    def scopes(self, ref: str, currency: str | None = None) -> list[SettlementScope]:
        scopes = {
            q.scope()
            for q in self.quotes
            if q.target_ref == ref and (currency is None or q.currency == currency)
        }
        return sorted(scopes, key=lambda s: (s.source_key, s.currency, s.start, s.end))

    def parts(
        self, extra: list[SettlementScope]
    ) -> tuple[list[SettlementPart], dict[str, ClaimQuote]]:
        parts = []
        chosen = {}
        for key in sorted({q.source_key for q in self.quotes}):
            quotes = [q for q in self.quotes if q.source_key == key]
            points = sorted(
                {p for q in quotes for p in (q.start, q.end)}
                | {p for s in extra if s.source_key == key for p in (s.start, s.end)}
            )
            for first, last in pairwise(points):
                covering = [q for q in quotes if q.start <= first and q.end >= last]
                if not covering:
                    continue
                primary = max(covering, key=lambda q: (q.priority, instant(q.created_at), q.ref))
                if len({q.currency for q in covering}) > 1:
                    self.flags.add("overlapping_claim_currency_conflict")
                ref = f"{key}:{first}:{last}:{primary.currency}"
                parts.append(
                    SettlementPart(
                        ref,
                        key,
                        first,
                        last,
                        primary.currency,
                        Fraction(primary.portion_nano(first, last), NANO),
                    )
                )
                chosen[ref] = primary
        return parts, chosen


class FinancialSourceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def capture(self, as_of: datetime) -> ClaimSources:
        owner = require_principal("read").operator_ref
        result = ClaimSources([], {}, set())
        d, r = FinancialDocument, FinancialDocumentRevision
        latest = (
            select(
                r.ref_id,
                func.row_number()
                .over(partition_by=r.document_ref, order_by=r.revision.desc())
                .label("n"),
            )
            .join(d, d.ref_id == r.document_ref)
            .where(d.operator_ref == owner, r.created_at <= as_of)
            .subquery()
        )
        documents = self.session.execute(
            select(d, r)
            .join(r, r.document_ref == d.ref_id)
            .join(latest, (latest.c.ref_id == r.ref_id) & (latest.c.n == 1))
        ).all()
        for head, revision in documents:
            summary = revision.snapshot_json.get("summary", {})
            target = {
                "ref": head.ref_id,
                "kind": head.kind,
                "status": revision.status,
                "revision_ref": revision.ref_id,
                "version": revision.revision,
                "engagement_ref": head.engagement_ref,
                "date_from": revision.date_from.isoformat(),
                "date_to": revision.date_to.isoformat(),
                "due_on": revision.snapshot_json.get("header", {}).get("due_on"),
                "label": revision.snapshot_json.get("header", {}).get("client_label")
                or revision.snapshot_json.get("engagement_label", head.engagement_ref),
                "flags": list(summary.get("flags", [])),
                "created_at": instant(revision.created_at).isoformat(),
            }
            result.targets[head.ref_id] = target
            if head.kind == "invoice" and revision.status == "issued":
                self.invoice(result, head, revision)
            elif head.kind == "timesheet" and revision.status in {
                "submitted",
                "approved",
                "rejected",
            }:
                self.timesheet(result, head, revision)
        self.expenses(result, owner, as_of)
        return result

    def invoice(
        self, result: ClaimSources, head: FinancialDocument, revision: FinancialDocumentRevision
    ) -> None:
        rows = list(
            self.session.scalars(
                select(DocumentAllocation).where(
                    DocumentAllocation.document_ref == head.ref_id,
                    DocumentAllocation.ref_id.in_(
                        revision.snapshot_json.get("allocation_refs", [])
                    ),
                )
            )
        )
        groups: dict[tuple[str, int], list[DocumentAllocation]] = {}
        for row in rows:
            if row.currency is None or row.gross_numerator is None or row.gross_denominator is None:
                result.flags.add("posted_invoice_monetary_basis_unknown")
                continue
            groups.setdefault((row.currency, row.money_decimals), []).append(row)
        for (currency, precision), allocations in groups.items():
            amounts = []
            for row in allocations:
                assert row.gross_numerator is not None and row.gross_denominator is not None
                amounts.append(Fraction(int(row.gross_numerator), int(row.gross_denominator)))
            total = sum(amounts, Fraction(0))
            rounded = expense_units(money(total, precision))
            assigned = distribute_money(rounded, amounts)
            for row, amount in zip(allocations, assigned, strict=True):
                result.quotes.append(
                    ClaimQuote(
                        row.ref_id,
                        head.ref_id,
                        revision.ref_id,
                        row.source_key,
                        int(row.start_units),
                        int(row.end_units),
                        currency,
                        amount,
                        row.quote_json,
                        30,
                        revision.created_at,
                        "reimbursement" if row.expense_ref else "work_compensation",
                    )
                )

    def timesheet(
        self, result: ClaimSources, head: FinancialDocument, revision: FinancialDocumentRevision
    ) -> None:
        evaluation = self.session.scalar(
            select(CompensationEvaluation).where(
                CompensationEvaluation.ref_id == revision.evaluation_ref
            )
        )
        if evaluation is None:
            result.flags.add("posted_timesheet_monetary_basis_unknown")
            return
        rows = evaluation.result_json["contributions"]
        claims = list(
            self.session.scalars(
                select(WorkClaim).where(
                    WorkClaim.ref_id.in_(revision.snapshot_json.get("claim_refs", []))
                )
            )
        )
        spans = WorkClaimService(self.session).source_spans(
            sorted({line["revision_ref"] for line in rows})
        )
        terms = {row["term_ref"]: row["terms"] for row in evaluation.result_json["basis"]}
        pending: list[tuple[ClaimQuote, Fraction, int]] = []
        for index, line in enumerate(rows):
            raw = line.get("raw_us")
            currency = line.get("currency")
            if not raw or currency is None:
                result.flags.add("posted_timesheet_monetary_basis_unknown")
                continue
            pay = terms.get(line.get("pay_term_ref"), {})
            reporting = terms.get(line.get("reporting_term_ref"), {})
            basis = WorkPriceBasis(
                raw,
                line.get("reportable_us"),
                line.get("overtime_us"),
                pay.get("rate") if pay.get("basis") == "hourly" else None,
                reporting.get("overtime_multiplier") or "1",
            )
            offset = evaluation.result_json["retained"][index]["offset_us"]
            line_position = 0
            for span in slice_spans(spans[line["revision_ref"]], offset, offset + raw):
                for claim in claims:
                    if claim.origin_ref != span.origin_ref:
                        continue
                    first, last = max(span.start, claim.start_us), min(span.end, claim.end_us)
                    if first >= last:
                        continue
                    quote = {
                        "kind": "work",
                        "source_start": span.start,
                        "line_offset_us": line_position,
                        "basis": {
                            "raw_us": basis.raw_us,
                            "reportable_us": basis.reportable_us,
                            "overtime_us": basis.overtime_us,
                            "rate": basis.rate,
                            "overtime_multiplier": basis.overtime_multiplier,
                        },
                    }
                    value = price_quote(quote, first, last)
                    if value is None:
                        result.flags.add("posted_timesheet_monetary_basis_unknown")
                        continue
                    candidate = ClaimQuote(
                        f"{revision.ref_id}:{index}:{claim.ref_id}",
                        head.ref_id,
                        revision.ref_id,
                        f"work:{span.origin_ref}:{head.component_key}",
                        first,
                        last,
                        currency,
                        0,
                        quote,
                        20 if revision.status == "approved" else 10,
                        revision.created_at,
                        "work_compensation",
                    )
                    pending.append((candidate, value, pay.get("money_decimals") or 0))
                line_position += span.quantity
        for currency, precision in sorted({(q.currency, p) for q, _value, p in pending}):
            group = [
                (q, value) for q, value, p in pending if q.currency == currency and p == precision
            ]
            total = sum((value for _q, value in group), Fraction(0))
            target = expense_units(money(total, precision))
            assigned = distribute_money(target, [value for _q, value in group])
            for (candidate_quote, _value), amount in zip(group, assigned, strict=True):
                candidate_quote.amount_nano = amount
                result.quotes.append(candidate_quote)

    def expenses(self, result: ClaimSources, owner: str, as_of: datetime) -> None:
        e, o = Expense, ExpenseObservation
        latest = (
            select(
                o.ref_id,
                func.row_number()
                .over(partition_by=o.expense_ref, order_by=o.version.desc())
                .label("n"),
            )
            .join(e, e.ref_id == o.expense_ref)
            .where(e.operator_ref == owner, o.created_at <= as_of)
            .subquery()
        )
        rows = self.session.execute(
            select(e, o, ExpenseRevision)
            .join(o, o.expense_ref == e.ref_id)
            .join(latest, (latest.c.ref_id == o.ref_id) & (latest.c.n == 1))
            .join(ExpenseRevision, ExpenseRevision.ref_id == o.basis_revision_ref)
        ).all()
        versions = {
            ref: version
            for ref, version in self.session.execute(
                select(ExpenseRevision.expense_ref, func.max(ExpenseRevision.revision))
                .join(e, e.ref_id == ExpenseRevision.expense_ref)
                .where(e.operator_ref == owner, ExpenseRevision.created_at <= as_of)
                .group_by(ExpenseRevision.expense_ref)
            ).all()
        }
        for expense, observation, revision in rows:
            flags = ["reported_expense_rejection"] if observation.status == "rejected" else []
            if versions[expense.ref_id] != observation.version:
                flags.append("expense_changed_since_claim_observation")
            result.targets[expense.ref_id] = {
                "ref": expense.ref_id,
                "kind": "expense",
                "status": observation.status,
                "revision_ref": observation.basis_revision_ref,
                "observation_ref": observation.ref_id,
                "version": versions[expense.ref_id],
                "engagement_ref": revision.engagement_ref,
                "label": revision.description[:200],
                "created_at": instant(observation.created_at).isoformat(),
                "flags": flags,
            }
            if observation.amount is None:
                result.flags.add("reported_expense_amount_unknown")
                continue
            quantity = expense_units(observation.amount)
            if quantity:
                result.quotes.append(
                    ClaimQuote(
                        observation.ref_id,
                        expense.ref_id,
                        observation.ref_id,
                        "expense:" + expense.ref_id,
                        0,
                        quantity,
                        observation.currency,
                        quantity,
                        {"kind": "expense"},
                        20 if observation.status == "approved" else 10,
                        observation.created_at,
                        "reimbursement",
                    )
                )
