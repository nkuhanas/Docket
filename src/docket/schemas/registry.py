from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.authority import PublicRef, StrictModel

EntityRef = Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]
EventRef = Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")]
IdentityRef = Annotated[str, Field(pattern=r"^idn_[0-9A-HJKMNP-TV-Z]{26}$")]


class EntityCreateSpec(StrictModel):
    ref_id: EntityRef | None = None
    entity_kind: Literal[
        "person",
        "organization",
        "institution",
        "place",
        "course",
        "course_section",
        "project",
    ]
    display_name: str = Field(min_length=1, max_length=512)
    preferred_name: str | None = Field(default=None, max_length=512)
    pronouns: str | None = Field(default=None, max_length=128)
    is_operator: bool = False
    parent_entity_ref: EntityRef | None = None
    organization_type: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def fields_match_kind(self) -> EntityCreateSpec:
        if self.is_operator and self.entity_kind != "person":
            raise ValueError("only a Person can be the Operator self-entity")
        organization_fields = (
            self.parent_entity_ref,
            self.organization_type,
            self.description,
        )
        if self.entity_kind not in {"organization", "institution"} and any(
            value is not None for value in organization_fields
        ):
            raise ValueError("organization fields require organization or institution")
        return self


class IdentityHandleCreateSpec(StrictModel):
    ref_id: IdentityRef | None = None
    handle_type: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=1024)
    entity_ref: EntityRef | None = None
    binding_rule: (
        Literal[
            "exact_identity_handle",
            "operator_alias",
            "provider_authoritative",
            "explicit_entity_ref",
            "operator_selection",
        ]
        | None
    ) = None
    source_refs: list[PublicRef] = Field(default_factory=list, max_length=25)
    associated_email_refs: list[IdentityRef] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def binding_is_complete(self) -> IdentityHandleCreateSpec:
        if (self.entity_ref is None) != (self.binding_rule is None):
            raise ValueError("entity_ref and binding_rule must be supplied together")
        if self.handle_type == "sender_label" and self.entity_ref is not None:
            raise ValueError("sender_label is an index handle and cannot bind an Entity")
        if self.handle_type != "sender_label" and self.associated_email_refs:
            raise ValueError("associated_email_refs require a sender_label handle")
        if len(self.associated_email_refs) != len(set(self.associated_email_refs)):
            raise ValueError("associated_email_refs must not contain duplicates")
        return self


class AffiliationCreateSpec(StrictModel):
    subject_ref: EntityRef
    organization_ref: EntityRef
    role: str | None = Field(default=None, max_length=512)
    domain: str | None = Field(default=None, max_length=512)
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "historical"] = "active"

    @model_validator(mode="after")
    def interval_is_ordered(self) -> AffiliationCreateSpec:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


class RelationshipCreateSpec(StrictModel):
    subject_ref: EntityRef
    object_ref: EntityRef
    relationship_type: str | None = Field(default=None, max_length=128)
    context: str | None = Field(default=None, max_length=4000)
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "historical"] = "active"

    @model_validator(mode="after")
    def relation_is_valid(self) -> RelationshipCreateSpec:
        if self.subject_ref == self.object_ref:
            raise ValueError("a Relationship requires two different entities")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


class FactCreateSpec(StrictModel):
    subject_ref: EntityRef
    predicate: str = Field(min_length=1, max_length=255)
    value_json: Any
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "historical"] = "active"

    @model_validator(mode="after")
    def interval_is_ordered(self) -> FactCreateSpec:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


class InteractionParticipantInput(StrictModel):
    entity_ref: EntityRef
    role: str = Field(default="participant", min_length=1, max_length=128)


class InteractionCreateSpec(StrictModel):
    interaction_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    ended_at: datetime | None = None
    summary: str = Field(min_length=1, max_length=4000)
    participants: list[InteractionParticipantInput] = Field(min_length=1, max_length=100)
    organization_refs: list[EntityRef] = Field(default_factory=list, max_length=25)
    event_ref: EventRef | None = None
    place_ref: EntityRef | None = None
    source_refs: list[PublicRef] = Field(default_factory=list, max_length=25)

    @field_validator("organization_refs")
    @classmethod
    def organizations_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("organization_refs must not contain duplicates")
        return values

    @model_validator(mode="after")
    def interaction_is_valid(self) -> InteractionCreateSpec:
        participant_keys = [
            (participant.entity_ref, participant.role) for participant in self.participants
        ]
        if len(participant_keys) != len(set(participant_keys)):
            raise ValueError("interaction participants must be unique by entity and role")
        if self.ended_at is not None and self.ended_at < self.occurred_at:
            raise ValueError("ended_at must not precede occurred_at")
        return self


class IdentityResolutionRequest(StrictModel):
    handle_type: str | None = Field(default=None, min_length=1, max_length=64)
    handle_value: str | None = Field(default=None, min_length=1, max_length=1024)
    mention: str | None = Field(default=None, min_length=1, max_length=512)
    entity_kind: str | None = Field(default=None, min_length=1, max_length=32)
    explicit_entity_ref: EntityRef | None = None
    operator_selected_ref: EntityRef | None = None
    provider_authoritative: bool = False
    basis_refs: list[PublicRef] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def has_resolution_input(self) -> IdentityResolutionRequest:
        if self.handle_type is None and self.handle_value is not None:
            raise ValueError("handle_type is required with handle_value")
        if self.handle_value is None and self.handle_type is not None:
            raise ValueError("handle_value is required with handle_type")
        if not any(
            (
                self.handle_value,
                self.mention,
                self.explicit_entity_ref,
                self.operator_selected_ref,
            )
        ):
            raise ValueError("at least one deterministic resolution input is required")
        return self
