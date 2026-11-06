"""Six flat task workflows; provider IDs and compiler inputs remain internal."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from docket.schemas.common import PublicRef, StrictModel
from docket.schemas.native_commands import CalendarRef, CommandBase, ItemRef
from docket.schemas.tracked_context import TaskListRef, TaskRef, TaskState

TaskObservationRef = Annotated[str, Field(pattern=r"^tobs_[0-9A-HJKMNP-TV-Z]{26}$")]
TaskConflictRef = Annotated[str, Field(pattern=r"^tscf_[0-9A-HJKMNP-TV-Z]{26}$")]
TaskSyncField = Literal[
    "title",
    "notes",
    "scheduled_date",
    "status",
    "completed_at",
    "parent",
    "deletion",
    "location",
]
TaskSyncMode = Literal["disabled", "observe", "two_way"]

TASK_TOOL_PURPOSES = {
    "docket_get_tasks_context": "Read task lists or selected task/sync records and their versions.",
    "docket_query_tasks": "Filter canonical tasks; return bounded results and a lossless cursor.",
    "docket_configure_task_list":
        "Select observe/two-way/disabled policy, default and reminder calendar.",
    "docket_refresh_tasks":
        "Queue list discovery or a selected list refresh under its existing policy.",
    "docket_link_google_task":
        "Link an observed Google task to an existing Task, or detach its link.",
    "docket_resolve_task_sync":
        "Resolve selected conflicting task fields using Docket or Google values.",
}


class GetTasksContext(StrictModel):
    task_list_ref: TaskListRef | None = None
    refs: list[PublicRef] = Field(default_factory=list, max_length=12)
    cursor: str | None = Field(default=None, max_length=1536)


class QueryTasks(StrictModel):
    task_list_ref: TaskListRef | None = None
    item_ref: ItemRef | None = None
    parent_task_ref: TaskRef | None = None
    text: str | None = Field(default=None, max_length=200)
    state: TaskState | None = None
    scheduled_from: date | None = None
    scheduled_to: date | None = None
    include_closed: bool = False
    include_retracted: bool = False
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=1536)


class ConfigureTaskList(CommandBase):
    task_list_ref: TaskListRef
    mode: TaskSyncMode
    is_default: bool | None = None
    reminder_calendar_ref: CalendarRef | None = None


class RefreshTasks(StrictModel):
    task_list_ref: TaskListRef | None = None


class LinkGoogleTask(CommandBase):
    task_ref: TaskRef
    observation_ref: TaskObservationRef | None = None
    detach: bool = False

    @model_validator(mode="after")
    def selected_counterparty_or_detachment(self) -> LinkGoogleTask:
        if self.detach == (self.observation_ref is not None):
            raise ValueError("Select an observed counterpart, or detach the current representation")
        return self


class ResolveTaskSync(CommandBase):
    conflict_ref: TaskConflictRef
    choose: Literal["docket", "google"]
    fields: list[TaskSyncField] | None = Field(default=None, min_length=1, max_length=8)


class TaskListPolicySpec(StrictModel):
    mode: TaskSyncMode
    is_default: bool | None = None
    reminder_calendar_ref: CalendarRef | None = None


class TaskGoogleLinkSpec(StrictModel):
    observation_ref: TaskObservationRef | None = None
    detach: bool = False

    @model_validator(mode="after")
    def selected_counterparty_or_detachment(self) -> TaskGoogleLinkSpec:
        if self.detach == (self.observation_ref is not None):
            raise ValueError("Select an observed counterpart, or detach the current representation")
        return self


class TaskSyncResolutionSpec(StrictModel):
    choose: Literal["docket", "google"]
    fields: list[TaskSyncField] | None = Field(default=None, min_length=1, max_length=8)
