"""Flat work commands and private, typed immutable work snapshots."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

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


class WorkAttributionFields(StrictModel):
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
    uncertainty: str | None = Field(default=None, max_length=2000)
    source_refs: list[Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list,
        max_length=25,
    )


class WorkPlacementFields(StrictModel):
    work_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None


class WorkMetadata(WorkAttributionFields, WorkPlacementFields):
    source_method: Literal["report", "import", "inference"] = "report"


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
    end_at: datetime | None = None
    start_offset_seconds: int
    end_offset_seconds: int | None = None


class WorkTimeSpec(StrictModel):
    precision: Literal["interval", "duration", "incomplete", "timer", "composite"]
    timezone: str = Field(min_length=1, max_length=128)
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_offset_seconds: int | None = None
    end_offset_seconds: int | None = None
    duration_us: int | None = Field(default=None, ge=0, le=2**63 - 1)
    segments: list[WorkSegmentSpec] = Field(default_factory=list, max_length=4097)
    component_revision_refs: list[
        Annotated[str, Field(pattern=r"^wrev_[0-9A-HJKMNP-TV-Z]{26}$")]
    ] = Field(default_factory=list, max_length=25)


class WorkSessionSpec(WorkAttributionFields, WorkPlacementFields):
    source_refs: list[Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list,
    )
    source_method: Literal["report", "import", "inference", "timer"] = "report"
    description: str = Field(min_length=1, max_length=4000)
    status: Literal["recorded", "retracted", "running", "paused"] = "recorded"
    time: WorkTimeSpec

    @model_validator(mode="after")
    def placement(self) -> "WorkSessionSpec":
        if bool(self.period_start) != bool(self.period_end):
            raise ValueError("period_start and exclusive period_end must be supplied together")
        if self.period_start and self.period_end and self.period_end <= self.period_start:
            raise ValueError("period_end must follow period_start")
        if self.work_date and self.period_start:
            raise ValueError("use work_date or a reporting period")
        if self.time.precision in {"interval", "composite"} and (
            self.work_date or self.period_start
        ):
            raise ValueError("bounded time establishes its own dates; omit work_date/period")
        return self


class StartWorkSession(CommandBase):
    description: str = Field(min_length=1, max_length=4000)
    timezone: str | None = Field(default=None, max_length=128)
    engagement_ref: EngagementRef | None = None
    organization_ref: EntityRef | None = None
    project_ref: EntityRef | None = None
    item_ref: Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    task_ref: Annotated[str, Field(pattern=r"^task_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    event_ref: Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    place_ref: EntityRef | None = None
    attribution_label: str | None = Field(default=None, max_length=512)
    category: str | None = Field(default=None, max_length=128)
    reported_compensation_class: Literal["billable", "paid_work", "unpaid", "unknown"] | None = None
    work_mode: Literal["remote", "onsite", "travel"] | None = None
    reported_location: str | None = Field(default=None, max_length=1000)


class WorkTimerTarget(CommandBase):
    work_ref: WorkRef


class StopWorkSession(WorkTimerTarget):
    end: str | None = Field(
        default=None,
        max_length=64,
        description="Reported past stop timestamp; omit to use retained request time.",
    )
    outcome: str | None = Field(default=None, max_length=4000)


class SwitchWorkSession(StartWorkSession):
    from_work_ref: WorkRef
    previous_outcome: str | None = Field(default=None, max_length=4000)


class WorkTimerStartSpec(StrictModel):
    effective_at: AwareDatetime
    snapshot: WorkSessionSpec


class WorkTimerTransitionSpec(WorkTimerStartSpec):
    transition: Literal["pause", "resume", "stop"]
    offset_seconds: int
    reported_end: bool = False


class WorkTimerSwitchSpec(WorkTimerStartSpec):
    previous_snapshot: WorkSessionSpec
    previous_offset_seconds: int


class WorkSplitPart(StrictModel):
    source_work_ref: WorkRef | None = Field(
        default=None, description="For composite work, select a returned source component."
    )
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    duration_minutes: Decimal | None = Field(default=None, gt=0)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    engagement_ref: EngagementRef | None = None
    organization_ref: EntityRef | None = None
    project_ref: EntityRef | None = None
    reported_compensation_class: Literal["billable", "paid_work", "unpaid", "unknown"] | None = None
    work_mode: Literal["remote", "onsite", "travel"] | None = None
    reported_location: str | None = Field(default=None, max_length=1000)
    work_date: date | None = None


class SplitWorkSession(CommandBase):
    work_ref: WorkRef
    parts: list[WorkSplitPart] = Field(min_length=2, max_length=25)


class MergeWorkSessions(WorkAttributionFields, CommandBase):
    work_refs: list[WorkRef] = Field(min_length=2, max_length=25)
    description: str | None = Field(default=None, min_length=1, max_length=4000)


class WorkLineageSpec(StrictModel):
    parent_refs: list[WorkRef] = Field(min_length=1, max_length=25)
    children: list[WorkSessionSpec] = Field(min_length=1, max_length=25)
    child_source_work_refs: list[WorkRef | None] = Field(default_factory=list, max_length=25)


class QueryWorkSessions(StrictModel):
    date_from: date | None = None
    date_to: date | None = Field(default=None, description="Exclusive; defaults to this week.")
    timezone: str | None = Field(default=None, max_length=128)
    engagement_ref: EngagementRef | None = None
    organization_ref: EntityRef | None = None
    project_ref: EntityRef | None = None
    reported_compensation_class: Literal["billable", "paid_work", "unpaid", "unknown"] | None = None
    include_unplaced: bool = True
    group_by: Literal["engagement", "organization", "project", "day", "week"] = "engagement"
    as_of: datetime | None = None
    format: Literal["summary", "json", "csv"] = "summary"
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)


class GetWorkContext(StrictModel):
    search: str | None = Field(default=None, min_length=1, max_length=128)
    refs: list[
        Annotated[
            str,
            Field(
                pattern=r"^(eng|work|ent|evt|item|task|aff|cpol|fdoc|exp)_[0-9A-HJKMNP-TV-Z]{26}$",
            ),
        ]
    ] = Field(default_factory=list, max_length=32)


WORK_TOOL_PURPOSES = {
    "docket_split_work_session": "Split recorded work into parts without changing total work. "
    "Use start/end or minutes; breaks are inherited. "
    "For composite work select a returned source_work_ref.",
    "docket_merge_work_sessions": "Merge recorded work, preserving quantities, gaps and history. "
    "Supply overrides for conflicting attribution; source evidence is retained internally.",
    "docket_start_work_session": "Start the primary timer at its retained request time. "
    "Preview and unchanged retries retain that exact instant; no calendar effect.",
    "docket_pause_work_session": "Pause the selected running timer at the retained request time.",
    "docket_resume_work_session": "Resume the selected paused timer at the retained request time.",
    "docket_stop_work_session": "Stop at retained request time or a reported past end. "
    "An outcome is optional.",
    "docket_switch_work_session": "Stop from_work_ref and start new work atomically.",
    "docket_query_work_sessions": "Summarize actual work over the full matching set. "
    "JSON/CSV exports share the retained basis; concatenate content chunks in order. "
    "Reuse report_cursor to restart that basis in another format. "
    "Unplaced quantities and overlapping time are not verified hours. Read-only; no submission.",
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
