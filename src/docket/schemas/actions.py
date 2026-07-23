from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from docket.schemas.calendar import (
    CalendarReminderDisposition,
    CalendarReminderPlanInput,
    StandaloneCalendarEventInput,
)
from docket.schemas.records import (
    DiscordId,
    DiscordRequestKey,
    MeetingId,
    RecordSourceInput,
    StrictModel,
    validate_discord_request_fields,
)

CalendarActionType = Literal["calendar_create_meeting", "calendar_update_meeting"]
StandaloneCalendarActionType = Literal[
    "calendar_create_event",
    "calendar_update_event",
    "calendar_update_reminders",
    "calendar_cancel_event",
]


class CalendarMeetingActionParameters(StrictModel):
    meeting_id: MeetingId
    calendar_id: str = Field(min_length=1, max_length=1024)


class ProposeActionInput(StrictModel):
    action_type: CalendarActionType
    record_id: UUID
    expected_record_version: int = Field(ge=1)
    account_id: UUID
    parameters: CalendarMeetingActionParameters
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def request_matches_source(self) -> "ProposeActionInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        return self


class CreateCalendarEventProposal(StrictModel):
    kind: Literal["create"]
    event: StandaloneCalendarEventInput


class UpdateCalendarEventProposal(StrictModel):
    kind: Literal["update"]
    provider_event_id: str = Field(min_length=1, max_length=1024)
    replacement: StandaloneCalendarEventInput
    reminder_disposition: CalendarReminderDisposition = "preserve"
    reminder_plan: CalendarReminderPlanInput | None = None

    @model_validator(mode="after")
    def reminder_change_is_explicit(self) -> "UpdateCalendarEventProposal":
        if self.replacement.reminder_plan is not None:
            raise ValueError(
                "update replacement event omits reminder_plan; use reminder_disposition"
            )
        if self.reminder_disposition == "replace" and self.reminder_plan is None:
            raise ValueError("replace requires an explicit reminder_plan")
        if self.reminder_disposition != "replace" and self.reminder_plan is not None:
            raise ValueError("reminder_plan is valid only when reminder_disposition is replace")
        return self


class UpdateCalendarRemindersProposal(StrictModel):
    kind: Literal["reminders"]
    provider_event_id: str = Field(min_length=1, max_length=1024)
    reminder_plan: CalendarReminderPlanInput


class CancelCalendarEventProposal(StrictModel):
    kind: Literal["cancel"]
    provider_event_id: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=1000)


CalendarEventProposal = Annotated[
    CreateCalendarEventProposal
    | UpdateCalendarEventProposal
    | UpdateCalendarRemindersProposal
    | CancelCalendarEventProposal,
    Field(discriminator="kind"),
]


class ProposeCalendarEventInput(StrictModel):
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=1024)
    proposal: CalendarEventProposal
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def request_matches_source(self) -> "ProposeCalendarEventInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        return self


class ProposalResult(StrictModel):
    request_id: UUID
    disposition: Literal["proposed", "replayed_request"]
    queue_item_id: UUID
    action_id: UUID
    action_revision_id: UUID
    approval_id: UUID
    short_code: str
    expires_at: datetime
    preview: dict[str, Any]
    projection_status: Literal["pending"] = "pending"


class AccountResult(StrictModel):
    account_id: UUID
    provider: Literal["google"]
    external_account_id: str
    display_name: str | None
    email_address: str | None
    capabilities: list[str]
