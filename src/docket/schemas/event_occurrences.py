from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from docket.schemas.calendar import CalendarEventTiming, TimedEventTiming
from docket.schemas.common import StrictModel


class OccurrenceIdentity(StrictModel):
    """Original recurrence coordinate, never the current replacement's start."""

    series_ref: str = Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")
    original_date: date
    timezone: str = Field(min_length=1, max_length=128)
    original_start_local: datetime | None = None
    fold: Literal[0, 1] | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("occurrence timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def valid_original_coordinate(self) -> OccurrenceIdentity:
        if self.original_start_local is None:
            if self.fold is not None:
                raise ValueError("all-day occurrence identity cannot have a fold")
            return self
        if self.original_start_local.tzinfo is not None:
            raise ValueError("original_start_local must omit UTC offsets")
        if self.original_start_local.date() != self.original_date:
            raise ValueError("original start must be on original_date")
        zone = ZoneInfo(self.timezone)
        folds = TimedEventTiming._valid_folds(self.original_start_local, zone)
        offsets = {
            self.original_start_local.replace(tzinfo=zone, fold=fold).utcoffset() for fold in folds
        }
        if not folds:
            raise ValueError("original occurrence start does not exist in its timezone")
        if len(offsets) > 1 and self.fold is None:
            raise ValueError("ambiguous original start requires fold")
        if (self.fold or 0) not in folds:
            raise ValueError("original occurrence fold is invalid")
        if len(offsets) == 1:
            self.fold = 0  # One canonical encoding for an unambiguous zoned start.
        return self

    @property
    def coordinate_key(self) -> str:
        if self.original_start_local is None:
            return f"date:{self.original_date.isoformat()}"
        aware = self.original_start_local.replace(
            tzinfo=ZoneInfo(self.timezone), fold=self.fold or 0
        )
        return f"timed:{aware.isoformat()}"


class OccurrenceReplacement(StrictModel):
    """Only fields describing this occurrence; no recurrence or routing changes."""

    title: str = Field(min_length=1, max_length=512)
    timing: CalendarEventTiming
    location: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)


class ResolvedCalendarDate(StrictModel):
    date: date
    timezone: str
    source_utterance_ref: str = Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    relative_day: Literal["today", "tomorrow"] | None = None


class OneTimeEventScope(StrictModel):
    kind: Literal["one_time"] = "one_time"


class EntireSeriesEventScope(StrictModel):
    kind: Literal["entire_series"] = "entire_series"


class OccurrenceEventScope(StrictModel):
    kind: Literal["occurrence"] = "occurrence"
    identity: OccurrenceIdentity


EventMutationScope = Annotated[
    OneTimeEventScope | EntireSeriesEventScope | OccurrenceEventScope,
    Field(discriminator="kind"),
]


class OccurrenceEditPlan(StrictModel):
    """Internal compiler output, never accepted from the interactive model."""

    identity: OccurrenceIdentity
    original_timing: dict[str, Any]
    series_version: int
    occurrence_version: int | None = None
    master_after: dict[str, Any]
    replacement_event_ref: str | None = None
    replacement_after: dict[str, Any] | None = None
    status: Literal["cancelled", "replaced"]
    no_op: bool


class CompiledOccurrenceEdit(StrictModel):
    source_change_id: str
    source_change: dict[str, Any]
    source_scope: EventMutationScope
    plan: OccurrenceEditPlan
    replacement_change_id: str | None = None
    action_hashes: dict[str, str]
    basis_refs: list[str]
