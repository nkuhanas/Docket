"""Small operational commands and private retained exception snapshots."""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.common import PublicRef, StrictModel
from docket.schemas.native_commands import CommandBase
from docket.schemas.workload import ExceptionKind

OperationalSubject = Annotated[
    str, Field(pattern=r"^(work|eng|fdoc|exp|pay)_[0-9A-HJKMNP-TV-Z]{26}$")
]


class ScheduleFollowup(CommandBase):
    subject_ref: OperationalSubject
    at: str = Field(max_length=64, description="ISO timestamp with UTC offset.")
    timezone: str | None = Field(default=None, max_length=128)
    label: str | None = Field(default=None, max_length=512)


class ReconcileWorkload(CommandBase):
    date_from: date | None = None
    date_to: date | None = Field(default=None, description="Exclusive; defaults to next week.")
    timezone: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=25, ge=1, le=100)


class OperationalReminderSpec(StrictModel):
    subject_ref: OperationalSubject
    trigger_at: datetime
    timezone: str
    label: str | None = None
    subject_change_id: None = None
    delivery_channels: list[Literal["docket_queue"]] = Field(
        default=["docket_queue"], min_length=1, max_length=1
    )
    lead_seconds: list[Literal[0]] = Field(default=[0], min_length=1, max_length=1)
    canonical_status: Literal["active"] = "active"

    @field_validator("trigger_at")
    @classmethod
    def exact_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("trigger_at requires a UTC offset")
        return value


class WorkloadException(StrictModel):
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    category: ExceptionKind
    subject_ref: PublicRef
    related_refs: list[PublicRef] = Field(default_factory=list, max_length=10)
    summary: str = Field(max_length=512)
    basis_versions: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)


class WorkloadReconciliationSpec(StrictModel):
    as_of: datetime
    date_from: date
    date_to: date
    timezone: str
    exceptions: list[WorkloadException] = Field(max_length=100)
    coverage: list[str] = Field(default_factory=list, max_length=25)
    preference_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    notifications_enabled: bool = True
    suppressed_exceptions: list[ExceptionKind] = Field(default_factory=list)
    cooldown_hours: int = Field(default=168, ge=1, le=8760)

    @model_validator(mode="after")
    def exact_period_and_identity(self) -> "WorkloadReconciliationSpec":
        if self.as_of.tzinfo is None or not 0 < (self.date_to - self.date_from).days <= 366:
            raise ValueError("Use an offset as_of and a positive period of at most 366 days")
        if len({row.key for row in self.exceptions}) != len(self.exceptions):
            raise ValueError("Exceptions require distinct retained identities")
        return self


FOLLOWUP_TOOL_PURPOSES = {
    "docket_schedule_followup": "Schedule one inbox reminder for selected work, engagement, "
    "document, expense or payment at an explicit instant. Creates no Calendar event or "
    "financial/provider action. Retract the returned reminder to cancel.",
    "docket_reconcile_workload": "Review a bounded period for forgotten timers, overlaps, "
    "unclassified work, overdue tasks/money and capacity conflicts. Retain nonbinding attention "
    "cases and deduplicated inbox alerts under current preferences. Never creates actual work "
    "or payment. The connected client owns scheduling.",
}
