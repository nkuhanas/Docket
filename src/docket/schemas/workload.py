"""Small planning commands; exact schedules and calculations stay server-owned."""

from datetime import date, time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.native_commands import CommandBase
from docket.schemas.work import EntityRef

ExceptionKind = Literal[
    "open_timer",
    "work_overlap",
    "unclassified_work",
    "reporting_cutoff",
    "overdue_task",
    "outstanding_money",
    "reconciliation",
    "capacity_conflict",
]


LocalClock = Annotated[
    str,
    Field(
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?$",
        description="Local HH:MM or HH:MM:SS; no UTC offset.",
    ),
]


class WorkloadPreferenceCommon(StrictModel):
    timezone: str | None = Field(default=None, max_length=128)
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(
        default=None, max_length=7, description="Monday=0; unknown when unset."
    )
    travel_buffer_minutes: int | None = Field(default=None, ge=0, le=1440)
    notifications_enabled: bool | None = None
    suppressed_exceptions: list[ExceptionKind] | None = Field(default=None, max_length=8)
    notification_cooldown_hours: int | None = Field(default=None, ge=1, le=8760)


class SetWorkloadPreferences(WorkloadPreferenceCommon, CommandBase):
    available_start: LocalClock | None = None
    available_end: LocalClock | None = None
    protected_start: LocalClock | None = None
    protected_end: LocalClock | None = None


class WorkloadPreferenceSpec(WorkloadPreferenceCommon):
    available_start: time | None = None
    available_end: time | None = None
    protected_start: time | None = None
    protected_end: time | None = None

    @model_validator(mode="after")
    def coherent_windows(self) -> "WorkloadPreferenceSpec":
        for first, last in (
            (self.available_start, self.available_end),
            (self.protected_start, self.protected_end),
        ):
            if (first is None) != (last is None):
                raise ValueError("Supply both ends of a local clock window, or clear both")
            if (
                first is not None
                and last is not None
                and (first.tzinfo or last.tzinfo or first == last)
            ):
                raise ValueError("Use different local clock times without offsets")
        if self.weekdays is not None and len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must be distinct")
        return self


class ProjectBudgetSpec(StrictModel):
    project_ref: EntityRef
    weekly_hours: Annotated[Decimal, Field(ge=0, le=168, decimal_places=6, allow_inf_nan=False)]
    timezone: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=1000)


class SetProjectBudget(ProjectBudgetSpec, CommandBase):
    pass


class GetWorkloadContext(StrictModel):
    date_from: date | None = None
    date_to: date | None = Field(default=None, description="Exclusive; defaults to one week.")
    timezone: str | None = Field(default=None, max_length=128)
    project_ref: EntityRef | None = None
    limit: int = Field(default=20, ge=1, le=100)


WORKLOAD_TOOL_PURPOSES = {
    "docket_get_workload_context": "Read commitments, deadlines, actual work, outcomes, "
    "configured limits and potential capacity with explicit coverage. Overlapping commitments "
    "consume time once. Calendar plans do not prove actual work; "
    "free space is not guaranteed capacity.",
    "docket_set_workload_preferences": "Set reported availability/protected local clock windows, "
    "travel buffers and exception-notification preferences. Omitted fields retain values; "
    "null clears optional values. Overnight windows are supported. No scheduling/provider effect.",
    "docket_set_project_budget": "Set an optional weekly project hour budget, without changing "
    "work, compensation or commitments. Docket retains it as an auditable Preference; "
    "retract the returned preference to remove it.",
}
