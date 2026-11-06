"""One opt-in display command; projection support stays private."""

from typing import Annotated

from pydantic import Field, model_validator

from docket.schemas.common import StrictModel
from docket.schemas.native_commands import CalendarRef, CommandBase, EventRef
from docket.schemas.work import WorkRef

WorkCalendarRef = Annotated[str, Field(pattern=r"^wcal_[0-9A-HJKMNP-TV-Z]{26}$")]
WorkRevisionRef = Annotated[str, Field(pattern=r"^wrev_[0-9A-HJKMNP-TV-Z]{26}$")]


class ShowWorkSession(CommandBase):
    work_ref: WorkRef
    calendar_ref: CalendarRef


class WorkCalendarSpec(StrictModel):
    work_ref: WorkRef
    work_version: int = Field(ge=1)
    source_revision_ref: WorkRevisionRef
    calendar_ref: CalendarRef
    event_ref: EventRef | None = None
    event_change_id: str | None = Field(default=None, max_length=128)
    renderer_version: int = Field(default=1, ge=1, le=1)

    @model_validator(mode="after")
    def one_event(self) -> "WorkCalendarSpec":
        if (self.event_ref is None) == (self.event_change_id is None):
            raise ValueError("Select one event ref or its staged create change")
        return self


WORK_CALENDAR_PURPOSES = {
    "docket_show_work_session": "Show a recorded bounded work session as a Calendar snapshot "
    "pinned to its exact revision. Docket derives the display and provider intent. "
    "Corrections flag it for refresh without blocking work. Reuse this command to refresh; "
    "retract the returned wcal_ to hide. Duration-only/incomplete work remains valid but "
    "cannot invent a Calendar interval.",
}
