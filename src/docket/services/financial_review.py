"""Conserve observed money across expenses, tax payments and transfer legs."""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from docket.agent_auth import require_principal
from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AccountActivity,
    AttachmentEvidence,
    Engagement,
    ExpenseRevision,
    FinancialCandidate,
    FinancialIntake,
    FinancialReviewFact,
)
from docket.schemas.financial_review import (
    ActivityMatch,
    ExpenseTreatment,
    IncomeEvidence,
    RecurringExpense,
    TaxAllocation,
    WithdrawFact,
)
from docket.services.expenses import ExpenseService
from docket.services.financial_intake import FinancialIntakeService
from docket.services.financial_state import FinancialState, amount_text
from docket.services.funding import FundingService
from docket.services.notifications import identity_lock
from docket.services.work_attribution import WorkAttributionService


def reject(reason: str) -> DocketError:
    return DocketError(code="financial_review_conflict", message=reason)


def current_facts(session: Session, owner: str) -> list[FinancialReviewFact]:
    successor = aliased(FinancialReviewFact)
    rows = session.scalars(
        select(FinancialReviewFact)
        .where(
            FinancialReviewFact.operator_ref == owner,
            ~select(successor.ref_id)
            .where(successor.prior_ref == FinancialReviewFact.ref_id)
            .exists(),
        )
        .order_by(FinancialReviewFact.ref_id)
    )
    return [r for r in rows if not r.fields.get("withdrawn")]


def legacy_matches(session: Session, owner: str) -> dict[str, set[str]]:
    pairs: dict[str, set[str]] = {}
    for expense, activity in session.execute(
        select(FinancialCandidate.expense_ref, FinancialCandidate.activity_ref)
        .join(FinancialIntake)
        .where(
            FinancialIntake.operator_ref == owner,
            FinancialCandidate.activity_ref.is_not(None),
            FinancialCandidate.expense_ref.is_not(None),
        )
    ):
        pairs.setdefault(expense, set()).add(activity)
    return pairs


def lock_review(session: Session, owner: str) -> None:
    identity_lock(session, "financial-review:" + owner)


class FinancialReviewService:
    def __init__(self, session: Session):
        self.session = session

    @property
    def owner(self) -> str:
        return require_principal("read").operator_ref

    def handlers(self) -> dict[str, Any]:
        return dict.fromkeys(
            (
                "activity_match",
                "recurring_expense",
                "expense_treatment",
                "tax_allocation",
                "income_evidence",
                "financial_withdrawal",
                "financial_year_review",
            ),
            self.apply,
        )

    def posted(self, ref: str) -> AccountActivity:
        row = self.session.get(AccountActivity, ref)
        if row is None or row.operator_ref != self.owner or row.fields["state"] != "posted":
            raise reject("Select a posted observation from an owned account.")
        if self.session.scalar(
            select(AccountActivity.ref_id)
            .where(
                AccountActivity.account_ref == row.account_ref,
                AccountActivity.fields["corrects_ref"].as_string() == ref,
            )
            .limit(1)
        ):
            raise reject("Select the corrected posting.")
        return row

    def expense(self, ref: str) -> ExpenseRevision:
        svc = ExpenseService(self.session)
        head = svc.head(ref, lock=True)
        if head.retracted:
            raise reject("Select a current expense.")
        return svc.revision(head)

    def apply(self, session: Session, changeset: Any, change: Any) -> list[str]:
        owner = require_principal("commit").operator_ref
        lock_review(session, owner)
        kind = change.object_type
        if kind == "financial_year_review":
            from docket.services.financial_year import FinancialYearService

            return FinancialYearService(session).reconcile(change, changeset)
        active = current_facts(session, owner)
        specs: dict[str, type[BaseModel]] = {
            "activity_match": ActivityMatch,
            "recurring_expense": RecurringExpense,
            "expense_treatment": ExpenseTreatment,
            "tax_allocation": TaxAllocation,
            "income_evidence": IncomeEvidence,
            "financial_withdrawal": WithdrawFact,
        }
        fields = (
            specs[kind]
            .model_validate(change.create_spec)
            .model_dump(mode="json", exclude_none=True)
        )
        previous = fields.pop("prior_ref", None)
        prior = next((r for r in active if r.ref_id == previous), None)
        if previous and (not prior or kind not in (prior.kind, "financial_withdrawal")):
            raise reject("Refresh the current revision before correcting it.")
        others = [r for r in active if r != prior]
        if kind == "financial_withdrawal" and prior:
            key, kind = prior.identity_key, prior.kind
            fields["withdrawn"] = True
        else:
            validate = {
                "activity_match": self.match,
                "recurring_expense": self.recurring,
                "expense_treatment": self.treatment,
                "tax_allocation": self.tax,
                "income_evidence": self.income,
            }[kind]
            key = sha256_json(validate(fields, others))
            if any(r.kind == kind and r.identity_key == key for r in others):
                raise reject("This fact already exists. Correct its current revision.")
            # Corrections can fix a misattributed year or relationship. Retain the lineage
            # and reject a collision with another current natural identity.
        revision = prior.revision + 1 if prior else 1
        historic = session.scalar(
            select(FinancialReviewFact.revision)
            .where(
                FinancialReviewFact.operator_ref == owner,
                FinancialReviewFact.kind == kind,
                FinancialReviewFact.identity_key == key,
            )
            .order_by(FinancialReviewFact.revision.desc())
            .limit(1)
        )
        row = FinancialReviewFact(
            ref_id=new_public_ref("finrev"),
            kind=kind,
            operator_ref=owner,
            revision=max(revision, (historic or 0) + 1),
            prior_ref=previous,
            identity_key=key,
            fields=fields,
            changeset_ref=changeset.ref_id,
        )
        session.add(row)
        session.flush()
        return [row.ref_id]

    def activity_capacity(
        self, ref: str, amount: Decimal, others: list[FinancialReviewFact]
    ) -> AccountActivity:
        posting = self.posted(ref)
        used = sum(
            Decimal(r.fields["amount"])
            for r in others
            if r.kind in ("activity_match", "tax_allocation")
            and ref in (r.fields.get("activity_ref"), r.fields.get("counterpart_ref"))
        )
        if any(ref in refs for refs in legacy_matches(self.session, self.owner).values()):
            raise reject("The intake match already accounts for this posting.")
        if amount <= 0 or used + amount > abs(Decimal(posting.fields["amount"])):
            raise reject("The allocation exceeds the available posted amount.")
        return posting

    def match(self, f: dict[str, Any], others: list[FinancialReviewFact]) -> list[Any]:
        amount = Decimal(f["amount"])
        a = self.activity_capacity(f["activity_ref"], amount, others)
        role, cost = f["role"], None
        outgoing = Decimal(a.fields["amount"]) < 0
        allowed = {
            "purchase": ("purchase", "unknown"),
            "transfer": ("transfer", "unknown"),
            "refund": ("refund", "unknown"),
            "reimbursement": ("deposit", "unknown"),
        }
        if outgoing != (role in ("purchase", "transfer")) or a.fields["kind"] not in allowed[role]:
            raise reject("The posting direction or classification conflicts with this role.")
        f["currency"], f["posted_on"] = a.fields["currency"], a.fields["posted_on"]
        if "expense_ref" in f:
            cost = self.expense(f["expense_ref"])
            f["expense_revision_ref"] = cost.ref_id
            if cost.currency != f["currency"]:
                raise reject("Match within the expense currency.")
            used = sum(
                Decimal(r.fields["amount"])
                for r in others
                if r.kind == "activity_match"
                and r.fields.get("expense_ref") == cost.expense_ref
                and r.fields["role"] == role
            )
            if role == "purchase" and legacy_matches(self.session, self.owner).get(
                cost.expense_ref
            ):
                raise reject("The expense already has a full intake purchase match.")
            if used + amount > Decimal(cost.amount):
                raise reject("The allocations exceed the expense.")
        if b_ref := f.get("counterpart_ref"):
            b = self.activity_capacity(b_ref, amount, others)
            if (
                b.account_ref == a.account_ref
                or b.fields["currency"] != f["currency"]
                or Decimal(b.fields["amount"]) <= 0
                or b.fields["kind"] not in ("transfer", "unknown")
            ):
                raise reject("Select the incoming leg on a different owned account.")
        if role == "reimbursement":
            state = FinancialState(self.session)
            payment = f["payment_ref"]
            applied = sum(
                m["amount_nano"]
                for entry in state.entries
                if entry.kind == "cash" and entry.payment_ref == payment
                for m in state.matches_json(entry.ref_id)
                if m["source_key"] == "expense:" + f["expense_ref"]
                and m["currency"] == f["currency"]
            )
            used = sum(
                Decimal(r.fields["amount"])
                for r in others
                if r.kind == "activity_match"
                and r.fields.get("payment_ref") == payment
                and r.fields.get("expense_ref") == f["expense_ref"]
            )
            if used + amount > Decimal(applied).scaleb(-9):
                raise reject(
                    "Record and apply the reimbursement to this claim before matching its posting."
                )
        return [
            a.ref_id,
            role,
            f.get("expense_ref"),
            f.get("counterpart_ref"),
            f.get("payment_ref"),
        ]

    def recurring(self, f: dict[str, Any], others: list[FinancialReviewFact]) -> list[Any]:
        FundingService(self.session).head(f["account_ref"])
        if (
            Decimal(f["amount"]) <= 0
            or f.get("ends_on", f["first_expected_on"]) < f["first_expected_on"]
        ):
            raise reject("Use a positive expectation and a valid date range.")
        return [f["account_ref"], f["merchant"].casefold(), f["currency"], f["first_expected_on"]]

    def treatment(self, f: dict[str, Any], others: list[FinancialReviewFact]) -> list[Any]:
        cost = self.expense(f["expense_ref"])
        if f.get("engagement_ref"):
            WorkAttributionService(self.session).head(Engagement, f["engagement_ref"])
        if f.get("source_ref"):
            self.evidence(f["source_ref"])
        allocation, reviewed = Decimal(f["allocated_amount"]), f.get("reviewed_deduction")
        final = f["status"] in ("included", "excluded")
        if (reviewed is not None) != final or (reviewed and Decimal(reviewed) > allocation):
            raise reject("A final treatment needs an explicit deduction within the allocation.")
        if f["status"] == "excluded" and Decimal(reviewed or "0") != 0:
            raise reject("An excluded share has a zero reviewed deduction.")
        used = sum(
            Decimal(r.fields["allocated_amount"])
            for r in others
            if r.kind == "expense_treatment"
            and r.fields["expense_ref"] == cost.expense_ref
            and r.fields["jurisdiction"] == f["jurisdiction"]
        )
        if used + allocation > Decimal(cost.amount):
            raise reject("Activity allocations exceed this expense in the selected jurisdiction.")
        state = FinancialState(self.session)
        adjustments = [
            r.ref_id
            for r in others
            if r.kind == "activity_match"
            and r.fields.get("expense_ref") == cost.expense_ref
            and r.fields["role"] in ("refund", "reimbursement")
        ]
        settlements = [
            f"{entry.ref_id}/{m['amount_nano']}"
            for entry in state.entries
            if entry.kind == "cash"
            for m in state.matches_json(entry.ref_id)
            if m["source_key"] == "expense:" + cost.expense_ref
        ]
        f["adjustment_basis"] = sorted(adjustments + settlements)
        f["currency"], f["expense_revision_ref"] = cost.currency, cost.ref_id
        return [
            cost.expense_ref,
            f["jurisdiction"],
            f["tax_year"],
            f["activity"],
            f.get("engagement_ref"),
        ]

    def evidence(self, ref: str) -> AttachmentEvidence | FinancialIntake:
        row = self.session.get(FinancialIntake, ref)
        if row is not None:
            return FinancialIntakeService(self.session, get_settings()).owned(ref)
        return FundingService(self.session).source(ref)

    def tax(self, f: dict[str, Any], others: list[FinancialReviewFact]) -> list[Any]:
        amount = Decimal(f["amount"])
        if f.get("source_ref"):
            self.evidence(f["source_ref"])
        if bool(f.get("activity_ref")) == bool(f.get("deduction_ref")):
            raise reject("Select one posted activity or documented withholding entry.")
        if f.get("activity_ref"):
            a = self.activity_capacity(f["activity_ref"], amount, others)
            if f["kind"] == "withholding" or (Decimal(a.fields["amount"]) > 0) != (
                f["kind"] == "refund"
            ):
                raise reject(
                    "Use a paid-tax debit, a refund credit or supported payroll withholding."
                )
            f["currency"], f["posted_on"] = a.fields["currency"], a.fields["posted_on"]
        else:
            state = FinancialState(self.session)
            entry = state.by_ref.get(f["deduction_ref"])
            used = sum(
                Decimal(r.fields["amount"])
                for r in others
                if r.kind == "tax_allocation"
                and r.fields.get("deduction_ref") == f["deduction_ref"]
            )
            if (
                amount <= 0
                or f["kind"] != "withholding"
                or not entry
                or entry.kind != "deduction"
                or entry.deduction_kind != "withholding"
                or amount + used > Decimal(amount_text(state.effective[entry.ref_id]))
            ):
                raise reject("Select available documented withholding.")
            f["currency"] = entry.currency
        return [
            f.get("activity_ref") or f.get("deduction_ref"),
            f.get("tax_year"),
            f["jurisdiction"],
            f["tax_type"],
            f["kind"],
        ]

    def income(self, f: dict[str, Any], others: list[FinancialReviewFact]) -> list[Any]:
        WorkAttributionService(self.session).head(Engagement, f["engagement_ref"])
        self.evidence(f["source_ref"])
        if (
            f["period_end"] < f["period_start"]
            or ((f["coverage"] == "annual") != (f["document_kind"] in ("W-2", "1099")))
            or int(f["period_end"][:4]) != f["tax_year"]
        ):
            raise reject("Check the period, year and coverage of this evidence.")
        if f["coverage"] == "period":
            for row in others:
                if row.kind != "income_evidence" or row.fields["coverage"] != "period":
                    continue
                for name in ("payment_ref", "withholding_allocation_ref"):
                    if (
                        f.get(name)
                        and row.fields.get(name) == f[name]
                        and row.fields["jurisdiction"] == f["jurisdiction"]
                    ):
                        raise reject(
                            "This money is already attributed to another payroll period."
                        )
        if f.get("payment_ref"):
            payment = FinancialState(self.session).payments.get(f["payment_ref"])
            if (
                not payment
                or payment.currency != f["currency"]
                or payment.engagement_ref not in (None, f["engagement_ref"])
                or payment.category not in ("wages", "contract", "unknown")
            ):
                raise reject("Select an owned income payment in the same currency and engagement.")
        if ref := f.get("withholding_allocation_ref"):
            evidence = next(
                (r.fields for r in others if r.ref_id == ref and r.kind == "tax_allocation"), None
            )
            if (
                not evidence
                or evidence["kind"] != "withholding"
                or any(
                    evidence.get(k) != f.get(k)
                    for k in ("tax_year", "jurisdiction", "tax_type", "currency")
                )
                or Decimal(evidence["amount"]) != Decimal(f.get("withheld", "-1"))
            ):
                raise reject("The referenced withholding must cover this amount and designation.")
        return [
            f["engagement_ref"],
            f["jurisdiction"],
            f["tax_type"],
            f["document_kind"],
            f["coverage"],
            f["period_start"],
            f["period_end"],
        ]
