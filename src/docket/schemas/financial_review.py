"""Ordinary finance inputs. All amounts are exact decimal strings in the named currency."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.expenses import Currency, ExpenseRef, ReceiptRef
from docket.schemas.funding import AccountRef, ActivityRef
from docket.schemas.settlement import PaymentRef
from docket.schemas.work import EngagementRef

Amount = Annotated[str, Field(pattern=r"^(?:0|[1-9]\d{0,17})(?:\.\d{1,9})?$")]
Year = Annotated[int, Field(ge=1900, le=2199)]
Label = Annotated[str, Field(min_length=1, max_length=120)]
FactRef = Annotated[str, Field(pattern=r"^finrev_[0-9A-HJKMNP-TV-Z]{26}$")]
YearReportRef = Annotated[str, Field(pattern=r"^frep_[0-9A-HJKMNP-TV-Z]{26}$")]
Jurisdiction = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]{1,23}$")]


class Revision(StrictModel):
    prior_ref: FactRef | None = Field(
        default=None, description="Exact previous revision for a correction."
    )


class ActivityMatch(Revision):
    activity_ref: ActivityRef
    role: Literal["purchase", "refund", "transfer", "reimbursement"]
    amount: Amount
    expense_ref: ExpenseRef | None = None
    counterpart_ref: ActivityRef | None = None
    payment_ref: PaymentRef | None = None

    @model_validator(mode="after")
    def targets(self) -> "ActivityMatch":
        if self.role != "transfer" and self.expense_ref is None:
            raise ValueError("Select the existing expense.")
        if (self.role == "reimbursement") != bool(self.payment_ref):
            raise ValueError("Select a posted reimbursement payment only for reimbursement.")
        if self.role != "transfer" and self.counterpart_ref:
            raise ValueError("Only transfers can pair posting legs.")
        if self.role == "transfer" and not (self.expense_ref or self.counterpart_ref):
            raise ValueError("Select the related expense or counterpart.")
        return self


class RecurringExpense(Revision):
    account_ref: AccountRef
    merchant: Label
    first_expected_on: date
    cadence: Literal["monthly", "yearly"]
    amount: Amount
    currency: Currency
    ends_on: date | None = None


class ExpenseTreatment(Revision):
    expense_ref: ExpenseRef
    tax_year: Year
    jurisdiction: Jurisdiction
    activity: Literal["contract", "employee", "personal", "asset", "other"]
    engagement_ref: EngagementRef | None = None
    allocated_amount: Amount
    reviewed_deduction: Amount | None = None
    status: Literal["needs_evidence", "asset_review", "included", "excluded"]
    rationale: str = Field(min_length=1, max_length=500)
    source_ref: ReceiptRef | None = None


class TaxAllocation(Revision):
    tax_year: Year | None = None
    jurisdiction: Jurisdiction
    tax_type: Label
    kind: Literal["estimated", "balance", "refund", "withholding"]
    amount: Amount
    activity_ref: ActivityRef | None = None
    deduction_ref: str | None = Field(default=None, pattern=r"^fent_[0-9A-HJKMNP-TV-Z]{26}$")
    source_ref: ReceiptRef | None = None


class IncomeEvidence(Revision):
    engagement_ref: EngagementRef
    tax_year: Year
    jurisdiction: Jurisdiction
    tax_type: Label = "income"
    document_kind: Literal["paystub", "W-2", "1099"]
    period_start: date
    period_end: date
    source_ref: ReceiptRef
    currency: Currency
    gross: Amount | None = Field(
        default=None, description="Gross pay or documented gross nonemployee compensation."
    )
    taxable_wages: Amount | None = Field(
        default=None, description="Reported taxable wages for this jurisdiction."
    )
    withheld: Amount | None = None
    net: Amount | None = None
    payment_ref: PaymentRef | None = None
    withholding_allocation_ref: FactRef | None = Field(
        default=None, description="Previously documented settlement withholding for this period."
    )
    coverage: Literal["period", "year_to_date", "annual"]

    @model_validator(mode="after")
    def quantities(self) -> "IncomeEvidence":
        if all(
            value is None for value in (self.gross, self.taxable_wages, self.withheld, self.net)
        ):
            raise ValueError("Supply at least one documented income or withholding amount.")
        return self


class WithdrawFact(StrictModel):
    prior_ref: FactRef
    reason: Label


class AnnualReview(StrictModel):
    report_ref: YearReportRef


class ReadFinancialYear(StrictModel):
    tax_year: Year
    refs: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="Exact owned inputs to observe for a command.",
    )
    report_ref: YearReportRef | None = None
    section: Literal[
        "decisions", "expenses", "allocations", "income", "totals", "evidence", "recurrence"
    ] = "decisions"
    offset: int = Field(default=0, ge=0)
    format: Literal["json", "csv", "json_text"] = "json"
