"""Flat work commands and private, typed immutable work snapshots."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.native_commands import CommandBase

EntityRef = Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]
EngagementRef = Annotated[str, Field(pattern=r"^eng_[0-9A-HJKMNP-TV-Z]{26}$")]
WorkRef = Annotated[str, Field(pattern=r"^work_[0-9A-HJKMNP-TV-Z]{26}$")]


class EngagementFields(StrictModel):
    organization_ref: EntityRef | None = None
    payer_ref: EntityRef | None = None
    affiliation_ref: Annotated[str, Field(pattern=r"^aff_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    effective_start: date | None = None
    effective_end: date | None = None
    role: str | None = Field(default=None, max_length=512)
    reporting_notes: str | None = Field(default=None, max_length=4000)


class EngagementSpec(EngagementFields):
    label: str = Field(min_length=1, max_length=512)
    kind: Literal["unknown", "employment", "contract", "research", "volunteer", "personal"] = (
        "unknown"
    )

    @model_validator(mode="after")
    def ordered_dates(self) -> "EngagementSpec":
        if (
            self.effective_start
            and self.effective_end
            and self.effective_end < self.effective_start
        ):
            raise ValueError("effective_end must not precede effective_start")
        return self


class CreateEngagement(EngagementSpec, CommandBase):
    pass


class UpdateEngagement(EngagementFields, CommandBase):
    engagement_ref: EngagementRef
    label: str | None = Field(default=None, min_length=1, max_length=512)
    kind: (
        Literal["unknown", "employment", "contract", "research", "volunteer", "personal"] | None
    ) = None

    @field_validator("label", "kind")
    @classmethod
    def required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged required field; it cannot be cleared")
        return value


class WorkMetadata(StrictModel):
    outcome: str | None = Field(default=None, max_length=4000)
    category: str | None = Field(default=None, max_length=128)
    organization_ref: EntityRef | None = None
    engagement_ref: EngagementRef | None = None
    project_ref: EntityRef | None = None
    item_ref: Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    task_ref: Annotated[str, Field(pattern=r"^task_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    event_ref: Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    place_ref: EntityRef | None = None
    attribution_label: str | None = Field(default=None, max_length=512)
    reported_compensation_class: Literal["billable", "paid_work", "unpaid", "unknown"] | None = None
    work_mode: Literal["remote", "onsite", "travel"] | None = None
    reported_location: str | None = Field(default=None, max_length=1000)
    source_method: Literal["report", "import", "inference"] = "report"
    uncertainty: str | None = Field(default=None, max_length=2000)
    source_refs: list[Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list,
        max_length=25,
    )
    work_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None


class WorkBreak(StrictModel):
    start: str = Field(max_length=64)
    end: str = Field(max_length=64)


class WorkTimeFields(StrictModel):
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=128)
    duration_minutes: Decimal | None = Field(default=None, gt=0)
    breaks: list[WorkBreak] | None = Field(default=None, max_length=25)


class LogWorkSession(WorkMetadata, WorkTimeFields, CommandBase):
    description: str = Field(min_length=1, max_length=4000)


class UpdateWorkSession(WorkMetadata, WorkTimeFields, CommandBase):
    work_ref: WorkRef
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    status: Literal["recorded", "retracted"] | None = None

    @field_validator("description", "timezone", "source_method", "source_refs", "status")
    @classmethod
    def required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged required field; it cannot be cleared")
        return value


class WorkSegmentSpec(StrictModel):
    kind: Literal["active", "break"]
    start_at: datetime
    end_at: datetime
    start_offset_seconds: int
    end_offset_seconds: int


class WorkTimeSpec(StrictModel):
    precision: Literal["interval", "duration", "incomplete"]
    timezone: str = Field(min_length=1, max_length=128)
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_offset_seconds: int | None = None
    end_offset_seconds: int | None = None
    duration_us: int | None = Field(default=None, ge=0, le=2**63 - 1)
    segments: list[WorkSegmentSpec] = Field(default_factory=list, max_length=51)


class WorkSessionSpec(WorkMetadata):
    description: str = Field(min_length=1, max_length=4000)
    status: Literal["recorded", "retracted"] = "recorded"
    time: WorkTimeSpec

    @model_validator(mode="after")
    def placement(self) -> "WorkSessionSpec":
        if bool(self.period_start) != bool(self.period_end):
            raise ValueError("period_start and exclusive period_end must be supplied together")
        if self.period_start and self.period_end and self.period_end <= self.period_start:
            raise ValueError("period_end must follow period_start")
        if self.work_date and self.period_start:
            raise ValueError("use work_date or a reporting period")
        if self.time.precision == "interval" and (self.work_date or self.period_start):
            raise ValueError("bounded time establishes its own dates; omit work_date/period")
        return self


class GetWorkContext(StrictModel):
    search: str | None = Field(default=None, min_length=1, max_length=128)
    refs: list[
        Annotated[
            str,
            Field(
                pattern=r"^(eng|work|ent|evt|item|task|aff)_[0-9A-HJKMNP-TV-Z]{26}$",
            ),
        ]
    ] = Field(default_factory=list, max_length=10)


WORK_TOOL_PURPOSES = {
    "docket_get_work_context": "Read work, engagement and attribution choices with versions.",
    "docket_create_engagement": "Record an arrangement without requiring payer or pay terms.",
    "docket_update_engagement": "Correct supplied engagement fields; null clears optional values.",
    "docket_log_work_session": "Record actual work with ISO bounds, minutes, or incomplete time. "
    "Use work_date or exclusive period_start/period_end only for unbounded reports. "
    "Reported paid_work is not payment. No calendar/provider effect.",
    "docket_update_work_session": "Correct supplied work fields; null clears optional values. "
    "duration_minutes selects duration-only time; start/end select interval time. "
    "status=retracted retains history. No calendar/provider effect.",
}
