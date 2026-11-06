"""Flat reported expense instructions; observations are not reimbursements received."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from docket.schemas.common import StrictModel
from docket.schemas.compensation import Money
from docket.schemas.native_commands import CommandBase
from docket.schemas.work import EngagementRef, EntityRef

ExpenseRef = Annotated[str, Field(pattern=r"^exp_[0-9A-HJKMNP-TV-Z]{26}$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
ReceiptRef = Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]


class ExpenseFields(StrictModel):
    amount: Money | SkipJsonSchema[None] = None
    currency: Currency | SkipJsonSchema[None] = None
    incurred_on: date | None = None
    description: str | SkipJsonSchema[None] = Field(default=None, min_length=1, max_length=4000)
    vendor: str | None = Field(default=None, max_length=512)
    category: str | None = Field(default=None, max_length=128)
    engagement_ref: EngagementRef | None = None
    organization_ref: EntityRef | None = None
    project_ref: EntityRef | None = None
    payer_ref: EntityRef | None = None
    attribution_label: str | None = Field(default=None, max_length=512)
    reimbursable: bool | None = None
    reimbursable_amount: Money | None = None
    receipt_ref: ReceiptRef | None = None
    receipt_required: bool | None = None
    receipt_note: str | None = Field(default=None, max_length=1000)
    ownership: Literal["unknown", "operator", "organization", "shared"] | None = None
    owner_ref: EntityRef | None = None
    source_method: Literal["report", "import", "inference"] | None = None
    uncertainty: str | None = Field(default=None, max_length=1000)

    @field_validator("amount", "currency", "description")
    @classmethod
    def required_when_supplied(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged required value")
        return value


class ExpenseReportingFields(StrictModel):
    submission_status: Literal["submitted", "approved", "rejected"] | SkipJsonSchema[None] = None
    claim_amount: Money | None = Field(
        default=None, description="Reported submitted/approved amount; unknown stays unknown."
    )
    observed_at: datetime | None = Field(
        default=None, description="Reported decision/submission time; omit when unknown."
    )

    @field_validator("submission_status")
    @classmethod
    def nonnull_status(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged status")
        return value

    @model_validator(mode="after")
    def observation_required(self) -> "ExpenseReportingFields":
        if "claim_amount" in self.model_fields_set and self.submission_status is None:
            raise ValueError("claim_amount needs submission_status")
        if "observed_at" in self.model_fields_set and self.submission_status is None:
            raise ValueError("observed_at needs submission_status")
        return self


class RecordExpense(ExpenseFields, ExpenseReportingFields, CommandBase):
    amount: Money
    currency: Currency
    description: str = Field(min_length=1, max_length=4000)
    external_source: str | None = Field(default=None, min_length=1, max_length=128)
    external_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def external_pair(self) -> "RecordExpense":
        if (self.external_source is None) != (self.external_id is None):
            raise ValueError("external_source and external_id must be supplied together")
        return self


class UpdateExpense(ExpenseFields, ExpenseReportingFields, CommandBase):
    expense_ref: ExpenseRef
    retracted: bool | SkipJsonSchema[None] = None

    @field_validator("retracted")
    @classmethod
    def nonnull_retraction(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged retraction state")
        return value


class ExpenseSpec(ExpenseFields):
    amount: Money
    currency: Currency
    description: str = Field(min_length=1, max_length=4000)
    receipt_required: bool = False
    receipt_status: Literal["not_provided", "available", "unavailable"] = "not_provided"
    ownership: Literal["unknown", "operator", "organization", "shared"] = "unknown"
    source_method: Literal["report", "import", "inference"] = "report"
    retracted: bool = False
    external_source: str | None = None
    external_id: str | None = None
    submission_status: Literal["submitted", "approved", "rejected"] | None = None
    claim_amount: Money | None = None
    claim_amount_supplied: bool = False
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def monetary_bounds(self) -> "ExpenseSpec":
        if self.reimbursable_amount is not None:
            if self.reimbursable_amount > self.amount:
                raise ValueError("reimbursable_amount cannot exceed cost")
            if self.reimbursable is False and self.reimbursable_amount:
                raise ValueError("an unreimbursable expense cannot have a positive eligible amount")
        if self.claim_amount is not None and self.claim_amount > self.amount:
            raise ValueError("claim_amount cannot exceed cost")
        return self


class QueryExpenses(StrictModel):
    date_from: date | None = None
    date_to: date | None = Field(
        default=None, description="Exclusive; defaults to the current month."
    )
    engagement_ref: EngagementRef | None = None
    organization_ref: EntityRef | None = None
    project_ref: EntityRef | None = None
    currency: Currency | None = None
    reimbursable: bool | None = None
    submission_status: Literal["not_submitted", "submitted", "approved", "rejected"] | None = None
    include_undated: bool = True
    as_of: datetime | None = None
    cursor: str | None = Field(default=None, max_length=512)
    format: Literal["summary", "json", "csv"] = "summary"
    limit: int = Field(default=25, ge=1, le=100)


EXPENSE_TOOL_PURPOSES = {
    "docket_record_expense": "Record a reported cost and optional reimbursement/receipt context. "
    "Missing receipts/dates/attribution do not block recording. Stable external IDs deduplicate; "
    "similar date/amount/vendor only suggests a duplicate. No payment or provider effect.",
    "docket_update_expense": "Correct costs or record reported submission/approval/rejection. "
    "Earlier cost/approval snapshots remain immutable. An approval is not money received; "
    "a cost correction does not silently change an earlier approved amount.",
    "docket_query_expenses": "Query reported expenses and exact full-set currency totals. "
    "Distinguish undated costs, unknown eligibility and retained submitted/approved amounts. "
    "JSON/CSV continuation preserves one captured basis. No payment inference.",
}
