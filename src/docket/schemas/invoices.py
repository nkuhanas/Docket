"""Flat invoice intents; source selection, valuation and claims remain internal."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.compensation import ComponentKey
from docket.schemas.expenses import Currency, ExpenseRef
from docket.schemas.financial_documents import DocumentRef, EvaluationRef
from docket.schemas.native_commands import CommandBase
from docket.schemas.work import EngagementRef, WorkRef


class InvoiceFields(StrictModel):
    date_from: date | None = None
    date_to: date | None = Field(default=None, description="Exclusive; default through today.")
    timezone: str | None = Field(default=None, max_length=128)
    currency: Currency | None = None
    money_decimals: int | None = Field(default=None, ge=0, le=9)
    work_refs: list[WorkRef] | None = Field(default=None, max_length=50)
    expense_refs: list[ExpenseRef] | None = Field(
        default=None, max_length=50, description="Omit for related dated costs; [] excludes costs."
    )
    max_work_minutes: Annotated[Decimal, Field(gt=0)] | None = None
    issuer_label: str | None = Field(default=None, min_length=1, max_length=512)
    client_label: str | None = Field(default=None, min_length=1, max_length=512)
    payment_terms: str | None = Field(default=None, max_length=1000)
    due_on: date | None = None
    number_series: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
    notes: str | None = Field(default=None, max_length=4000)


InvoiceFormat = Literal["json", "csv", "markdown"]


class PrepareInvoice(InvoiceFields, CommandBase):
    format: InvoiceFormat | None = None
    engagement_ref: EngagementRef
    component_key: ComponentKey = "base"
    amends_ref: DocumentRef | None = None


class RefreshInvoice(InvoiceFields, CommandBase):
    invoice_ref: DocumentRef


class UpdateInvoice(CommandBase):
    format: InvoiceFormat | None = None
    invoice_ref: DocumentRef
    status: Literal["draft", "issued", "cancelled", "delivered"]
    note: str | None = Field(default=None, max_length=4000)
    observed_at: datetime | None = Field(
        default=None,
        description="Reported delivery time; omit unknown. Issue uses a retained server instant.",
    )

    @model_validator(mode="after")
    def final_export(self) -> "UpdateInvoice":
        if self.format and self.status != "issued":
            raise ValueError("A final export requires status=issued.")
        return self


class InvoiceSpec(StrictModel):
    format: InvoiceFormat | None = None
    issue_at: datetime | None = None
    document_key: str = Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")
    engagement_ref: EngagementRef
    component_key: ComponentKey = "base"
    evaluation_ref: EvaluationRef
    parameters: InvoiceFields
    expense_revisions: dict[ExpenseRef, str] = Field(default_factory=dict)
    financial_basis: str = Field(pattern=r"^[a-f0-9]{64}$")
    amends_ref: DocumentRef | None = None

    @property
    def document_ref(self) -> str:
        return "fdoc_" + self.document_key


class InvoiceStatusSpec(StrictModel):
    format: InvoiceFormat | None = None
    status: Literal["draft", "issued", "cancelled", "delivered"]
    note: str | None = None
    observed_at: datetime | None = None
    issued_at: datetime | None = None


INVOICE_TOOL_PURPOSES = {
    "docket_prepare_invoice": "Prepare an invoice for remaining billable work and expenses. "
    "Set format to issue and export JSON, CSV or Markdown. Omit format for a draft. "
    "Preview reserves nothing. Follow the returned export cursor.",
    "docket_refresh_invoice": "Edit a draft invoice using current work and terms. Omitted "
    "fields retain prior choices. Reopen an issued invoice before editing.",
    "docket_update_invoice": "Set issued or draft idempotently. Optional format on issue "
    "returns a final export. Reopening retains the number and last issued balance. "
    "Cancel an unissued draft or record reported delivery independently.",
}
