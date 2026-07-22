from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from docket.schemas.records import (
    DiscordId,
    DiscordRequestKey,
    RecordSourceInput,
    StrictModel,
    validate_discord_request_fields,
)

QueueStatus = Literal[
    "pending",
    "awaiting_approval",
    "executing",
    "completed",
    "failed",
    "reconciliation_required",
    "snoozed",
    "ignored",
]
QueuePriority = Literal["low", "normal", "high", "urgent"]


class QueueWriteInput(StrictModel):
    queue_item_id: UUID
    expected_version: int = Field(ge=1)
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def request_matches_source(self) -> "QueueWriteInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        return self


class SnoozeQueueItemInput(QueueWriteInput):
    snoozed_until: datetime | None = None
    snooze_local_date: date | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def exactly_one_wake_time(self) -> "SnoozeQueueItemInput":
        if (self.snoozed_until is None) == (self.snooze_local_date is None):
            raise ValueError("Exactly one of snoozed_until and snooze_local_date is required")
        if self.snoozed_until is not None and self.snoozed_until.tzinfo is None:
            raise ValueError("snoozed_until must include a timezone")
        return self


class IgnoreQueueItemInput(QueueWriteInput):
    reason: str = Field(min_length=1, max_length=1000)


class QueueMutationResult(StrictModel):
    request_id: UUID
    queue_item_id: UUID
    version: int
    status: QueueStatus
    disposition: Literal["updated", "replayed_request"]
    snoozed_until: datetime | None = None
    snooze_local_date: date | None = None
