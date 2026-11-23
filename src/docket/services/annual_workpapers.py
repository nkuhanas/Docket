"""Build a year from current revisions and posted facts. No journal writes occur here."""

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AccountActivity,
    AttachmentEvidence,
    Expense,
    ExpenseRevision,
    FinancialCandidate,
    FinancialDocumentRevision,
    FinancialIntake,
    FinancialReviewFact,
    FundingIdentity,
    StatementImport,
)
from docket.models.base import utc_now
from docket.services.financial_intake import FinancialIntakeService
from docket.services.financial_review import FinancialReviewService, current_facts
from docket.services.financial_state import FinancialState, amount_text

ZERO = Decimal(0)


def number(value: Decimal | int) -> str:
    return format(value, "f")


class AnnualWorkpapers:
    def __init__(self, session: Session, year: int):
        self.session, self.year = session, year
        self.owner = require_principal("read").operator_ref
        self.facts = current_facts(session, self.owner)
        self.state = FinancialState(session)
        self.sections: dict[str, list[dict[str, Any]]] = {
            k: []
            for k in (
                "decisions",
                "expenses",
                "allocations",
                "income",
                "totals",
                "evidence",
                "recurrence",
            )
        }
        self.totals: dict[tuple[str, str, str], defaultdict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        self.source_refs: set[str] = set()
        self.postings = {
            a.ref_id: a
            for a in session.scalars(
                select(AccountActivity).where(AccountActivity.operator_ref == self.owner)
            )
        }
        self.corrected = {r.fields.get("corrects_ref") for r in self.postings.values()}
        self.statements = list(
            session.scalars(
                select(StatementImport)
                .join(FundingIdentity, FundingIdentity.ref_id == StatementImport.account_ref)
                .where(FundingIdentity.operator_ref == self.owner)
                .order_by(StatementImport.ref_id)
            )
        )
        self.candidates: defaultdict[
            str | None, list[tuple[FinancialCandidate, FinancialIntake]]
        ] = defaultdict(list)
        for c, intake in session.execute(
            select(FinancialCandidate, FinancialIntake)
            .join(FinancialIntake)
            .where(FinancialIntake.operator_ref == self.owner)
            .order_by(FinancialCandidate.ref_id)
        ):
            self.candidates[c.expense_ref].append((c, intake))

    def issue(self, kind: str, subject: str, summary: str, **detail: Any) -> None:
        self.sections["decisions"].append(
            dict(kind=kind, subject=subject, summary=summary, **detail)
        )

    def bucket(
        self, currency: str, jurisdiction: str = "", tax_type: str = ""
    ) -> defaultdict[str, Decimal]:
        return self.totals[(currency, jurisdiction, tax_type)]

    def eligible_year(self, day: str | date | None) -> bool:
        return day is None or str(day)[:4] == str(self.year)

    def build(self) -> dict[str, list[dict[str, Any]]]:
        self.expenses()
        self.activity()
        self.recurring()
        self.tax()
        self.income()
        covered_sources = {r.fields.get("source_ref") for r in self.facts}
        covered_sources.update(s.source_ref for s in self.statements)
        for intake in self.session.scalars(
            select(FinancialIntake).where(FinancialIntake.operator_ref == self.owner)
        ):
            role = intake.declarations.get("evidence_role")
            if (
                role
                and intake.source_ref not in covered_sources
                and self.eligible_year(intake.created_at.date())
            ):
                self.source_refs.add(intake.source_ref)
                self.issue(
                    "unclassified_document",
                    intake.source_ref,
                    "Classify this retained financial document.",
                    role=role,
                )
        for c, intake in self.candidates[None]:
            if intake.source_ref in covered_sources:
                continue
            if self.eligible_year(
                c.facts.get("incurred_on") or intake.created_at.date()
            ) and c.disposition not in ("dismissed", "ignored"):
                self.source_refs.add(intake.source_ref)
                self.issue(
                    "unrecognized_evidence",
                    c.ref_id,
                    "Review financial evidence awaiting recognition.",
                    source_ref=intake.source_ref,
                    disposition=c.disposition,
                    version=c.version,
                )
        for ref in sorted(self.source_refs):
            try:
                FinancialReviewService(self.session).evidence(ref)
            except DocketError:
                metadata = None
            else:
                metadata = self.session.scalar(
                    select(AttachmentEvidence).where(AttachmentEvidence.ref_id == ref)
                )
            self.sections["evidence"].append(
                dict(
                    source_ref=ref,
                    retention=metadata.retention_disposition
                    if metadata
                    else "inaccessible_or_unavailable",
                    digest=metadata.content_hash if metadata else None,
                )
            )
        for (cur, jurisdiction, tax_type), totals in sorted(self.totals.items()):
            if jurisdiction and tax_type:
                totals["gross_tax_paid"] = sum(
                    (totals[k] for k in ("estimated", "balance", "withholding")), ZERO
                )
                totals["net_tax_paid"] = totals["gross_tax_paid"] - totals["refund"]
            self.sections["totals"].append(
                {
                    "currency": cur,
                    "jurisdiction": jurisdiction,
                    "tax_type": tax_type,
                    **{k: number(v) for k, v in sorted(totals.items())},
                }
            )
        self.sections["decisions"].sort(key=lambda d: (d["kind"], d["subject"]))
        return self.sections

    def expenses(self) -> None:
        query = (
            select(ExpenseRevision)
            .join(Expense, Expense.ref_id == ExpenseRevision.expense_ref)
            .where(
                Expense.operator_ref == self.owner,
                Expense.retracted.is_(False),
                Expense.version == ExpenseRevision.revision,
            )
            .order_by(Expense.ref_id)
        )
        for rev in self.session.scalars(query):
            ref = rev.expense_ref
            links = [
                r
                for r in self.facts
                if r.kind == "activity_match" and r.fields.get("expense_ref") == ref
            ]
            treatments = [
                r
                for r in self.facts
                if r.kind == "expense_treatment" and r.fields["expense_ref"] == ref
            ]
            if not self.eligible_year(rev.incurred_on) and not any(
                r.fields["tax_year"] == self.year for r in treatments
            ):
                continue
            amount = Decimal(rev.amount)
            evidence = self.candidates[ref]
            observations = {c.activity_ref for c, _ in evidence if c.activity_ref}
            funded = sum(
                abs(Decimal(self.postings[a].fields["amount"]))
                for a in observations
                if a not in self.corrected
            )
            by_role: defaultdict[str, Decimal] = defaultdict(Decimal)
            for r in links:
                if (
                    r.fields["activity_ref"] in self.corrected
                    or r.fields.get("counterpart_ref", "") in self.corrected
                ):
                    self.issue(
                        "corrected_posting",
                        r.ref_id,
                        "Review a match against a corrected posting.",
                        expense_ref=ref,
                    )
                else:
                    by_role[r.fields["role"]] += Decimal(r.fields["amount"])
            funded += by_role["purchase"]
            reimbursed = ZERO
            settlements = []
            for entry in self.state.entries:
                if entry.kind == "cash":
                    for match in self.state.matches_json(entry.ref_id):
                        if match["source_key"] == "expense:" + ref:
                            reimbursed += Decimal(match["amount_nano"]).scaleb(-9)
                            settlements.append(f"{entry.ref_id}/{match['amount_nano']}")
            if reimbursed + by_role["refund"] > amount:
                self.issue(
                    "excess_recovery",
                    ref,
                    "Review refunds and reimbursements exceeding the cost.",
                    expense_revision_ref=rev.ref_id,
                    reimbursed=number(reimbursed),
                    refunded=number(by_role["refund"]),
                )
            declarations = []
            for c, intake in evidence:
                self.source_refs.add(intake.source_ref)
                declarations.append(
                    dict(
                        candidate_ref=c.ref_id,
                        version=c.version,
                        source_ref=intake.source_ref,
                        **FinancialIntakeService.declaration(intake, c),
                    )
                )
            if rev.receipt_ref:
                self.source_refs.add(rev.receipt_ref)
            if rev.receipt_required and not (rev.receipt_ref or evidence):
                self.issue(
                    "missing_receipt",
                    ref,
                    "Supply or explain the required receipt.",
                    revision_ref=rev.ref_id,
                )
            if not rev.incurred_on:
                self.issue(
                    "undated_expense",
                    ref,
                    "Identify the year for this expense.",
                    revision_ref=rev.ref_id,
                )
            elif self.eligible_year(rev.incurred_on):
                self.bucket(rev.currency)["economic_expense"] += amount
            if funded != amount or observations & self.corrected:
                choices = [
                    a.ref_id
                    for a in self.postings.values()
                    if a.ref_id not in self.corrected
                    and a.fields["state"] == "posted"
                    and a.fields["currency"] == rev.currency
                    and a.fields["kind"] in ("purchase", "unknown")
                    and rev.incurred_on
                    and abs((date.fromisoformat(a.fields["posted_on"]) - rev.incurred_on).days) <= 7
                    and Decimal(a.fields["amount"]) == -(amount - funded)
                ]
                self.issue(
                    "funding",
                    ref,
                    "Review the unmatched purchase amount.",
                    funded=number(funded),
                    expense_revision_ref=rev.ref_id,
                    possible_count=len(choices),
                    possible_refs=sorted(choices)[:8],
                )
            pct = {
                Decimal(str(d["business_use_percent"]))
                for d in declarations
                if d.get("business_use_percent") is not None
            }
            if (
                len(pct) > 1
                or len({d["write_off_intent"] for d in declarations if "write_off_intent" in d}) > 1
            ):
                self.issue(
                    "declaration_conflict",
                    ref,
                    "Resolve conflicting operator declarations.",
                    sources=[d["source_ref"] for d in declarations],
                )
            proposed = amount * next(iter(pct)) / 100 if len(pct) == 1 else None
            active = [r for r in treatments if r.fields["tax_year"] == self.year]
            if not active:
                self.issue(
                    "tax_treatment",
                    ref,
                    "Review tax treatment or record an exclusion.",
                    revision_ref=rev.ref_id,
                )
            basis = sorted(
                [r.ref_id for r in links if r.fields["role"] in ("refund", "reimbursement")]
                + settlements
            )
            needs_review = False
            tax_status = "unreviewed"
            for jurisdiction in sorted({r.fields["jurisdiction"] for r in active}):
                selected = [r for r in active if r.fields["jurisdiction"] == jurisdiction]
                allocated = sum(Decimal(r.fields["allocated_amount"]) for r in selected)
                if allocated != amount:
                    needs_review = True
                    self.issue(
                        "tax_allocation",
                        ref + "/" + jurisdiction,
                        "Review the unallocated cost or changed amount.",
                        allocated=number(allocated),
                    )
                for r in selected:
                    f = r.fields
                    if f.get("source_ref"):
                        self.source_refs.add(f["source_ref"])
                    stale = (
                        f["expense_revision_ref"] != rev.ref_id
                        or f.get("adjustment_basis", []) != basis
                    )
                    if stale or f["status"] in ("needs_evidence", "asset_review"):
                        needs_review = True
                        self.issue(
                            "treatment_review",
                            r.ref_id,
                            "Review the changed basis or pending tax treatment.",
                            expense_ref=ref,
                            source_basis=basis,
                            expense_revision_ref=rev.ref_id,
                        )
                    if f.get("reviewed_deduction") is not None:
                        self.bucket(rev.currency, jurisdiction)["reviewed_deduction"] += Decimal(
                            f["reviewed_deduction"]
                        )
                    self.sections["allocations"].append(
                        dict(ref=r.ref_id, kind=r.kind, **f, basis_changed=stale)
                    )
            if active:
                tax_status = "review_required" if needs_review else "reviewed"
            self.sections["expenses"].append(
                dict(
                    expense_ref=ref,
                    revision_ref=rev.ref_id,
                    description=rev.description,
                    incurred_on=str(rev.incurred_on) if rev.incurred_on else None,
                    currency=rev.currency,
                    amount=rev.amount,
                    funded=number(funded),
                    refunded=number(by_role["refund"]),
                    reimbursed=number(reimbursed),
                    matched_reimbursement=number(by_role["reimbursement"]),
                    declared_amount=number(proposed) if proposed is not None else None,
                    declarations=declarations,
                    tax_status=tax_status,
                    match_refs=[r.ref_id for r in links],
                    intake_activity_refs=sorted(observations),
                    settlement_refs=sorted({s.split("/")[0] for s in settlements}),
                    receipt_ref=rev.receipt_ref,
                )
            )
            if proposed is not None and rev.incurred_on and self.eligible_year(rev.incurred_on):
                self.bucket(rev.currency)["declared_business_amount"] += proposed

    def activity(self) -> None:
        for r in self.facts:
            if r.kind == "activity_match" and self.eligible_year(r.fields["posted_on"]):
                self.sections["allocations"].append(dict(ref=r.ref_id, kind=r.kind, **r.fields))
        # Whole-period coverage belongs to the account and original statement.
        for s in self.statements:
            if s.basis["period_end"][:4] < str(self.year) or s.basis["period_start"][:4] > str(
                self.year
            ):
                continue
            self.source_refs.add(s.source_ref)
            self.sections["allocations"].append(
                dict(
                    kind="statement_period",
                    ref=s.ref_id,
                    account_ref=s.account_ref,
                    period_start=s.basis["period_start"],
                    period_end=s.basis["period_end"],
                    source_ref=s.source_ref,
                    unresolved_rows=sum("ref" not in row for row in s.rows),
                )
            )
        for a in self.postings.values():
            if a.ref_id in self.corrected or not self.eligible_year(a.fields["posted_on"]):
                continue
            if a.fields["state"] != "posted":
                continue
            consumed = sum(
                (
                    Decimal(r.fields["amount"])
                    for r in self.facts
                    if r.kind in ("activity_match", "tax_allocation")
                    and a.ref_id in (r.fields.get("activity_ref"), r.fields.get("counterpart_ref"))
                ),
                ZERO,
            )
            intake_match = any(
                c.activity_ref == a.ref_id for cs in self.candidates.values() for c, _ in cs
            )
            if (
                a.fields["kind"] != "transfer"
                and not intake_match
                and consumed < abs(Decimal(a.fields["amount"]))
            ):
                self.issue(
                    "unmatched_posting",
                    a.ref_id,
                    "Identify the unmatched statement amount.",
                    remaining=number(abs(Decimal(a.fields["amount"])) - consumed),
                    currency=a.fields["currency"],
                )
            if a.fields["kind"] == "transfer" and not any(
                r.kind == "activity_match"
                and a.ref_id in (r.fields.get("activity_ref"), r.fields.get("counterpart_ref"))
                for r in self.facts
            ):
                self.issue(
                    "unpaired_transfer", a.ref_id, "Identify or explain the transfer counterpart."
                )

    def recurring(self) -> None:
        for r in self.facts:
            if r.kind != "recurring_expense":
                continue
            f = r.fields
            self.sections["recurrence"].append(dict(kind="expectation", ref=r.ref_id, **f))
            first = date.fromisoformat(f["first_expected_on"])
            last = min(utc_now().date(), date.fromisoformat(f.get("ends_on", "9999-12-31")))
            for month in range(1, 13):
                if f["cadence"] == "yearly" and month != first.month:
                    continue
                end = calendar.monthrange(self.year, month)[1]
                due = date(self.year, month, min(first.day, end))
                if not first <= due <= last:
                    continue
                start, stop = due.replace(day=1).isoformat(), due.replace(day=end).isoformat()
                statements = [
                    s
                    for s in self.statements
                    if s.account_ref == f["account_ref"]
                    and s.basis["period_start"] <= start
                    and s.basis["period_end"] >= stop
                    and all("ref" in row for row in s.rows)
                ]
                charges = [
                    a
                    for a in self.postings.values()
                    if a.account_ref == f["account_ref"]
                    and a.ref_id not in self.corrected
                    and a.fields["state"] == "posted"
                    and a.fields["kind"] in ("purchase", "unknown")
                    and a.fields["currency"] == f["currency"]
                    and a.fields["description"].casefold().strip()
                    == f["merchant"].casefold().strip()
                    and start <= a.fields["posted_on"] <= stop
                    and Decimal(a.fields["amount"]) < 0
                ]
                status = (
                    "ambiguous"
                    if len(charges) > 1
                    else (
                        "observed"
                        if -Decimal(charges[0].fields["amount"]) == Decimal(f["amount"])
                        else "price_changed"
                    )
                    if charges
                    else "missing"
                    if statements and stop <= last.isoformat()
                    else "coverage_unknown"
                )
                key = r.identity_key + "/" + due.isoformat()
                row = dict(
                    expectation_ref=r.ref_id,
                    due=due.isoformat(),
                    status=status,
                    activity_refs=sorted(a.ref_id for a in charges),
                    statement_refs=[s.ref_id for s in statements],
                )
                self.sections["recurrence"].append(row)
                if status in ("missing", "price_changed", "ambiguous"):
                    self.issue(
                        "recurring_charge", key, "Review the expected recurring charge.", **row
                    )

    def tax(self) -> None:
        for r in self.facts:
            if r.kind != "tax_allocation":
                continue
            f = r.fields
            if f.get("tax_year") not in (None, self.year):
                continue
            if f.get("source_ref"):
                self.source_refs.add(f["source_ref"])
            self.sections["allocations"].append(dict(ref=r.ref_id, kind_of_record=r.kind, **f))
            if not f.get("tax_year"):
                self.issue("tax_year_unknown", r.ref_id, "Designate the year for this tax payment.")
                continue
            if f.get("activity_ref", "") in self.corrected or (
                f.get("deduction_ref")
                and Decimal(amount_text(self.state.effective.get(f["deduction_ref"], Fraction(0))))
                < Decimal(f["amount"])
            ):
                self.issue(
                    "tax_basis_changed", r.ref_id, "Review a corrected or reversed tax payment."
                )
                continue
            self.bucket(f["currency"], f["jurisdiction"], f["tax_type"])[f["kind"]] += Decimal(
                f["amount"]
            )

    def income(self) -> None:
        groups: defaultdict[tuple[str, str, str, str], list[FinancialReviewFact]] = defaultdict(
            list
        )
        for r in self.facts:
            if r.kind == "income_evidence" and r.fields["tax_year"] == self.year:
                f = r.fields
                groups[
                    (f["engagement_ref"], f["jurisdiction"], f["currency"], f["tax_type"])
                ].append(r)
                self.source_refs.add(f["source_ref"])
                self.sections["income"].append(dict(ref=r.ref_id, **f))
        for key, rows in sorted(groups.items()):
            engagement, jurisdiction, cur, tax_type = key
            period = sorted(
                (r for r in rows if r.fields["coverage"] == "period"),
                key=lambda r: r.fields["period_start"],
            )
            annual = [r for r in rows if r.fields["coverage"] == "annual"]
            duplicate = any(
                b.fields["period_start"] <= a.fields["period_end"] for a, b in pairwise(period)
            )
            refs = sorted(r.ref_id for r in rows)
            batch = sha256_json(key)
            if duplicate or len(annual) > 1:
                self.issue(
                    "income_overlap",
                    batch,
                    "Resolve overlapping periods or annual forms before totaling them.",
                    records=refs,
                )
                continue
            supported = period or annual
            if not supported:
                self.issue(
                    "income_coverage",
                    batch,
                    "Supply period or annual evidence for year-to-date figures.",
                    records=refs,
                )
                continue
            totals = {
                k: sum(Decimal(r.fields[k]) for r in supported)
                if all(k in r.fields for r in supported)
                else None
                for k in ("gross", "withheld", "net")
            }
            if annual and period:
                for field in ("gross", "withheld"):
                    expected = annual[0].fields.get(field)
                    if expected is not None and totals[field] != Decimal(expected):
                        self.issue(
                            "annual_difference",
                            batch + "/" + field,
                            "Reconcile annual and period amounts.",
                            column=field,
                            records=refs,
                            annual=expected,
                            periods=str(totals[field]),
                        )
            for ytd in (r for r in rows if r.fields["coverage"] == "year_to_date"):
                covered = [
                    r
                    for r in period
                    if ytd.fields["period_start"] <= r.fields["period_start"]
                    and r.fields["period_end"] <= ytd.fields["period_end"]
                ]
                for column in ("gross", "withheld"):
                    expected_ytd = ytd.fields.get(column)
                    if expected_ytd is None:
                        continue
                    observed = sum(
                        (Decimal(r.fields[column]) for r in covered if column in r.fields), ZERO
                    )
                    if (
                        not covered
                        or any(column not in r.fields for r in covered)
                        or observed != Decimal(expected_ytd)
                    ):
                        self.issue(
                            "year_to_date_difference",
                            ytd.ref_id + "/" + column,
                            "Compare cumulative payroll figures with documented periods.",
                            column=column,
                            reported=expected_ytd,
                            period_total=number(observed),
                        )
            if not annual:
                self.issue(
                    "annual_coverage", batch, "Annual corroboration is outstanding.", records=refs
                )
            if totals["withheld"] is None:
                self.issue(
                    "withholding_unknown",
                    batch,
                    "Establish withholding or document zero.",
                    records=refs,
                )
            else:
                # A separately classified settlement deduction may be the same withheld money.
                # Until attributed to this pay evidence its overlap is unresolved.
                linked = {r.fields.get("withholding_allocation_ref") for r in supported} - {None}
                standalone = {
                    r.ref_id
                    for r in self.facts
                    if r.kind == "tax_allocation"
                    and r.fields["kind"] == "withholding"
                    and (
                        r.fields.get("tax_year"),
                        r.fields["jurisdiction"],
                        r.fields["currency"],
                        r.fields["tax_type"],
                    )
                    == (self.year, jurisdiction, cur, tax_type)
                }
                if (
                    standalone - linked
                    or linked - standalone
                    or len(linked)
                    != sum(bool(r.fields.get("withholding_allocation_ref")) for r in supported)
                ):
                    self.issue(
                        "withholding_overlap",
                        batch,
                        "Identify overlapping settlement and payroll withholding.",
                        records=refs,
                    )
                else:
                    additional = sum(
                        Decimal(r.fields["withheld"])
                        for r in supported
                        if not r.fields.get("withholding_allocation_ref")
                    )
                    self.bucket(cur, jurisdiction, tax_type)["withholding"] += additional
            payment_refs = sorted(
                {r.fields["payment_ref"] for r in rows if r.fields.get("payment_ref")}
            )
            received = sum(
                (
                    Decimal(amount_text(self.state.received.get(ref, Fraction(0))))
                    for ref in payment_refs
                ),
                ZERO,
            )
            if totals["net"] is None or not payment_refs or received != totals["net"]:
                self.issue(
                    "income_receipts",
                    batch,
                    "Compare net pay with evidenced payments.",
                    records=refs,
                    payments=payment_refs,
                )
            self.sections["income"].append(
                dict(
                    kind="income_comparison",
                    engagement_ref=engagement,
                    jurisdiction=jurisdiction,
                    currency=cur,
                    tax_type=tax_type,
                    basis_refs=refs,
                    payment_refs=payment_refs,
                    reported_gross=str(totals["gross"]),
                    reported_net=number(totals["net"]) if totals["net"] is not None else None,
                    received=number(received),
                    reported_withholding=number(totals["withheld"])
                    if totals["withheld"] is not None
                    else None,
                )
            )
        self.compensation(groups)

    def compensation(
        self, groups: dict[tuple[str, str, str, str], list[FinancialReviewFact]]
    ) -> None:
        # Use the settlement engine's disjoint parts. Timesheets and invoices can
        # quote the same work. Adding their document totals would count it twice.
        amounts: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
        bases: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        cross_year: set[tuple[str, str]] = set()
        for part in self.state.parts:
            q = self.state.primary[part.ref]
            if q.category != "work_compensation":
                continue
            doc = self.session.scalar(
                select(FinancialDocumentRevision).where(
                    FinancialDocumentRevision.ref_id == q.basis_ref
                )
            )
            if not doc or doc.date_from.year > self.year or doc.date_to <= date(self.year, 1, 1):
                continue
            engagement = self.state.sources.targets[q.target_ref]["engagement_ref"]
            key = (engagement, part.currency)
            bases[key].add(q.basis_ref)
            if doc.date_from < date(self.year, 1, 1) or doc.date_to > date(self.year + 1, 1, 1):
                cross_year.add(key)
            else:
                amounts[key] += Decimal(amount_text(part.amount))
        for key, documents in sorted(bases.items()):
            engagement, currency = key
            basis = sorted(documents)
            self.sections["income"].append(
                dict(
                    kind="compensation_claims",
                    engagement_ref=engagement,
                    currency=currency,
                    posted_gross=number(amounts[key]),
                    basis_refs=basis,
                    cross_year_coverage=key in cross_year,
                )
            )
            comparisons = [
                r
                for r in self.sections["income"]
                if r.get("kind") == "income_comparison"
                and (r["engagement_ref"], r["currency"]) == key
            ]
            if (
                not comparisons
                or key in cross_year
                or any(Decimal(r["reported_gross"]) != amounts[key] for r in comparisons)
            ):
                self.issue(
                    "compensation_coverage",
                    engagement + "/" + currency,
                    "Compare posted compensation with reported income and service periods.",
                    posted_gross=number(amounts[key]),
                    basis_refs=basis,
                )
        linked = {r.fields.get("payment_ref") for rows in groups.values() for r in rows}
        for ref, p in self.state.payments.items():
            if p.category not in ("wages", "contract") or not self.eligible_year(
                p.received_on or p.received_at
            ):
                continue
            received = Decimal(amount_text(self.state.received[ref]))
            self.sections["income"].append(
                dict(
                    kind="payment",
                    payment_ref=ref,
                    version=p.version,
                    currency=p.currency,
                    received=number(received),
                    engagement_ref=p.engagement_ref,
                    received_on=str(p.received_on or p.received_at or ""),
                    category=p.category,
                )
            )
            if received > 0 and ref not in linked:
                self.issue(
                    "unmatched_income_payment",
                    ref,
                    "Identify supporting income evidence for the payment.",
                    version=p.version,
                )
