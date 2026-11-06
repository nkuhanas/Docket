"""Flat policy instructions and private normalized effective terms."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.native_commands import CommandBase
from docket.schemas.work import EngagementRef, EntityRef

ComponentKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=9, allow_inf_nan=False)]
Minutes = Annotated[Decimal, Field(gt=0, le=10080, decimal_places=6, allow_inf_nan=False)]
Hours = Annotated[Decimal, Field(gt=0, le=168, decimal_places=6, allow_inf_nan=False)]


class EffectivePolicyFields(StrictModel):
    effective_from: date
    effective_to: date | None = Field(
        default=None, description="Exclusive; omit for open-ended terms."
    )
    timezone: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def date_order(self) -> "EffectivePolicyFields":
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must follow effective_from")
        return self


class PayTermsFields(StrictModel):
    basis: Literal["unknown", "hourly", "salary", "fixed_fee", "unpaid"] | None = None
    rate: Money | None = Field(
        default=None, description="Hourly rate; null records an unknown rate."
    )
    amount: Money | None = Field(
        default=None, description="Configured salary or fixed fee, not hourly earnings."
    )
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    payer_ref: EntityRef | None = None
    compensable: bool | None = None
    billable: bool | None = None
    money_decimals: int | None = Field(default=None, ge=0, le=9)

    @field_validator("basis", "money_decimals")
    @classmethod
    def required_when_supplied(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged required value")
        return value


class SetPayTerms(PayTermsFields, EffectivePolicyFields, CommandBase):
    engagement_ref: EngagementRef
    component_key: ComponentKey = "base"


class ReportingRulesFields(StrictModel):
    week_starts_on: int | None = Field(default=None, ge=0, le=6, description="Monday=0.")
    period_days: int | None = Field(default=None, ge=1, le=366)
    anchor_date: date | None = None
    rounding_minutes: Minutes | None = None
    rounding_scope: Literal["segment", "session", "day", "period"] | None = None
    rounding_mode: Literal["nearest", "up", "down"] | None = None
    hour_limit_week: Hours | None = None
    overtime_after_hours: Hours | None = None
    overtime_multiplier: Annotated[Decimal, Field(ge=1, le=100, decimal_places=6)] | None = None
    overtime_scope: Literal["day", "week"] | None = None
    group_with_payer: bool | None = None

    @field_validator(
        "week_starts_on", "rounding_scope", "rounding_mode", "overtime_scope", "group_with_payer"
    )
    @classmethod
    def required_when_supplied(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged required value")
        return value


class SetReportingRules(ReportingRulesFields, EffectivePolicyFields, CommandBase):
    engagement_ref: EngagementRef


class PolicyTermSpec(StrictModel):
    kind: Literal["pay", "reporting"]
    start_at: datetime
    end_at: datetime | None = None
    timezone: str
    basis: Literal["unknown", "hourly", "salary", "fixed_fee", "unpaid"] | None = None
    rate: Money | None = None
    amount: Money | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    payer_ref: EntityRef | None = None
    compensable: bool | None = None
    billable: bool | None = None
    money_decimals: int | None = Field(default=None, ge=0, le=9)
    week_starts_on: int | None = Field(default=None, ge=0, le=6)
    period_days: int | None = Field(default=None, ge=1, le=366)
    anchor_date: date | None = None
    rounding_minutes: Minutes | None = None
    rounding_scope: Literal["segment", "session", "day", "period"] | None = None
    rounding_mode: Literal["nearest", "up", "down"] | None = None
    hour_limit_week: Hours | None = None
    overtime_after_hours: Hours | None = None
    overtime_multiplier: Annotated[Decimal, Field(ge=1, le=100, decimal_places=6)] | None = None
    overtime_scope: Literal["day", "week"] | None = None
    group_with_payer: bool | None = None

    @model_validator(mode="after")
    def consistent_terms(self) -> "PolicyTermSpec":
        if self.start_at.tzinfo is None or (self.end_at and self.end_at.tzinfo is None):
            raise ValueError("policy boundaries must be offset timestamps")
        if self.end_at and self.end_at <= self.start_at:
            raise ValueError("policy window must be positive")
        pay_fields = set(PayTermsFields.model_fields)
        reporting_fields = set(ReportingRulesFields.model_fields)
        wrong = reporting_fields if self.kind == "pay" else pay_fields
        if any(getattr(self, key) is not None for key in wrong):
            raise ValueError("policy values must match the selected domain")
        if self.kind == "pay":
            if self.basis is None or self.money_decimals is None:
                raise ValueError("pay basis and monetary precision are required")
            if self.basis != "hourly" and self.rate is not None:
                raise ValueError("rate applies to hourly terms only")
            if self.basis not in {"salary", "fixed_fee"} and self.amount is not None:
                raise ValueError("amount applies to salary or fixed-fee terms only")
        else:
            if (self.period_days is None) != (self.anchor_date is None):
                raise ValueError("period_days and anchor_date must be set or cleared together")
            if (self.overtime_after_hours is None) != (self.overtime_multiplier is None):
                raise ValueError(
                    "overtime threshold and multiplier must be set or cleared together"
                )
            if any(
                getattr(self, key) is None
                for key in (
                    "week_starts_on",
                    "rounding_scope",
                    "rounding_mode",
                    "overtime_scope",
                    "group_with_payer",
                )
            ):
                raise ValueError("reporting rule defaults are required")
        return self


class PolicySetSpec(StrictModel):
    component_key: ComponentKey = "base"
    term: PolicyTermSpec
    defaults: PolicyTermSpec
    changed_fields: list[str] = Field(max_length=20)


POLICY_TOOL_PURPOSES = {
    "docket_set_pay_terms": "Set effective pay terms for an engagement. "
    "Omitted fields retain applicable terms. "
    "rate is hourly; amount is salary/fixed fee. No work, claim, payment or provider effect.",
    "docket_set_reporting_rules": "Set effective reporting, rounding and hour/overtime rules. "
    "Omitted fields retain applicable rules. Employer grouping is explicit. "
    "No legal rules are inferred.",
}
