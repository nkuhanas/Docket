"""Small business contracts; internal compiler vocabulary stays in services."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, field_validator

from docket.schemas.calendar import CalendarRecurrenceInput
from docket.schemas.common import StrictModel
from docket.schemas.tracked_context import TaskPriority, TaskState

ContextId = Annotated[str, Field(pattern=r"^nctx_[0-9A-HJKMNP-TV-Z]{26}$")]
OperationId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Reuse for retries.",
    ),
]
CalendarRef = Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")]


class CommandBase(StrictModel):
    context_id: ContextId
    operation_id: OperationId
    preview: bool = False
    replaces_change_id: Annotated[str, Field(pattern=r"^prep_[0-9A-HJKMNP-TV-Z]{26}$")] | None = (
        None
    )


class CreateEvent(CommandBase):
    title: str = Field(min_length=1, max_length=512)
    start: str = Field(max_length=64, description="ISO timestamp; ISO date with all_day=true.")
    end: str = Field(max_length=64, description="After start; all-day end date is exclusive.")
    timezone: str = Field(max_length=128, description="IANA zone, e.g. America/Los_Angeles.")
    calendar_ref: CalendarRef
    location: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)
    all_day: bool = False
    recurrence: CalendarRecurrenceInput | None = None
    entity_refs: list[Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list, max_length=100
    )
    item_refs: list[Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list, max_length=100
    )
    time_refs: list[Annotated[str, Field(pattern=r"^time_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list, max_length=100
    )
    tags: list[str] = Field(default_factory=list, max_length=8)
    context_labels: list[str] = Field(default_factory=list, max_length=25)
    operator_policy_text: str | None = Field(default=None, max_length=4000)


EventRef = Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")]
TaskRef = Annotated[str, Field(pattern=r"^task_[0-9A-HJKMNP-TV-Z]{26}$")]
ItemRef = Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")]
ChangeId = Annotated[str, Field(pattern=r"^prep_[0-9A-HJKMNP-TV-Z]{26}$")]


class EventScope(CommandBase):
    event_ref: EventRef
    scope: Literal["event", "series", "occurrence"] = "event"
    occurrence_start: str | None = Field(default=None, max_length=64)
    occurrence_date: date | None = None


class UpdateEvent(EventScope):
    all_day: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=128)
    calendar_ref: CalendarRef | None = None
    location: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)
    recurrence: CalendarRecurrenceInput | None = None
    entity_refs: list[Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]] | None = None
    item_refs: list[ItemRef] | None = None
    time_refs: list[Annotated[str, Field(pattern=r"^time_[0-9A-HJKMNP-TV-Z]{26}$")]] | None = None
    context_labels: list[str] | None = None
    operator_policy_text: str | None = Field(default=None, max_length=4000)
    routing_ref: Annotated[str, Field(pattern=r"^route_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    status: Literal["active", "cancelled", "archived"] | None = None
    replace: bool = False

    @field_validator("title", "start", "end", "timezone", "calendar_ref")
    @classmethod
    def required_values_cannot_be_cleared(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit an unchanged required field; it cannot be cleared")
        return value


class CancelEvent(EventScope):
    pass


class CreateTask(CommandBase):
    title: str = Field(min_length=1, max_length=1024)
    notes: str | None = Field(default=None, max_length=16000)
    state: TaskState = "not_started"
    priority: TaskPriority = "normal"
    due: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=128)
    item_ref: ItemRef | None = None


class UpdateTask(CommandBase):
    task_ref: TaskRef
    title: str | None = Field(default=None, min_length=1, max_length=1024)
    notes: str | None = Field(default=None, max_length=16000)
    state: TaskState | None = None
    priority: TaskPriority | None = None
    due: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=128)
    item_ref: ItemRef | None = None
    canonical_status: Literal["active", "historical", "retracted"] | None = None
    completed_at: str | None = Field(default=None, max_length=64)
    source_refs: list[Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]] | None = None


class CompleteTask(CommandBase):
    task_ref: TaskRef
    completed_at: str | None = Field(default=None, max_length=64)


class ApplyChange(StrictModel):
    change_id: ChangeId


class PrepareBatch(StrictModel):
    operation_id: OperationId
    change_ids: list[ChangeId] = Field(min_length=2, max_length=25)
