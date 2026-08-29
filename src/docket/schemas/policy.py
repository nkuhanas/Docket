from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from docket.domain.public_refs import parse_public_ref
from docket.schemas.common import PublicRef, StrictModel
from docket.schemas.registry import EntityRef, EventRef

PreferenceRef = Annotated[str, Field(pattern=r"^pref_[0-9A-HJKMNP-TV-Z]{26}$")]
LaneRef = Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")]


class PreferenceCreateSpec(StrictModel):
    preference_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,254}$")
    policy_kind: Literal["behavior", "suppression", "calendar_route"]
    target_type: Literal["global", "entity", "identity", "source", "semantic_class"]
    target_ref: PublicRef | None = None
    target_change_id: str | None = None
    semantic_class: str | None = Field(default=None, min_length=1, max_length=64)
    policy_text: str = Field(min_length=1, max_length=4000)
    policy_json: dict[str, Any] = Field(default_factory=dict)
    scope_json: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10_000)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Literal["active"] = "active"

    @model_validator(mode="after")
    def target_is_explicit(self) -> PreferenceCreateSpec:
        if self.target_type in {"entity", "identity", "source"}:
            if (self.target_ref is None) == (self.target_change_id is None):
                raise ValueError(
                    "entity, identity, and source targets require one ref or change id"
                )
        elif self.target_ref is not None or self.target_change_id is not None:
            raise ValueError("global and semantic-class targets omit exact target values")
        if self.target_ref is not None:
            prefix, _payload = parse_public_ref(self.target_ref)
            expected_prefixes = {
                "entity": {"ent"},
                "identity": {"idn"},
                "source": {"src"},
            }
            if prefix not in expected_prefixes[self.target_type]:
                raise ValueError("preference target_ref type does not match target_type")
        if self.target_type == "semantic_class" and self.semantic_class is None:
            raise ValueError("semantic_class targets require semantic_class")
        if self.target_type != "semantic_class" and self.semantic_class is not None:
            raise ValueError("semantic_class is valid only for a semantic-class target")
        if (
            self.policy_kind == "calendar_route"
            and not isinstance(self.policy_json.get("lane_ref"), str)
        ):
            raise ValueError("calendar_route policy requires policy_json.lane_ref")
        if (
            self.policy_kind == "suppression"
            and self.policy_json.get("disposition") != "suppress"
        ):
            raise ValueError(
                "suppression policy requires policy_json.disposition='suppress'"
            )
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class PreferencePatchSpec(StrictModel):
    policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    policy_json: dict[str, Any] | None = None
    scope_json: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Literal["active", "historical", "retracted"] | None = None

    @model_validator(mode="after")
    def has_change(self) -> PreferencePatchSpec:
        if not self.model_fields_set:
            raise ValueError("preference patch requires at least one field")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class CalendarLaneCreateSpec(StrictModel):
    ref_id: LaneRef | None = None
    account_id: UUID
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    display_name: str = Field(min_length=1, max_length=255)
    color_hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    provider_calendar_binding: str | None = Field(
        default=None, min_length=1, max_length=1024
    )
    operator_policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    @field_validator("color_hex")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return value.upper()


class CalendarLanePatchSpec(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    operator_policy_text: str | None = Field(default=None, min_length=1, max_length=4000)
    metadata_json: dict[str, Any] | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def has_change(self) -> CalendarLanePatchSpec:
        if not self.model_fields_set:
            raise ValueError("CalendarLane patch requires at least one field")
        return self


class LaneRoutingDecisionCreateSpec(StrictModel):
    lane_ref: LaneRef | None = None
    lane_change_id: str | None = None
    event_ref: EventRef | None = None
    event_change_id: str | None = None
    organization_ref: EntityRef | None = None
    recurring_identity: str | None = Field(default=None, min_length=1, max_length=512)
    decision_kind: Literal[
        "explicit_operator",
        "structured_preference",
        "entity_rule",
        "historical_precedent",
        "semantic_inference",
    ]
    applicability_scope: dict[str, Any] = Field(default_factory=dict)
    operator_confirmed: bool = False

    @model_validator(mode="after")
    def has_applicability(self) -> LaneRoutingDecisionCreateSpec:
        if (self.lane_ref is None) == (self.lane_change_id is None):
            raise ValueError("routing decision requires one lane ref or change id")
        if self.event_ref is not None and self.event_change_id is not None:
            raise ValueError("routing decision event uses a ref or change id, not both")
        if not any(
            (
                self.event_ref,
                self.event_change_id,
                self.organization_ref,
                self.recurring_identity,
            )
        ):
            raise ValueError("routing decision requires an event or applicability identity")
        if self.decision_kind == "explicit_operator" and not self.operator_confirmed:
            raise ValueError("explicit Operator routing must be operator_confirmed")
        return self
