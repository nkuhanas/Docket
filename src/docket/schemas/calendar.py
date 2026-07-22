from datetime import datetime
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

CalendarFreshness = Literal["prefer_cache", "require_fresh"]
CalendarRelativeDay = Literal["today", "tomorrow"]
ReminderScope = Literal["calendar", "event"]


class SetReminderRuleInput(StrictModel):
    rule_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=1024)
    scope: ReminderScope
    provider_event_id: str | None = Field(default=None, min_length=1, max_length=1024)
    lead_seconds: int = Field(ge=0, le=2_678_400)
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId

    @model_validator(mode="after")
    def validate_rule(self) -> "SetReminderRuleInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        if (self.rule_id is None) != (self.expected_version is None):
            raise ValueError("rule_id and expected_version must be supplied together")
        if self.scope == "event" and self.provider_event_id is None:
            raise ValueError("event-scoped reminders require provider_event_id")
        if self.scope == "calendar" and self.provider_event_id is not None:
            raise ValueError("calendar-scoped reminders omit provider_event_id")
        return self


class DisableReminderRuleInput(StrictModel):
    rule_id: UUID
    expected_version: int = Field(ge=1)
    request_key: DiscordRequestKey
    source: RecordSourceInput
    actor_type: Literal["hermes"] = "hermes"
    actor_id: DiscordId
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_source(self) -> "DisableReminderRuleInput":
        validate_discord_request_fields(self.request_key, self.source, self.actor_id)
        return self


class ReminderRuleResult(StrictModel):
    request_id: UUID
    rule_id: UUID
    version: int
    enabled: bool
    disposition: Literal["created", "updated", "matched_existing", "disabled", "replayed_request"]
    materialized_notifications: int = 0


class CalendarLookupInput(StrictModel):
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=1024)
    start: datetime | None = None
    end: datetime | None = None
    relative_day: CalendarRelativeDay | None = None
    text_filter: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=100, ge=1, le=100)
    freshness: CalendarFreshness = "prefer_cache"

    @model_validator(mode="after")
    def validate_bounds(self) -> "CalendarLookupInput":
        if self.relative_day is not None and (self.start is not None or self.end is not None):
            raise ValueError("relative_day cannot be combined with explicit start or end bounds")
        if (self.start is None) != (self.end is None):
            raise ValueError("Calendar lookup start and end must be supplied together")
        if self.start is None or self.end is None:
            return self
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Calendar lookup bounds must include timezones")
        if self.end <= self.start:
            raise ValueError("Calendar lookup end must be after start")
        return self
