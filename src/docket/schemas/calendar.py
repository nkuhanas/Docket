from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, field_validator, model_validator

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
CalendarPriority = Literal["low", "normal", "high", "urgent"]
CalendarPriorityBasis = Literal["default", "explicit_operator"]
CalendarProposalMode = Literal["explicit_only", "suggest", "off"]
CalendarConflictPolicy = Literal["warn", "block"]
CalendarReminderDisposition = Literal["preserve", "replace", "disable"]
CalendarReminderChannel = Literal["google_popup", "docket_queue"]

def _normalize_operator_tag(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


OperatorTag = Annotated[
    str,
    BeforeValidator(_normalize_operator_tag),
    Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]


def _default_reminder_channels() -> list[CalendarReminderChannel]:
    return ["google_popup", "docket_queue"]


class CalendarReminderPlanInput(StrictModel):
    delivery_channels: list[CalendarReminderChannel] = Field(
        default_factory=_default_reminder_channels,
        min_length=2,
        max_length=2,
    )
    lead_seconds: list[int] = Field(default_factory=lambda: [600], max_length=5)

    @field_validator("delivery_channels")
    @classmethod
    def channels_are_fixed(
        cls, value: list[CalendarReminderChannel]
    ) -> list[CalendarReminderChannel]:
        if set(value) != {"google_popup", "docket_queue"} or len(set(value)) != 2:
            raise ValueError(
                "reminder delivery must include google_popup and docket_queue exactly once"
            )
        return ["google_popup", "docket_queue"]

    @field_validator("lead_seconds")
    @classmethod
    def leads_are_provider_compatible(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("reminder leads must be unique")
        if any(lead < 0 or lead > 2_419_200 or lead % 60 != 0 for lead in value):
            raise ValueError(
                "reminder leads must be whole minutes from zero through 28 days"
            )
        return sorted(value)


class TimedEventTiming(StrictModel):
    kind: Literal["timed"]
    start_local: datetime
    end_local: datetime
    timezone: str = Field(min_length=1, max_length=128)
    fold: Literal[0, 1] | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @staticmethod
    def _valid_folds(value: datetime, zone: ZoneInfo) -> list[int]:
        valid: list[int] = []
        for fold in (0, 1):
            aware = value.replace(tzinfo=zone, fold=fold)
            round_trip = aware.astimezone(UTC).astimezone(zone)
            if round_trip.replace(tzinfo=None) == value and round_trip.fold == fold:
                valid.append(fold)
        return valid

    @model_validator(mode="after")
    def local_times_are_unambiguous_and_ordered(self) -> "TimedEventTiming":
        if self.start_local.tzinfo is not None or self.end_local.tzinfo is not None:
            raise ValueError("timed event local values must omit UTC offsets")
        zone = ZoneInfo(self.timezone)
        start_folds = self._valid_folds(self.start_local, zone)
        end_folds = self._valid_folds(self.end_local, zone)
        if not start_folds or not end_folds:
            raise ValueError("timed event falls in a nonexistent daylight-saving local time")
        start_offsets = {
            self.start_local.replace(tzinfo=zone, fold=fold).utcoffset()
            for fold in start_folds
        }
        end_offsets = {
            self.end_local.replace(tzinfo=zone, fold=fold).utcoffset() for fold in end_folds
        }
        ambiguous = len(start_offsets) > 1 or len(end_offsets) > 1
        if ambiguous and self.fold is None:
            raise ValueError("ambiguous daylight-saving local time requires fold")
        fold = self.fold or 0
        if fold not in start_folds or fold not in end_folds:
            raise ValueError("selected fold is invalid for the event bounds")
        start = self.start_local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        end = self.end_local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if end <= start:
            raise ValueError("event end must be after start")
        return self


class AllDayEventTiming(StrictModel):
    kind: Literal["all_day"]
    start_date: date
    end_date: date
    timezone: str = Field(default="America/Los_Angeles", min_length=1, max_length=128)

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "AllDayEventTiming":
        if self.end_date <= self.start_date:
            raise ValueError("all-day end_date must be exclusive and after start_date")
        return self


CalendarEventTiming = Annotated[
    TimedEventTiming | AllDayEventTiming,
    Field(discriminator="kind"),
]


class CalendarRecurrenceInput(StrictModel):
    frequency: Literal["daily", "weekly", "monthly"]
    interval: int = Field(default=1, ge=1, le=52)
    weekdays: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] = Field(
        default_factory=list,
        max_length=7,
    )
    month_days: list[int] = Field(default_factory=list, max_length=31)
    count: int | None = Field(default=None, ge=1, le=1000)
    until_date: date | None = None
    excluded_dates: list[date] = Field(default_factory=list, max_length=100)
    additional_dates: list[date] = Field(default_factory=list, max_length=100)

    @field_validator("weekdays", "month_days", "excluded_dates", "additional_dates")
    @classmethod
    def values_are_unique(cls, value: list[object]) -> list[object]:
        if len(value) != len(set(value)):
            raise ValueError("recurrence selector values must be unique")
        return value

    @field_validator("month_days")
    @classmethod
    def month_days_are_valid(cls, value: list[int]) -> list[int]:
        if any(day < 1 or day > 31 for day in value):
            raise ValueError("month_days must be from 1 through 31")
        return sorted(value)

    @model_validator(mode="after")
    def recurrence_is_bounded_and_typed(self) -> "CalendarRecurrenceInput":
        if (self.count is None) == (self.until_date is None):
            raise ValueError("recurrence requires exactly one of count or until_date")
        if self.frequency == "weekly" and not self.weekdays:
            raise ValueError("weekly recurrence requires weekdays")
        if self.frequency != "weekly" and self.weekdays:
            raise ValueError("weekdays are valid only for weekly recurrence")
        if self.frequency == "monthly" and not self.month_days:
            raise ValueError("monthly recurrence requires month_days")
        if self.frequency != "monthly" and self.month_days:
            raise ValueError("month_days are valid only for monthly recurrence")
        overlap = set(self.excluded_dates) & set(self.additional_dates)
        if overlap:
            raise ValueError("a recurrence date cannot be both excluded and added")
        return self


class StandaloneCalendarEventInput(StrictModel):
    title: str = Field(min_length=1, max_length=512)
    timing: CalendarEventTiming
    location: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)
    operator_tags: list[OperatorTag] = Field(default_factory=list, max_length=8)
    priority: CalendarPriority = "normal"
    recurrence: CalendarRecurrenceInput | None = None
    reminder_plan: CalendarReminderPlanInput | None = None

    @field_validator("operator_tags")
    @classmethod
    def tags_are_normalized_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [tag.lower() for tag in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("operator tags must be unique")
        return sorted(normalized)

    @model_validator(mode="after")
    def priority_and_recurrence_are_safe(self) -> "StandaloneCalendarEventInput":
        if self.priority != "normal":
            raise ValueError(
                "initial proposals default to normal priority; use the authenticated "
                "Priority control for a non-default value"
            )
        if (
            self.recurrence is not None
            and self.recurrence.until_date is not None
            and isinstance(self.timing, AllDayEventTiming)
            and self.recurrence.until_date < self.timing.start_date
        ):
            raise ValueError("recurrence until_date must not precede event start")
        if (
            self.recurrence is not None
            and self.recurrence.until_date is not None
            and isinstance(self.timing, TimedEventTiming)
            and self.recurrence.until_date < self.timing.start_local.date()
        ):
            raise ValueError("recurrence until_date must not precede event start")
        return self

    @property
    def recurrence_kind(self) -> Literal["one_time", "recurring"]:
        return "recurring" if self.recurrence is not None else "one_time"

    @property
    def system_tags(self) -> list[str]:
        timing_kind = "all_day" if isinstance(self.timing, AllDayEventTiming) else "timed"
        return [self.recurrence_kind, timing_kind, "standalone"]


class CalendarProfileInput(StrictModel):
    proposal_mode: CalendarProposalMode = "suggest"
    default_reminder_lead_seconds: list[int] = Field(
        default_factory=lambda: [600], max_length=5
    )
    default_reminder_delivery_channels: list[CalendarReminderChannel] = Field(
        default_factory=_default_reminder_channels,
        min_length=2,
        max_length=2,
    )
    conflict_policy: CalendarConflictPolicy = "warn"

    @model_validator(mode="after")
    def reminder_defaults_are_canonical(self) -> "CalendarProfileInput":
        plan = CalendarReminderPlanInput(
            delivery_channels=self.default_reminder_delivery_channels,
            lead_seconds=self.default_reminder_lead_seconds,
        )
        self.default_reminder_delivery_channels = plan.delivery_channels
        self.default_reminder_lead_seconds = plan.lead_seconds
        return self


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
