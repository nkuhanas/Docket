from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from docket.schemas.records import (
    DiscordId,
    DiscordRequestKey,
    MeetingId,
    RecordSourceInput,
    StrictModel,
    validate_discord_request_fields,
)

CalendarActionType = Literal["calendar_create_meeting", "calendar_update_meeting"]


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
