from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from docket.config import get_settings
from docket.schemas.common import StrictModel

CalendarFreshness = Literal["prefer_cache", "require_fresh"]
CalendarEventResultView = Literal["occurrences", "series"]
CalendarRelativeDay = Literal["today", "tomorrow"]
CalendarPriority = Literal["low", "normal", "high", "urgent"]
CalendarReminderChannel = Literal["google_popup", "docket_queue"]
CalendarLane = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
    Field(
        description=(
            "Stable Docket Calendar lane slug. Read configured lanes before selecting one; "
            "the five built-in slugs are defaults, not a closed set."
        )
    ),
]


def _normalize_operator_tag(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


OperatorTag = Annotated[
    str,
    BeforeValidator(_normalize_operator_tag),
    Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]


def _default_reminder_channels() -> list[CalendarReminderChannel]:
    return ["google_popup", "docket_queue"]


def _configured_timezone() -> str:
    return get_settings().timezone


class CalendarReminderPlanInput(StrictModel):
    delivery_channels: list[CalendarReminderChannel] = Field(
        default_factory=_default_reminder_channels,
        min_length=1,
        max_length=2,
    )
    lead_seconds: list[int] = Field(default_factory=lambda: [600], max_length=5)

    @field_validator("delivery_channels")
    @classmethod
    def channels_are_canonical(
        cls, value: list[CalendarReminderChannel]
    ) -> list[CalendarReminderChannel]:
        if "google_popup" not in value or len(value) != len(set(value)):
            raise ValueError(
                "reminder delivery must include google_popup; docket_queue is optional"
            )
        return [channel for channel in ("google_popup", "docket_queue") if channel in value]

    @field_validator("lead_seconds")
    @classmethod
    def leads_are_provider_compatible(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("reminder leads must be unique")
        if any(lead < 0 or lead > 2_419_200 or lead % 60 != 0 for lead in value):
            raise ValueError("reminder leads must be whole minutes from zero through 28 days")
        return sorted(value)


class TimedEventTiming(StrictModel):
    kind: Literal["timed"]
    start_local: datetime
    end_local: datetime
    timezone: str = Field(
        default_factory=_configured_timezone,
        min_length=1,
        max_length=128,
        description=(
            "Explicit IANA timezone. Omit to inherit Docket's configured DOCKET_TIMEZONE."
        ),
    )
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
            self.start_local.replace(tzinfo=zone, fold=fold).utcoffset() for fold in start_folds
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
    timezone: str = Field(
        default_factory=_configured_timezone,
        min_length=1,
        max_length=128,
        description=(
            "Explicit IANA timezone. Omit to inherit Docket's configured DOCKET_TIMEZONE."
        ),
    )

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
    calendar_lane: CalendarLane = Field(
        default="unsorted",
        description=(
            "One stable destination lane. Use explicit operator direction first, then a "
            "stored entity default, then bounded inference; use unsorted only when genuinely "
            "ambiguous."
        ),
    )
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
    def priority_and_recurrence_are_safe(
        self,
        info: ValidationInfo,
    ) -> "StandaloneCalendarEventInput":
        allow_explicit_priority = bool(
            info.context and info.context.get("allow_explicit_priority") is True
        )
        if self.priority != "normal" and not allow_explicit_priority:
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


class CalendarLaneResult(StrictModel):
    ref: str
    lane_id: UUID
    lane: CalendarLane
    display_name: str
    color_hex: str
    status: Literal["unprovisioned", "provisioning", "active", "failed", "deleting", "deleted"]
    account_id: UUID
    calendar_id: str | None = None
    operator_policy_text: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = 100
    basis_refs: list[str] = Field(default_factory=list)
    decision_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    created_by_changeset_ref: str
    version: int = Field(ge=1)
