"""Small receipt and reconciliation operations; source matching is server-owned."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.compensation import Money
from docket.schemas.expenses import Currency, ReceiptRef
from docket.schemas.native_commands import CommandBase
from docket.schemas.work import EngagementRef, EntityRef

PaymentRef = Annotated[str, Field(pattern=r"^pay_[0-9A-HJKMNP-TV-Z]{26}$")]
ClaimTarget = Annotated[str, Field(pattern=r"^(fdoc|exp)_[0-9A-HJKMNP-TV-Z]{26}$")]
FinancialRef = Annotated[str, Field(pattern=r"^(pay|ftxn|fent|fdoc|exp)_[0-9A-HJKMNP-TV-Z]{26}$")]
PositiveMoney = Annotated[Money, Field(gt=0)]


class ApplyPaymentFields(StrictModel):
    target_ref: ClaimTarget
    amount: PositiveMoney | None = Field(
        default=None,
        description="Cash to apply; omit for the available amount up to the claim balance.",
    )
    deduction_amount: PositiveMoney | None = None
    deduction_kind: Literal["withholding", "fee", "other"] | None = None
    note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def deduction_pair(self) -> "ApplyPaymentFields":
        if (self.deduction_amount is None) != (self.deduction_kind is None):
            raise ValueError("deduction_amount and deduction_kind must be supplied together")
        return self


class ReceiptFields(StrictModel):
    amount: PositiveMoney
    currency: Currency
    received_at: datetime | None = None
    received_on: date | None = None
    engagement_ref: EngagementRef | None = None
    payer_ref: EntityRef | None = None
    category: Literal["unknown", "wages", "contract", "reimbursement", "other"] = "unknown"
    description: str | None = Field(default=None, max_length=4000)
    source_method: Literal["report", "import"] = "report"
    source_ref: ReceiptRef | None = None
    external_source: str | None = Field(default=None, min_length=1, max_length=128)
    external_id: str | None = Field(default=None, min_length=1, max_length=512)
    replaces_ref: PaymentRef | None = None
    target_ref: ClaimTarget | None = None
    deduction_amount: PositiveMoney | None = None
    deduction_kind: Literal["withholding", "fee", "other"] | None = None

    @model_validator(mode="after")
    def pairs(self) -> "ReceiptFields":
        if (self.external_source is None) != (self.external_id is None):
            raise ValueError("external_source and external_id must be supplied together")
        if (self.deduction_amount is None) != (self.deduction_kind is None):
            raise ValueError("deduction_amount and deduction_kind must be supplied together")
        if self.deduction_amount is not None and self.target_ref is None:
            raise ValueError("A deduction needs its claim target")
        return self


class RecordPayment(ReceiptFields, CommandBase):
    pass


class ReceiptSpec(ReceiptFields):
    evidence_gap: str | None = None
    received_offset_seconds: int | None = Field(default=None, ge=-86399, le=86399)


class PaymentAllocationFields(ApplyPaymentFields):
    payment_ref: PaymentRef


class ApplyPayment(PaymentAllocationFields, CommandBase):
    pass


class FinancialAdjustmentFields(StrictModel):
    target_ref: ClaimTarget
    currency: Currency | None = None
    direction: Literal["credit", "charge"]
    amount: PositiveMoney
    reason: str = Field(min_length=1, max_length=4000)


class AdjustFinancialClaim(FinancialAdjustmentFields, CommandBase):
    pass


class FinancialReversalFields(StrictModel):
    ref: Annotated[str, Field(pattern=r"^(pay|ftxn|fent)_[0-9A-HJKMNP-TV-Z]{26}$")]
    amount: PositiveMoney | None = None
    reason: str = Field(min_length=1, max_length=4000)


class ReverseFinancialRecord(FinancialReversalFields, CommandBase):
    pass


class GetFinancialContext(StrictModel):
    refs: list[FinancialRef] = Field(default_factory=list, max_length=25)
    engagement_ref: EngagementRef | None = None
    as_of: datetime | None = None
    format: Literal["summary", "json", "csv"] = "summary"
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=20, ge=1, le=100)


class SettlementSpec(StrictModel):
    command: Literal["receipt", "allocation", "credit", "charge", "reversal"]
    parameters: dict[str, object]
    basis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


SETTLEMENT_TOOL_PURPOSES = {
    "docket_record_payment": (
        "Record reported money actually received, with optional claim matching. "
        "Expected/promised payments are not receipts. Preserve evidence/date gaps and "
        "exact currency. External transaction identities deduplicate. No bank transfer "
        "or provider effect."
    ),
    "docket_apply_payment": (
        "Apply an existing receipt to an issued invoice, submitted timesheet or "
        "reported expense claim. Docket derives shared source matches and "
        "partial/unapplied balances. Only explicit deductions reconcile net/gross "
        "differences."
    ),
    "docket_adjust_financial_claim": (
        "Record an explicit credit or additional charge against a posted financial "
        "claim. Preserve original documents, source lineage and received money; "
        "overpayment becomes unapplied credit, not an invented refund."
    ),
    "docket_reverse_financial_record": (
        "Reverse all or part of a receipt or ledger record with retained history. "
        "Docket derives affected matches and balances; this bookkeeping sends no money "
        "and does not edit issued documents."
    ),
    "docket_get_financial_context": (
        "Read received, applied, credited, deducted, unapplied and outstanding money "
        "without counting shared invoice/timesheet sources twice. Includes due/overdue "
        "and reconciliation coverage; JSON/CSV continuation retains one basis. Gross "
        "claims are separate from receipt of money."
    ),
}
