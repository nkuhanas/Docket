from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.common import PublicRef, StrictModel
from docket.schemas.tracked_context import ItemRef

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
    parent_entity_change_id: str | None = None
    organization_type: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def fields_match_kind(self) -> EntityCreateSpec:
        if self.is_operator and self.entity_kind != "person":
            raise ValueError("only a Person can be the Operator self-entity")
        organization_fields = (
            self.parent_entity_ref,
            self.parent_entity_change_id,
            self.organization_type,
            self.description,
        )
        if self.entity_kind not in {"organization", "institution"} and any(
            value is not None for value in organization_fields
        ):
            raise ValueError("organization fields require organization or institution")
        if self.parent_entity_ref and self.parent_entity_change_id:
            raise ValueError("parent entity uses a ref or change id, not both")
        return self


class EntityPatchSpec(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=512)
    canonical_status: Literal["active", "historical"] | None = None

    @model_validator(mode="after")
    def has_change(self) -> EntityPatchSpec:
        if not self.model_fields_set:
            raise ValueError("entity patch requires at least one field")
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


class IdentityHandleOnlyCreateSpec(StrictModel):
    ref_id: IdentityRef | None = None
    handle_type: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=1024)
    source_refs: list[PublicRef] = Field(default_factory=list, max_length=25)
    associated_email_refs: list[IdentityRef] = Field(default_factory=list, max_length=25)
    associated_email_change_ids: list[str] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def associations_match_handle_type(self) -> IdentityHandleOnlyCreateSpec:
        if self.handle_type != "sender_label" and (
            self.associated_email_refs or self.associated_email_change_ids
        ):
            raise ValueError("email associations require a sender_label handle")
        return self


class IdentityAssociationPatchSpec(StrictModel):
    add_associated_email_ref: IdentityRef | None = None
    add_associated_email_change_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    remove_associated_email_ref: IdentityRef | None = None

    @model_validator(mode="after")
    def has_exactly_one_operation(self) -> IdentityAssociationPatchSpec:
        supplied = sum(
            value is not None
            for value in (
                self.add_associated_email_ref,
                self.add_associated_email_change_id,
                self.remove_associated_email_ref,
            )
        )
        if supplied != 1:
            raise ValueError("identity association patch requires exactly one operation")
        return self


class IdentityResolutionBasis(StrictModel):
    kind: Literal[
        "exact_identity_handle",
        "operator_alias",
        "provider_authoritative",
        "explicit_entity_ref",
        "operator_selection",
    ]
    utterance_ref: Annotated[
        str, Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")
    ] | None = None
    basis_ref: PublicRef | None = None

    @model_validator(mode="after")
    def has_exact_basis(self) -> IdentityResolutionBasis:
        if self.kind == "operator_selection":
            if self.utterance_ref is None or self.basis_ref is not None:
                raise ValueError("operator_selection requires only utterance_ref")
        elif self.basis_ref is None or self.utterance_ref is not None:
            raise ValueError("non-selection identity basis requires only basis_ref")
        return self


class IdentityBindingBindSpec(StrictModel):
    entity_ref: EntityRef | None = None
    entity_change_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    resolution_basis: IdentityResolutionBasis

    @model_validator(mode="after")
    def target_is_exact(self) -> IdentityBindingBindSpec:
        if (self.entity_ref is None) == (self.entity_change_id is None):
            raise ValueError("identity binding requires one entity_ref or entity_change_id")
        return self


class EmptyMutationSpec(StrictModel):
    pass


class AssertionUpdateSpec(StrictModel):
    valid_to: date | None = None
    status: Literal["active", "historical"] | None = None

    @model_validator(mode="after")
    def has_change(self) -> AssertionUpdateSpec:
        if not self.model_fields_set:
            raise ValueError("assertion update requires at least one field")
        return self


class AffiliationCreateSpec(StrictModel):
    subject_ref: EntityRef | None = None
    subject_change_id: str | None = None
    organization_ref: EntityRef | None = None
    organization_change_id: str | None = None
    role: str | None = Field(default=None, max_length=512)
    domain: str | None = Field(default=None, max_length=512)
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "historical"] = "active"

    @model_validator(mode="after")
    def interval_is_ordered(self) -> AffiliationCreateSpec:
        if (self.subject_ref is None) == (self.subject_change_id is None):
            raise ValueError("affiliation requires one subject ref or change id")
        if (self.organization_ref is None) == (self.organization_change_id is None):
            raise ValueError("affiliation requires one organization ref or change id")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


class RelationshipCreateSpec(StrictModel):
    subject_ref: EntityRef | None = None
    subject_change_id: str | None = None
    object_ref: EntityRef | None = None
    object_change_id: str | None = None
    relationship_type: str | None = Field(default=None, max_length=128)
    context: str | None = Field(default=None, max_length=4000)
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "historical"] = "active"

    @model_validator(mode="after")
    def relation_is_valid(self) -> RelationshipCreateSpec:
        if (self.subject_ref is None) == (self.subject_change_id is None):
            raise ValueError("relationship requires one subject ref or change id")
        if (self.object_ref is None) == (self.object_change_id is None):
            raise ValueError("relationship requires one object ref or change id")
        if self.subject_ref is not None and self.subject_ref == self.object_ref:
            raise ValueError("a Relationship requires two different entities")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


class FactCreateSpec(StrictModel):
    subject_ref: EntityRef | ItemRef | None = None
    subject_change_id: str | None = None
    predicate: str = Field(min_length=1, max_length=255)
    value_json: Any
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "historical"] = "active"

    @model_validator(mode="after")
    def interval_is_ordered(self) -> FactCreateSpec:
        if (self.subject_ref is None) == (self.subject_change_id is None):
            raise ValueError("fact requires one subject ref or change id")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self


class InteractionParticipantInput(StrictModel):
    entity_ref: EntityRef | None = None
    entity_change_id: str | None = None
    role: str = Field(default="participant", min_length=1, max_length=128)

    @model_validator(mode="after")
    def target_is_exact(self) -> InteractionParticipantInput:
        if (self.entity_ref is None) == (self.entity_change_id is None):
            raise ValueError("participant requires one entity ref or change id")
        return self


class InteractionCreateSpec(StrictModel):
    interaction_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    ended_at: datetime | None = None
    summary: str = Field(min_length=1, max_length=4000)
    participants: list[InteractionParticipantInput] = Field(min_length=1, max_length=100)
    organization_refs: list[EntityRef] = Field(default_factory=list, max_length=25)
    organization_change_ids: list[str] = Field(default_factory=list, max_length=25)
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
        if self.organization_refs and self.organization_change_ids:
            raise ValueError("interaction organizations use refs or change ids, not both")
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
