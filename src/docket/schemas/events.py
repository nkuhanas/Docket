from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.calendar import (
    AllDayEventTiming,
    CalendarRecurrenceInput,
    MaterializedCalendarEvent,
    StandaloneCalendarEventInput,
    TimedEventTiming,
)
from docket.schemas.common import StrictModel, validate_refs
from docket.schemas.policy import LaneRef
from docket.schemas.registry import EntityRef
from docket.schemas.tracked_context import ItemRef, TemporalBindingRef


class CanonicalEventCreateSpec(StrictModel):
    canonical_key: str | None = Field(default=None, min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=512)
    event_spec: StandaloneCalendarEventInput
    lane_ref: LaneRef | None = None
    lane_change_id: str | None = None
    routing_decision_ref: Annotated[
        str, Field(pattern=r"^route_[0-9A-HJKMNP-TV-Z]{26}$")
    ] | None = None
    entity_refs: list[EntityRef] = Field(default_factory=list, max_length=100)
    entity_change_ids: list[str] = Field(default_factory=list, max_length=100)
    item_refs: list[ItemRef] = Field(default_factory=list, max_length=100)
    item_change_ids: list[str] = Field(default_factory=list, max_length=100)
    realizes_temporal_binding_refs: list[TemporalBindingRef] = Field(
        default_factory=list, max_length=100
    )
    realizes_temporal_binding_change_ids: list[str] = Field(
        default_factory=list, max_length=100
    )
    context_labels: list[str] = Field(default_factory=list, max_length=25)
    operator_policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    status: Literal["active"] = "active"

    @field_validator("entity_refs", "item_refs", "realizes_temporal_binding_refs")
    @classmethod
    def validate_entity_refs(cls, values: list[str]) -> list[str]:
        return validate_refs(values)

    @field_validator("context_labels")
    @classmethod
    def validate_context_labels(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("context labels must be 1..128 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("context labels must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def dependencies_are_exact(self) -> CanonicalEventCreateSpec:
        if (self.lane_ref is None) == (self.lane_change_id is None):
            raise ValueError("canonical event requires one lane ref or change id")
        if self.item_refs and self.item_change_ids:
            raise ValueError("event Item links use refs or change ids, not both")
        if self.realizes_temporal_binding_refs and self.realizes_temporal_binding_change_ids:
            raise ValueError("realized time links use refs or change ids, not both")
        return self


class MaterializedEventCreateSpec(CanonicalEventCreateSpec):
    event_spec: MaterializedCalendarEvent


class PatchTimedTiming(TimedEventTiming):
    timezone: str = Field(min_length=1, max_length=128)


class PatchAllDayTiming(AllDayEventTiming):
    timezone: str = Field(min_length=1, max_length=128)


class EventFieldPatch(StrictModel):
    """Only supplied fields change. No creation, routing or recurrence defaults."""

    title: str | None = Field(default=None, min_length=1, max_length=512)
    location: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)
    timing: Annotated[
        PatchTimedTiming | PatchAllDayTiming, Field(discriminator="kind")
    ] | None = None

    @field_validator("title", "timing")
    @classmethod
    def required_fields_cannot_be_cleared(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("omit an unchanged required field; it cannot be cleared")
        return value

    @model_validator(mode="after")
    def patch_is_nonempty(self) -> EventFieldPatch:
        if not self.model_fields_set:
            raise ValueError("supply at least one changed event field")
        return self


class EventContextPatch(StrictModel):
    lane_ref: LaneRef | None = None
    lane_change_id: str | None = None
    routing_decision_ref: Annotated[
        str, Field(pattern=r"^route_[0-9A-HJKMNP-TV-Z]{26}$")
    ] | None = None
    routing_decision_change_id: str | None = None
    entity_refs: list[EntityRef] | None = Field(default=None, max_length=100)
    entity_change_ids: list[str] | None = Field(default=None, max_length=100)
    item_refs: list[ItemRef] | None = Field(default=None, max_length=100)
    item_change_ids: list[str] | None = Field(default=None, max_length=100)
    realizes_temporal_binding_refs: list[TemporalBindingRef] | None = Field(
        default=None, max_length=100
    )
    realizes_temporal_binding_change_ids: list[str] | None = Field(
        default=None, max_length=100
    )
    context_labels: list[str] | None = Field(default=None, max_length=25)
    operator_policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    status: Literal["active", "cancelled", "archived"] | None = None

    @field_validator("entity_refs", "item_refs", "realizes_temporal_binding_refs")
    @classmethod
    def validate_entity_refs(cls, values: list[str] | None) -> list[str] | None:
        return validate_refs(values) if values is not None else None

    @field_validator("context_labels")
    @classmethod
    def validate_context_labels(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("context labels must be 1..128 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("context labels must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def dependencies_are_exact(self) -> EventContextPatch:
        if self.lane_ref and self.lane_change_id:
            raise ValueError("lane update uses a ref or change id, not both")
        if self.routing_decision_ref and self.routing_decision_change_id:
            raise ValueError("routing decision uses a ref or change id, not both")
        if self.entity_refs is not None and self.entity_change_ids is not None:
            raise ValueError("entity targets use refs or change ids, not both")
        if self.item_refs is not None and self.item_change_ids is not None:
            raise ValueError("event Item links use refs or change ids, not both")
        if (
            self.realizes_temporal_binding_refs is not None
            and self.realizes_temporal_binding_change_ids is not None
        ):
            raise ValueError("realized time links use refs or change ids, not both")
        if not self.model_fields_set:
            raise ValueError("canonical event patch requires at least one field")
        return self


class CanonicalEventPatchSpec(EventContextPatch):
    event_spec: EventFieldPatch | None = None
    # Recurrence is a separate scheduling capability, never a content-patch field.
    recurrence: CalendarRecurrenceInput | None = None

    @field_validator("event_spec")
    @classmethod
    def event_patch_cannot_be_null(cls, value: EventFieldPatch | None) -> EventFieldPatch:
        if value is None:
            raise ValueError("supply a nonempty field patch or omit event_spec")
        return value

    @model_validator(mode="after")
    def patch_has_an_explicit_effect(self) -> CanonicalEventPatchSpec:
        if not self.model_fields_set:
            raise ValueError("supply an event field patch or a separate typed event change")
        return self


class MaterializedEventPatchSpec(EventContextPatch):
    """Compiler output only; never exposed as a model input or old-shape decoder."""

    title: str | None = Field(default=None, min_length=1, max_length=512)
    event_spec: MaterializedCalendarEvent | None = None


class ProviderOperationParameters(StrictModel):
    """Optional operation-specific hints; canonical targets remain authoritative."""

    reminder_plan: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=1000)
