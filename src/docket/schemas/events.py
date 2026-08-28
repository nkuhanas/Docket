from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from docket.schemas.authority import PublicRef, StrictModel, _validate_refs
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.policy import LaneRef


class CanonicalEventCreateSpec(StrictModel):
    canonical_key: str | None = Field(default=None, min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=512)
    event_spec: StandaloneCalendarEventInput
    lane_ref: LaneRef
    routing_decision_ref: PublicRef | None = None
    entity_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    context_labels: list[str] = Field(default_factory=list, max_length=25)
    operator_policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    status: Literal["active"] = "active"

    @field_validator("entity_refs")
    @classmethod
    def validate_entity_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)

    @field_validator("context_labels")
    @classmethod
    def validate_context_labels(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("context labels must be 1..128 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("context labels must not contain duplicates")
        return normalized


class CanonicalEventPatchSpec(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    event_spec: StandaloneCalendarEventInput | None = None
    lane_ref: LaneRef | None = None
    routing_decision_ref: PublicRef | None = None
    entity_refs: list[PublicRef] | None = Field(default=None, max_length=100)
    context_labels: list[str] | None = Field(default=None, max_length=25)
    operator_policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    status: Literal["active", "cancelled", "archived"] | None = None

    @field_validator("entity_refs")
    @classmethod
    def validate_entity_refs(cls, values: list[str] | None) -> list[str] | None:
        return _validate_refs(values) if values is not None else None

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


class ProviderOperationParameters(StrictModel):
    """Optional operation-specific hints; canonical targets remain authoritative."""

    reminder_plan: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=1000)
