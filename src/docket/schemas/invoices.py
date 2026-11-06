"""Flat invoice intents; source selection, valuation and claims remain internal."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

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


class PrepareInvoice(InvoiceFields, CommandBase):
    engagement_ref: EngagementRef
    component_key: ComponentKey = "base"
    amends_ref: DocumentRef | None = None


class RefreshInvoice(InvoiceFields, CommandBase):
    invoice_ref: DocumentRef


class UpdateInvoice(CommandBase):
    invoice_ref: DocumentRef
    status: Literal["issued", "cancelled", "delivered"]
    note: str | None = Field(default=None, max_length=4000)
    observed_at: datetime | None = Field(
        default=None,
        description="Reported delivery time; omit unknown. Issue uses a retained server instant.",
    )


class InvoiceSpec(StrictModel):
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
    status: Literal["issued", "cancelled", "delivered"]
    note: str | None = None
    observed_at: datetime | None = None
    issued_at: datetime | None = None


INVOICE_TOOL_PURPOSES = {
    "docket_prepare_invoice": "Draft an engagement's uninvoiced work and optional expenses. "
    "Dates/selection/maximum minutes and invoice terms are ordinary flat fields. "
    "A canonical draft reserves its exact remaining quantities; preview reserves nothing. "
    "Unknown rates stay unresolved. Does not issue, send or record payment.",
    "docket_refresh_invoice": "Regenerate a draft invoice from current work/terms and supplied "
    "header or selection changes; omitted fields retain prior choices. Releases/replaces "
    "reservations atomically. Issued snapshots cannot be refreshed.",
    "docket_update_invoice": "Issue or cancel a draft invoice, or record reported manual "
    "delivery of an issued invoice. Issue retains a unique number and exact monetary snapshot. "
    "Delivered is an observation, not provider success. No transmission or payment effect.",
}
