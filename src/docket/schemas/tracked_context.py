from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from docket.schemas.common import StrictModel, validate_refs

ItemRef = Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")]
TaskRef = Annotated[str, Field(pattern=r"^task_[0-9A-HJKMNP-TV-Z]{26}$")]
TemporalBindingRef = Annotated[str, Field(pattern=r"^time_[0-9A-HJKMNP-TV-Z]{26}$")]
EventRef = Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")]
EntityRef = Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]
SourceRef = Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]
LaneRef = Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")]
ReminderPlanRef = Annotated[str, Field(pattern=r"^rem_[0-9A-HJKMNP-TV-Z]{26}$")]

CanonicalStatus = Literal["active", "historical", "retracted"]
TemporalRole = Literal[
    "scheduled_on",
    "occurs_at",
    "due_by",
    "opens_at",
    "closes_at",
    "available_from",
    "available_until",
    "expected_at",
    "effective_from",
    "effective_until",
    "window",
]
TaskState = Literal["not_started", "in_progress", "blocked", "completed", "cancelled"]
TaskPriority = Literal["low", "normal", "high", "urgent"]


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be an IANA zone") from exc


def _valid_folds(value: datetime, zone: ZoneInfo) -> list[int]:
    valid: list[int] = []
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == value and round_trip.fold == fold:
            valid.append(fold)
    return valid


def _validate_local_datetime(value: datetime, timezone: str, fold: int | None) -> int:
    if value.tzinfo is not None:
        raise ValueError("local datetime must not contain an offset")
    zone = _zone(timezone)
    valid = _valid_folds(value, zone)
    if not valid:
        raise ValueError("local datetime falls in a nonexistent daylight-saving time")
    offsets = {value.replace(tzinfo=zone, fold=item).utcoffset() for item in valid}
    if len(offsets) > 1 and fold is None:
        raise ValueError("ambiguous daylight-saving local time requires fold")
    selected = 0 if fold is None else fold
    if selected not in valid:
        raise ValueError("selected fold is invalid for the local datetime")
    return selected


class DateTemporalValue(StrictModel):
    kind: Literal["date"]
    date: date
    timezone: str

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        _zone(value)
        return value


class DateTimeTemporalValue(StrictModel):
    kind: Literal["datetime"]
    local_datetime: datetime
    timezone: str
    fold: Literal[0, 1] | None = None

    @model_validator(mode="after")
    def local_time_exists(self) -> DateTimeTemporalValue:
        _validate_local_datetime(self.local_datetime, self.timezone, self.fold)
        return self


class DateIntervalTemporalValue(StrictModel):
    kind: Literal["date_interval"]
    start_date: date
    end_date: date
    end_inclusive: bool
    timezone: str

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        _zone(value)
        return value

    @model_validator(mode="after")
    def interval_is_ordered(self) -> DateIntervalTemporalValue:
        if self.end_date < self.start_date:
            raise ValueError("date interval end precedes its start")
        return self


class DateTimeIntervalTemporalValue(StrictModel):
    kind: Literal["datetime_interval"]
    start_local: datetime
    end_local: datetime
    timezone: str
    fold: Literal[0, 1] | None = None

    @model_validator(mode="after")
    def interval_exists_and_is_ordered(self) -> DateTimeIntervalTemporalValue:
        selected = _validate_local_datetime(self.start_local, self.timezone, self.fold)
        _validate_local_datetime(self.end_local, self.timezone, self.fold)
        zone = _zone(self.timezone)
        start = self.start_local.replace(tzinfo=zone, fold=selected).astimezone(UTC)
        end = self.end_local.replace(tzinfo=zone, fold=selected).astimezone(UTC)
        if end <= start:
            raise ValueError("datetime interval end must follow its start")
        return self


TemporalValue = Annotated[
    DateTemporalValue
    | DateTimeTemporalValue
    | DateIntervalTemporalValue
    | DateTimeIntervalTemporalValue,
    Field(discriminator="kind"),
]


class ItemInput(StrictModel):
    title: str = Field(min_length=1, max_length=512)
    kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$",
        max_length=128,
    )
    description: str | None = Field(default=None, max_length=16_000)
    context_entity_refs: list[EntityRef] = Field(default_factory=list, max_length=100)
    parent_item_ref: ItemRef | None = None
    parent_item_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    canonical_status: CanonicalStatus = "active"
    metadata_json: dict[str, object] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=100)

    @field_validator("context_entity_refs", "source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str]) -> list[str]:
        return validate_refs(values)

    @field_validator("metadata_json")
    @classmethod
    def metadata_is_bounded(cls, value: dict[str, object]) -> dict[str, object]:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise ValueError("metadata_json exceeds 16 KiB UTF-8")
        return value

    @model_validator(mode="after")
    def parent_is_exact(self) -> ItemInput:
        if self.parent_item_ref is not None and self.parent_item_change_id is not None:
            raise ValueError("parent item uses a ref or same-ChangeSet change id, not both")
        return self


class ItemPatchInput(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$",
        max_length=128,
    )
    description: str | None = Field(default=None, max_length=16_000)
    context_entity_refs: list[EntityRef] | None = Field(default=None, max_length=100)
    parent_item_ref: ItemRef | None = None
    parent_item_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    canonical_status: CanonicalStatus | None = None
    metadata_json: dict[str, object] | None = None
    source_refs: list[SourceRef] | None = Field(default=None, max_length=100)

    @field_validator("context_entity_refs", "source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str] | None) -> list[str] | None:
        return validate_refs(values) if values is not None else None

    @field_validator("metadata_json")
    @classmethod
    def metadata_is_bounded(
        cls, value: dict[str, object] | None
    ) -> dict[str, object] | None:
        if value is not None:
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            if len(encoded) > 16 * 1024:
                raise ValueError("metadata_json exceeds 16 KiB UTF-8")
        return value

    @model_validator(mode="after")
    def patch_is_exact(self) -> ItemPatchInput:
        if self.parent_item_ref is not None and self.parent_item_change_id is not None:
            raise ValueError("parent item uses a ref or same-ChangeSet change id, not both")
        if not self.model_fields_set:
            raise ValueError("item patch requires at least one field")
        return self


class TemporalBindingInput(StrictModel):
    subject_ref: ItemRef | TaskRef | None = None
    subject_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    role: TemporalRole
    binding_key: str = Field(
        default="default", pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$"
    )
    temporal_value: TemporalValue
    canonical_status: CanonicalStatus = "active"
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=100)

    @field_validator("source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str]) -> list[str]:
        return validate_refs(values)

    @model_validator(mode="after")
    def subject_and_value_shape_are_exact(self) -> TemporalBindingInput:
        if (self.subject_ref is None) == (self.subject_change_id is None):
            raise ValueError("temporal binding requires one subject ref or change id")
        is_interval = self.temporal_value.kind in {"date_interval", "datetime_interval"}
        if self.role == "window" and not is_interval:
            raise ValueError("window requires an interval temporal value")
        if self.role != "window" and is_interval:
            raise ValueError("point temporal roles require date or datetime values")
        return self


class TemporalBindingPatchInput(StrictModel):
    role: TemporalRole | None = None
    binding_key: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$"
    )
    temporal_value: TemporalValue | None = None
    canonical_status: CanonicalStatus | None = None
    source_refs: list[SourceRef] | None = Field(default=None, max_length=100)

    @field_validator("source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str] | None) -> list[str] | None:
        return validate_refs(values) if values is not None else None

    @model_validator(mode="after")
    def patch_is_exact(self) -> TemporalBindingPatchInput:
        if not self.model_fields_set:
            raise ValueError("temporal binding patch requires at least one field")
        role = self.role
        value = self.temporal_value
        if role is not None and value is not None:
            is_interval = value.kind in {"date_interval", "datetime_interval"}
            if role == "window" and not is_interval:
                raise ValueError("window requires an interval temporal value")
            if role != "window" and is_interval:
                raise ValueError("point temporal roles require date or datetime values")
        return self


class TaskInput(StrictModel):
    item_ref: ItemRef | None = None
    item_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=16_000)
    task_state: TaskState = "not_started"
    priority: TaskPriority = "normal"
    canonical_status: CanonicalStatus = "active"
    completed_at: datetime | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=100)

    @field_validator("source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str]) -> list[str]:
        return validate_refs(values)

    @model_validator(mode="after")
    def item_and_completion_are_exact(self) -> TaskInput:
        if (self.item_ref is None) == (self.item_change_id is None):
            raise ValueError("task requires one item ref or change id")
        if (self.task_state == "completed") != (self.completed_at is not None):
            raise ValueError("completed_at is present exactly when task_state is completed")
        return self


class TaskPatchInput(StrictModel):
    item_ref: ItemRef | None = None
    item_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=16_000)
    task_state: TaskState | None = None
    priority: TaskPriority | None = None
    canonical_status: CanonicalStatus | None = None
    completed_at: datetime | None = None
    source_refs: list[SourceRef] | None = Field(default=None, max_length=100)

    @field_validator("source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str] | None) -> list[str] | None:
        return validate_refs(values) if values is not None else None

    @model_validator(mode="after")
    def patch_is_exact(self) -> TaskPatchInput:
        if self.item_ref is not None and self.item_change_id is not None:
            raise ValueError("task item uses a ref or change id, not both")
        if not self.model_fields_set:
            raise ValueError("task patch requires at least one field")
        if self.task_state == "completed" and self.completed_at is None:
            raise ValueError("completed task patch requires completed_at")
        if self.task_state in {
            "not_started",
            "in_progress",
            "blocked",
            "cancelled",
        } and ("completed_at" not in self.model_fields_set or self.completed_at is not None):
            raise ValueError("non-completed task patch explicitly clears completed_at")
        return self


class AllDayMarkerPolicy(StrictModel):
    kind: Literal["all_day_marker"]
    transparency: Literal["transparent", "opaque"] = "transparent"


class TimedMarkerPolicy(StrictModel):
    kind: Literal["timed_marker"]
    duration_seconds: int = Field(gt=0, le=86_400)
    transparency: Literal["transparent", "opaque"] = "transparent"


class IntervalSpanPolicy(StrictModel):
    kind: Literal["interval_span"]
    transparency: Literal["transparent", "opaque"] = "transparent"


CalendarDisplayPolicy = Annotated[
    AllDayMarkerPolicy | TimedMarkerPolicy | IntervalSpanPolicy,
    Field(discriminator="kind"),
]


class TemporalCalendarProjectionInput(StrictModel):
    temporal_binding_ref: TemporalBindingRef | None = None
    temporal_binding_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    lane_ref: LaneRef | None = None
    lane_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    display_policy: CalendarDisplayPolicy
    reminder_plan_ref: ReminderPlanRef | None = None
    reminder_plan_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool = True

    @model_validator(mode="after")
    def dependencies_are_exact(self) -> TemporalCalendarProjectionInput:
        pairs = (
            (self.temporal_binding_ref, self.temporal_binding_change_id, "temporal binding"),
            (self.lane_ref, self.lane_change_id, "lane"),
        )
        for ref, change_id, label in pairs:
            if (ref is None) == (change_id is None):
                raise ValueError(f"projection requires one {label} ref or change id")
        if self.reminder_plan_ref and self.reminder_plan_change_id:
            raise ValueError("reminder plan uses a ref or change id, not both")
        return self


class TemporalCalendarProjectionPatchInput(StrictModel):
    lane_ref: LaneRef | None = None
    lane_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    display_policy: CalendarDisplayPolicy | None = None
    reminder_plan_ref: ReminderPlanRef | None = None
    reminder_plan_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None

    @model_validator(mode="after")
    def patch_is_exact(self) -> TemporalCalendarProjectionPatchInput:
        if self.lane_ref is not None and self.lane_change_id is not None:
            raise ValueError("lane update uses a ref or change id, not both")
        if self.reminder_plan_ref is not None and self.reminder_plan_change_id is not None:
            raise ValueError("reminder plan update uses a ref or change id, not both")
        if not self.model_fields_set:
            raise ValueError("temporal projection patch requires at least one field")
        return self


class ReminderPlanInput(StrictModel):
    subject_ref: EventRef | TemporalBindingRef | None = None
    subject_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    delivery_channels: list[Literal["docket_queue", "google_popup"]] = Field(
        min_length=1, max_length=2
    )
    lead_seconds: list[int] = Field(min_length=1, max_length=20)
    date_trigger_local_time: time | None = None
    timezone: str | None = None
    canonical_status: CanonicalStatus = "active"

    @field_validator("delivery_channels")
    @classmethod
    def channels_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("delivery channels must not contain duplicates")
        return values

    @field_validator("lead_seconds")
    @classmethod
    def leads_are_positive_and_unique(cls, values: list[int]) -> list[int]:
        if any(value < 0 for value in values) or len(values) != len(set(values)):
            raise ValueError("lead seconds must be nonnegative and unique")
        return sorted(values, reverse=True)

    @model_validator(mode="after")
    def target_and_date_policy_are_exact(self) -> ReminderPlanInput:
        if (self.subject_ref is None) == (self.subject_change_id is None):
            raise ValueError("reminder plan requires one subject ref or change id")
        if (self.date_trigger_local_time is None) != (self.timezone is None):
            raise ValueError("date trigger local time and timezone are supplied together")
        if self.timezone is not None:
            _zone(self.timezone)
        if "google_popup" in self.delivery_channels and any(
            lead > 2_419_200 or lead % 60 != 0 for lead in self.lead_seconds
        ):
            raise ValueError(
                "Google popup leads must be whole minutes through 28 days"
            )
        return self


class ReminderPlanPatchInput(StrictModel):
    subject_ref: EventRef | TemporalBindingRef | None = None
    subject_change_id: str | None = Field(default=None, min_length=1, max_length=128)
    delivery_channels: list[Literal["docket_queue", "google_popup"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    lead_seconds: list[int] | None = Field(default=None, min_length=1, max_length=20)
    date_trigger_local_time: time | None = None
    timezone: str | None = None
    canonical_status: CanonicalStatus | None = None

    @field_validator("delivery_channels")
    @classmethod
    def channels_are_unique(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and len(values) != len(set(values)):
            raise ValueError("delivery channels must not contain duplicates")
        return values

    @field_validator("lead_seconds")
    @classmethod
    def leads_are_positive_and_unique(cls, values: list[int] | None) -> list[int] | None:
        if values is not None:
            if any(value < 0 for value in values) or len(values) != len(set(values)):
                raise ValueError("lead seconds must be nonnegative and unique")
            return sorted(values, reverse=True)
        return None

    @model_validator(mode="after")
    def patch_is_exact(self) -> ReminderPlanPatchInput:
        if self.subject_ref is not None and self.subject_change_id is not None:
            raise ValueError("reminder subject uses a ref or change id, not both")
        date_fields = {"date_trigger_local_time", "timezone"}.intersection(
            self.model_fields_set
        )
        if date_fields and date_fields != {"date_trigger_local_time", "timezone"}:
            raise ValueError("date trigger local time and timezone are updated together")
        if self.timezone is not None:
            _zone(self.timezone)
        if not self.model_fields_set:
            raise ValueError("reminder plan patch requires at least one field")
        return self
