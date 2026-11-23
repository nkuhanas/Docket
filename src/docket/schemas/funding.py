"""Non-secret funding identities and exact account activity inputs."""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.expenses import Currency, ReceiptRef
from docket.schemas.work import EntityRef


def metadata_label(value: str) -> str:
    if not value.strip() or re.search(
        r"(?:\d[ -]?){8}|(?:password|passwd|secret|bearer|api[ _-]?key|cv[vcd]|cvc|pin)\b"
        r"|https?://|[=:@/]|\b\d{3,4}\b|\S{36,}",
        value,
        re.I,
    ):
        raise ValueError("Use a non-secret display label and the dedicated masked suffix field.")
    return value.strip()


Label = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(metadata_label)]


def instrument_hint(value: str) -> str:
    # Permit a single explicitly masked suffix in a reported hint.
    remainder = re.sub(r"(?:ending(?: in)? |[*•x]{2,})[0-9]{4}$", "masked", value, flags=re.I)
    metadata_label(remainder)
    return value.strip()


InstrumentHint = Annotated[
    str, Field(min_length=1, max_length=120), AfterValidator(instrument_hint)
]
Suffix = Annotated[str, Field(pattern=r"^[0-9]{4}$")]
AccountRef = Annotated[str, Field(pattern=r"^fund_[0-9A-HJKMNP-TV-Z]{26}$")]
InstrumentRef = Annotated[str, Field(pattern=r"^card_[0-9A-HJKMNP-TV-Z]{26}$")]
ActivityRef = Annotated[str, Field(pattern=r"^actv_[0-9A-HJKMNP-TV-Z]{26}$")]


class IdentityFields(StrictModel):
    label: Label | None = None
    institution: Label | None = None
    masked_suffix: Suffix | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    owner_ref: EntityRef | None = None
    liable_party_ref: EntityRef | None = None

    @model_validator(mode="after")
    def dates(self) -> "IdentityFields":
        if (
            self.effective_from
            and self.effective_until
            and self.effective_from > self.effective_until
        ):
            raise ValueError("Effective end precedes start.")
        return self


class FundingAccountPatch(IdentityFields):
    account_type: Literal["credit", "checking", "savings", "cash", "other", "unknown"] | None = None
    currency: Currency | None = None
    status: Literal["active", "inactive", "closed", "superseded"] | None = None
    replaces_ref: AccountRef | None = None


class FundingAccountSpec(FundingAccountPatch):
    label: Label
    status: Literal["active", "inactive", "closed", "superseded"] = "active"


class InstrumentPatch(IdentityFields):
    account_ref: AccountRef | None = None
    method: Literal["credit_card", "debit_card", "bank", "cash", "other"] | None = None
    cardholder_ref: EntityRef | None = None
    role: Literal["primary", "authorized_user", "shared", "unknown"] | None = None
    status: Literal["active", "retired"] | None = None
    replaces_ref: InstrumentRef | None = None


class InstrumentSpec(InstrumentPatch):
    label: Label
    status: Literal["active", "retired"] = "active"


class ActivityFields(StrictModel):
    account_ref: AccountRef
    instrument_ref: InstrumentRef | None = None
    posted_on: date
    amount: str = Field(pattern=r"^-?(?:0|[1-9]\d{0,17})(?:\.\d{1,9})?$")
    currency: Currency
    description: str = Field(min_length=1, max_length=180)
    external_id: str | None = Field(default=None, min_length=1, max_length=96)
    state: Literal["pending", "posted", "reversed"] = "posted"
    kind: Literal["purchase", "refund", "transfer", "deposit", "unknown"] = "unknown"
    corrects_ref: ActivityRef | None = None


class StatementFields(StrictModel):
    account_ref: AccountRef
    source_ref: ReceiptRef
    instrument_ref: InstrumentRef | None = None
    period_start: date
    period_end: date
    opening_balance: Decimal | None = Field(default=None, max_digits=24, decimal_places=9)
    closing_balance: Decimal | None = Field(default=None, max_digits=24, decimal_places=9)
    balance_currency: Currency | None = None

    @model_validator(mode="after")
    def period(self) -> "StatementFields":
        if (
            self.opening_balance is not None or self.closing_balance is not None
        ) and not self.balance_currency:
            raise ValueError("Balance currency required")
        if self.period_end < self.period_start:
            raise ValueError("Statement period ends before it starts.")
        return self


class FundingContext(StrictModel):
    statement_offset: int = Field(default=0, ge=0, le=2000)
    refs: list[str] = Field(default_factory=list, max_length=12)
    after_ref: str | None = None
    account_ref: AccountRef | None = None
