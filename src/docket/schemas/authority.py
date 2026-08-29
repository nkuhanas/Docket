from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.domain.public_refs import is_public_ref
from docket.schemas.common import PublicRef, StrictModel, validate_refs
from docket.schemas.events import CanonicalEventCreateSpec, CanonicalEventPatchSpec
from docket.schemas.policy import (
    CalendarLaneCreateSpec,
    CalendarLanePatchSpec,
    LaneRoutingDecisionCreateSpec,
    PreferenceCreateSpec,
    PreferencePatchSpec,
)
from docket.schemas.registry import (
    AffiliationCreateSpec,
    AssertionUpdateSpec,
    EmptyMutationSpec,
    EntityCreateSpec,
    EntityPatchSpec,
    FactCreateSpec,
    IdentityAssociationPatchSpec,
    IdentityBindingBindSpec,
    IdentityHandleOnlyCreateSpec,
    InteractionCreateSpec,
    RelationshipCreateSpec,
)

UtteranceRef = Annotated[str, Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")]
StatementRef = Annotated[str, Field(pattern=r"^stm_[0-9A-HJKMNP-TV-Z]{26}$")]
SessionRef = Annotated[str, Field(pattern=r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")]
ChangeSetRef = Annotated[str, Field(pattern=r"^chg_[0-9A-HJKMNP-TV-Z]{26}$")]
SemanticRequestRef = Annotated[str, Field(pattern=r"^sreq_[0-9A-HJKMNP-TV-Z]{26}$")]
ConflictRef = Annotated[str, Field(pattern=r"^cnf_[0-9A-HJKMNP-TV-Z]{26}$")]
AttentionCaseRef = Annotated[str, Field(pattern=r"^case_[0-9A-HJKMNP-TV-Z]{26}$")]
AttentionCaseRevisionRef = Annotated[
    str, Field(pattern=r"^caserev_[0-9A-HJKMNP-TV-Z]{26}$")
]
CaseItemRef = Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")]

def _validate_refs(values: list[str], *, provenance_only: bool = False) -> list[str]:
    return validate_refs(values, provenance_only=provenance_only)


class StatementInput(StrictModel):
    statement_kind: str = Field(min_length=1, max_length=128)
    subject_refs: list[PublicRef] = Field(min_length=1, max_length=25)
    predicate: str = Field(min_length=1, max_length=255)
    value_json: Any
    affected_fields: list[str] = Field(min_length=1, max_length=50)
    effective_from: date | None = None
    effective_to: date | None = None
    interpretation_json: dict[str, Any] = Field(default_factory=dict)
    interpreter_version: str = Field(min_length=1, max_length=255)

    @field_validator("subject_refs")
    @classmethod
    def validate_subject_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)

    @field_validator("affected_fields")
    @classmethod
    def unique_affected_fields(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("affected_fields must not contain duplicates")
        if any(not value or len(value) > 255 for value in values):
            raise ValueError("affected_fields entries must be 1..255 characters")
        return values

    @model_validator(mode="after")
    def effective_interval_is_ordered(self) -> StatementInput:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to must not precede effective_from")
        return self


class StatementRelationInput(StrictModel):
    source_statement_ref: StatementRef
    target_statement_ref: StatementRef
    relation_kind: Literal[
        "affirms", "amends", "supersedes", "contradicts", "retracts", "scopes"
    ]

    @model_validator(mode="after")
    def statements_differ(self) -> StatementRelationInput:
        if self.source_statement_ref == self.target_statement_ref:
            raise ValueError("a statement cannot relate to itself")
        return self


class IntentSessionOpen(StrictModel):
    source_utterance_ref: UtteranceRef
    case_refs: list[PublicRef] = Field(default_factory=list, max_length=25)
    case_revision_refs: list[PublicRef] = Field(default_factory=list, max_length=25)
    brief_ref: PublicRef | None = None
    trusted_context_refs: list[PublicRef] = Field(default_factory=list, max_length=50)

    @field_validator("case_refs", "case_revision_refs", "trusted_context_refs")
    @classmethod
    def validate_public_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)


class IntentTurnAppend(StrictModel):
    intent_session_ref: SessionRef
    utterance_ref: UtteranceRef
    statements: list[StatementInput] = Field(default_factory=list, max_length=100)
    relations: list[StatementRelationInput] = Field(default_factory=list, max_length=100)
    context_refs: list[PublicRef] = Field(default_factory=list, max_length=50)
    tool_call_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    agent_response_ref: PublicRef | None = None
    response_disposition: Literal["pending", "final_response", "no_response"] = "pending"
    resolved_intent_json: dict[str, Any] = Field(default_factory=dict)
    blocking_clarifications: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    semantic_request_ref: SemanticRequestRef | None = None
    authority_substitutions: dict[str, UtteranceRef] = Field(default_factory=dict)

    @field_validator("context_refs", "tool_call_refs")
    @classmethod
    def validate_public_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)


class IntentTurnFinalize(StrictModel):
    turn_ref: Annotated[str, Field(pattern=r"^turn_[0-9A-HJKMNP-TV-Z]{26}$")]
    tool_call_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    agent_response_ref: PublicRef | None = None
    resulting_semantic_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    response_disposition: Literal["final_response", "no_response"]

    @field_validator("tool_call_refs", "resulting_semantic_refs")
    @classmethod
    def validate_public_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)

    @model_validator(mode="after")
    def response_binding_matches_disposition(self) -> IntentTurnFinalize:
        if self.response_disposition == "final_response" and self.agent_response_ref is None:
            raise ValueError("final_response requires agent_response_ref")
        if self.response_disposition == "no_response" and self.agent_response_ref is not None:
            raise ValueError("no_response cannot include agent_response_ref")
        return self


class MutationBase(StrictModel):
    change_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    affected_fields: list[str] = Field(min_length=1, max_length=50)
    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)

    @field_validator("basis_refs")
    @classmethod
    def validate_basis_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values, provenance_only=True)



class EntityCreate(MutationBase):
    mutation_type: Literal["entity_create"] = "entity_create"
    action: Literal["create"]
    object_type: Literal["entity"]
    object_ref: None = None
    create_spec: EntityCreateSpec
    payload: None = None


class EntityModify(MutationBase):
    mutation_type: Literal["entity_modify"] = "entity_modify"
    action: Literal["update", "supersede"]
    object_type: Literal["entity"]
    object_ref: Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EntityPatchSpec


class EntityRetract(MutationBase):
    mutation_type: Literal["entity_retract"] = "entity_retract"
    action: Literal["retract"]
    object_type: Literal["entity"]
    object_ref: Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class IdentityHandleCreate(MutationBase):
    mutation_type: Literal["identity_handle_create"] = "identity_handle_create"
    action: Literal["create"]
    object_type: Literal["identity_binding"]
    object_ref: None = None
    create_spec: IdentityHandleOnlyCreateSpec
    payload: None = None


class IdentityHandleModify(MutationBase):
    mutation_type: Literal["identity_handle_modify"] = "identity_handle_modify"
    action: Literal["update", "supersede"]
    object_type: Literal["identity_binding"]
    object_ref: Annotated[str, Field(pattern=r"^idn_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: IdentityAssociationPatchSpec


class IdentityBindingBind(MutationBase):
    mutation_type: Literal["identity_binding_bind"] = "identity_binding_bind"
    action: Literal["bind"]
    object_type: Literal["identity_binding"]
    object_ref: Annotated[
        str, Field(pattern=r"^idn_[0-9A-HJKMNP-TV-Z]{26}$")
    ] | None = None
    object_change_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    create_spec: None = None
    payload: IdentityBindingBindSpec

    @model_validator(mode="after")
    def target_is_exact(self) -> IdentityBindingBind:
        if (self.object_ref is None) == (self.object_change_id is None):
            raise ValueError("identity binding target uses object_ref or object_change_id")
        return self


class IdentityBindingUnbind(MutationBase):
    mutation_type: Literal["identity_binding_unbind"] = "identity_binding_unbind"
    action: Literal["unbind"]
    object_type: Literal["identity_binding"]
    object_ref: Annotated[str, Field(pattern=r"^idn_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class IdentityHandleRetract(MutationBase):
    mutation_type: Literal["identity_handle_retract"] = "identity_handle_retract"
    action: Literal["retract"]
    object_type: Literal["identity_binding"]
    object_ref: Annotated[str, Field(pattern=r"^idn_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class AffiliationCreate(MutationBase):
    mutation_type: Literal["affiliation_create"] = "affiliation_create"
    action: Literal["create"]
    object_type: Literal["affiliation"]
    object_ref: None = None
    create_spec: AffiliationCreateSpec
    payload: None = None


class AffiliationUpdate(MutationBase):
    mutation_type: Literal["affiliation_update"] = "affiliation_update"
    action: Literal["update"]
    object_type: Literal["affiliation"]
    object_ref: Annotated[str, Field(pattern=r"^aff_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: AssertionUpdateSpec


class AffiliationSupersede(MutationBase):
    mutation_type: Literal["affiliation_supersede"] = "affiliation_supersede"
    action: Literal["supersede"]
    object_type: Literal["affiliation"]
    object_ref: Annotated[str, Field(pattern=r"^aff_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: Annotated[dict[str, AffiliationCreateSpec], Field(min_length=1, max_length=1)]

    @field_validator("payload")
    @classmethod
    def replacement_only(
        cls, value: dict[str, AffiliationCreateSpec]
    ) -> dict[str, AffiliationCreateSpec]:
        if set(value) != {"replacement"}:
            raise ValueError("supersede payload contains only replacement")
        return value


class AffiliationRetract(MutationBase):
    mutation_type: Literal["affiliation_retract"] = "affiliation_retract"
    action: Literal["retract"]
    object_type: Literal["affiliation"]
    object_ref: Annotated[str, Field(pattern=r"^aff_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class RelationshipCreate(MutationBase):
    mutation_type: Literal["relationship_create"] = "relationship_create"
    action: Literal["create"]
    object_type: Literal["relationship"]
    object_ref: None = None
    create_spec: RelationshipCreateSpec
    payload: None = None


class RelationshipUpdate(MutationBase):
    mutation_type: Literal["relationship_update"] = "relationship_update"
    action: Literal["update"]
    object_type: Literal["relationship"]
    object_ref: Annotated[str, Field(pattern=r"^rel_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: AssertionUpdateSpec


class RelationshipSupersede(MutationBase):
    mutation_type: Literal["relationship_supersede"] = "relationship_supersede"
    action: Literal["supersede"]
    object_type: Literal["relationship"]
    object_ref: Annotated[str, Field(pattern=r"^rel_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: Annotated[dict[str, RelationshipCreateSpec], Field(min_length=1, max_length=1)]

    @field_validator("payload")
    @classmethod
    def replacement_only(
        cls, value: dict[str, RelationshipCreateSpec]
    ) -> dict[str, RelationshipCreateSpec]:
        if set(value) != {"replacement"}:
            raise ValueError("supersede payload contains only replacement")
        return value


class RelationshipRetract(MutationBase):
    mutation_type: Literal["relationship_retract"] = "relationship_retract"
    action: Literal["retract"]
    object_type: Literal["relationship"]
    object_ref: Annotated[str, Field(pattern=r"^rel_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class FactCreate(MutationBase):
    mutation_type: Literal["fact_create"] = "fact_create"
    action: Literal["create"]
    object_type: Literal["fact"]
    object_ref: None = None
    create_spec: FactCreateSpec
    payload: None = None


class FactUpdate(MutationBase):
    mutation_type: Literal["fact_update"] = "fact_update"
    action: Literal["update"]
    object_type: Literal["fact"]
    object_ref: Annotated[str, Field(pattern=r"^fact_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: AssertionUpdateSpec


class FactSupersede(MutationBase):
    mutation_type: Literal["fact_supersede"] = "fact_supersede"
    action: Literal["supersede"]
    object_type: Literal["fact"]
    object_ref: Annotated[str, Field(pattern=r"^fact_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: Annotated[dict[str, FactCreateSpec], Field(min_length=1, max_length=1)]

    @field_validator("payload")
    @classmethod
    def replacement_only(
        cls, value: dict[str, FactCreateSpec]
    ) -> dict[str, FactCreateSpec]:
        if set(value) != {"replacement"}:
            raise ValueError("supersede payload contains only replacement")
        return value


class FactRetract(MutationBase):
    mutation_type: Literal["fact_retract"] = "fact_retract"
    action: Literal["retract"]
    object_type: Literal["fact"]
    object_ref: Annotated[str, Field(pattern=r"^fact_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class InteractionCreate(MutationBase):
    mutation_type: Literal["interaction_create"] = "interaction_create"
    action: Literal["create"]
    object_type: Literal["interaction"]
    object_ref: None = None
    create_spec: InteractionCreateSpec
    payload: None = None


class PreferenceCreate(MutationBase):
    mutation_type: Literal["preference_create"] = "preference_create"
    action: Literal["create"]
    object_type: Literal["preference"]
    object_ref: None = None
    create_spec: PreferenceCreateSpec
    payload: None = None


class PreferenceModify(MutationBase):
    mutation_type: Literal["preference_modify"] = "preference_modify"
    action: Literal["update", "supersede"]
    object_type: Literal["preference"]
    object_ref: Annotated[str, Field(pattern=r"^pref_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: PreferencePatchSpec


class PreferenceRetract(MutationBase):
    mutation_type: Literal["preference_retract"] = "preference_retract"
    action: Literal["retract"]
    object_type: Literal["preference"]
    object_ref: Annotated[str, Field(pattern=r"^pref_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class CalendarLaneCreate(MutationBase):
    mutation_type: Literal["calendar_lane_create"] = "calendar_lane_create"
    action: Literal["create"]
    object_type: Literal["calendar_lane"]
    object_ref: None = None
    create_spec: CalendarLaneCreateSpec
    payload: None = None


class CalendarLaneModify(MutationBase):
    mutation_type: Literal["calendar_lane_modify"] = "calendar_lane_modify"
    action: Literal["update", "supersede"]
    object_type: Literal["calendar_lane"]
    object_ref: Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: CalendarLanePatchSpec


class CalendarLaneRetract(MutationBase):
    mutation_type: Literal["calendar_lane_retract"] = "calendar_lane_retract"
    action: Literal["retract"]
    object_type: Literal["calendar_lane"]
    object_ref: Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class LaneRoutingDecisionCreate(MutationBase):
    mutation_type: Literal["lane_routing_decision_create"] = (
        "lane_routing_decision_create"
    )
    action: Literal["create"]
    object_type: Literal["lane_routing_decision"]
    object_ref: None = None
    create_spec: LaneRoutingDecisionCreateSpec
    payload: None = None


class CanonicalEventCreate(MutationBase):
    mutation_type: Literal["canonical_event_create"] = "canonical_event_create"
    action: Literal["create"]
    object_type: Literal["canonical_event"]
    object_ref: None = None
    create_spec: CanonicalEventCreateSpec
    payload: None = None


class CanonicalEventModify(MutationBase):
    mutation_type: Literal["canonical_event_modify"] = "canonical_event_modify"
    action: Literal["update", "supersede"]
    object_type: Literal["canonical_event"]
    object_ref: Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: CanonicalEventPatchSpec


class CanonicalEventCancel(MutationBase):
    mutation_type: Literal["canonical_event_cancel"] = "canonical_event_cancel"
    action: Literal["retract"]
    object_type: Literal["canonical_event"]
    object_ref: Annotated[str, Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


type RegistryMutation = (
    EntityCreate
    | EntityModify
    | EntityRetract
    | IdentityHandleCreate
    | IdentityHandleModify
    | IdentityBindingBind
    | IdentityBindingUnbind
    | IdentityHandleRetract
    | AffiliationCreate
    | AffiliationUpdate
    | AffiliationSupersede
    | AffiliationRetract
    | RelationshipCreate
    | RelationshipUpdate
    | RelationshipSupersede
    | RelationshipRetract
    | FactCreate
    | FactUpdate
    | FactSupersede
    | FactRetract
    | InteractionCreate
)
type PreferenceMutation = PreferenceCreate | PreferenceModify | PreferenceRetract
type LaneMutation = (
    CalendarLaneCreate
    | CalendarLaneModify
    | CalendarLaneRetract
    | LaneRoutingDecisionCreate
)
type EventMutation = CanonicalEventCreate | CanonicalEventModify | CanonicalEventCancel
type CanonicalMutation = (
    EntityCreate
    | EntityModify
    | EntityRetract
    | IdentityHandleCreate
    | IdentityHandleModify
    | IdentityBindingBind
    | IdentityBindingUnbind
    | IdentityHandleRetract
    | AffiliationCreate
    | AffiliationUpdate
    | AffiliationSupersede
    | AffiliationRetract
    | RelationshipCreate
    | RelationshipUpdate
    | RelationshipSupersede
    | RelationshipRetract
    | FactCreate
    | FactUpdate
    | FactSupersede
    | FactRetract
    | InteractionCreate
    | PreferenceCreate
    | PreferenceModify
    | PreferenceRetract
    | CalendarLaneCreate
    | CalendarLaneModify
    | CalendarLaneRetract
    | LaneRoutingDecisionCreate
    | CanonicalEventCreate
    | CanonicalEventModify
    | CanonicalEventCancel
)

type RegistryChangeInput = Annotated[RegistryMutation, Field(discriminator="mutation_type")]
type PreferenceChangeInput = Annotated[
    PreferenceMutation, Field(discriminator="mutation_type")
]
type LaneChangeInput = Annotated[LaneMutation, Field(discriminator="mutation_type")]
type EventChangeInput = Annotated[EventMutation, Field(discriminator="mutation_type")]
type CanonicalChangeInput = Annotated[
    CanonicalMutation, Field(discriminator="mutation_type")
]


class AttentionCaseItemDisposition(StrictModel):
    item_ref: CaseItemRef
    disposition: Literal["resolved", "rejected"]


class AttentionCaseResolutionInput(StrictModel):
    mutation_type: Literal["attention_case_resolution"] = "attention_case_resolution"
    change_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    action: Literal["update"]
    object_type: Literal["attention_case_resolution"]
    object_ref: AttentionCaseRef
    case_revision_ref: AttentionCaseRevisionRef
    case_outcome: Literal["keep_open", "resolved", "suppressed", "cancelled"]
    item_dispositions: list[AttentionCaseItemDisposition] = Field(max_length=25)
    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)

    @field_validator("basis_refs")
    @classmethod
    def validate_basis_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values, provenance_only=True)

    @field_validator("item_dispositions")
    @classmethod
    def unique_item_dispositions(
        cls, values: list[AttentionCaseItemDisposition]
    ) -> list[AttentionCaseItemDisposition]:
        refs = [item.item_ref for item in values]
        if len(refs) != len(set(refs)):
            raise ValueError("item_dispositions must not contain duplicate CaseItems")
        return values


type ResolutionChangeInput = AttentionCaseResolutionInput


class ProviderIntentInput(StrictModel):
    intent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    operation_type: str = Field(min_length=1, max_length=128)
    account_ref: PublicRef | None = None
    provider_binding: str | None = Field(default=None, min_length=1, max_length=512)
    canonical_target_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    canonical_target_change_ids: list[str] = Field(default_factory=list, max_length=100)
    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=512)
    parameters: dict[str, Any]

    @field_validator("canonical_target_refs")
    @classmethod
    def validate_target_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)

    @field_validator("basis_refs")
    @classmethod
    def validate_basis_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values, provenance_only=True)

    @model_validator(mode="after")
    def targets_are_resolved(self) -> ProviderIntentInput:
        if (self.account_ref is None) == (self.provider_binding is None):
            raise ValueError("provider intent requires exactly one provider target binding")
        if not self.canonical_target_refs and not self.canonical_target_change_ids:
            raise ValueError("provider intent requires at least one canonical target")
        if len(self.canonical_target_change_ids) != len(
            set(self.canonical_target_change_ids)
        ):
            raise ValueError("canonical_target_change_ids must not contain duplicates")
        return self


class ChangeSetContent(StrictModel):
    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)
    expected_versions: dict[PublicRef, int] = Field(default_factory=dict, max_length=100)
    registry_changes: list[RegistryChangeInput] = Field(default_factory=list, max_length=100)
    preference_changes: list[PreferenceChangeInput] = Field(
        default_factory=list, max_length=100
    )
    lane_changes: list[LaneChangeInput] = Field(default_factory=list, max_length=100)
    event_changes: list[EventChangeInput] = Field(default_factory=list, max_length=100)
    resolution_changes: list[ResolutionChangeInput] = Field(
        default_factory=list, max_length=100
    )
    provider_intents: list[ProviderIntentInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_mutation_tags(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        groups = (
            "registry_changes",
            "preference_changes",
            "lane_changes",
            "event_changes",
            "resolution_changes",
        )
        aliases = {
            ("entity", "update"): "entity_modify",
            ("entity", "supersede"): "entity_modify",
            ("identity_binding", "create"): "identity_handle_create",
            ("identity_binding", "update"): "identity_handle_modify",
            ("identity_binding", "supersede"): "identity_handle_modify",
            ("identity_binding", "bind"): "identity_binding_bind",
            ("identity_binding", "unbind"): "identity_binding_unbind",
            ("identity_binding", "retract"): "identity_handle_retract",
            ("preference", "update"): "preference_modify",
            ("preference", "supersede"): "preference_modify",
            ("calendar_lane", "update"): "calendar_lane_modify",
            ("calendar_lane", "supersede"): "calendar_lane_modify",
            ("canonical_event", "update"): "canonical_event_modify",
            ("canonical_event", "supersede"): "canonical_event_modify",
            ("canonical_event", "retract"): "canonical_event_cancel",
            ("attention_case_resolution", "update"): "attention_case_resolution",
        }
        for group in groups:
            changes = result.get(group)
            if not isinstance(changes, list):
                continue
            tagged: list[Any] = []
            for item in changes:
                if not isinstance(item, dict) or "mutation_type" in item:
                    tagged.append(item)
                    continue
                updated = dict(item)
                object_type = str(updated.get("object_type", ""))
                action = str(updated.get("action", ""))
                updated["mutation_type"] = aliases.get(
                    (object_type, action),
                    f"{object_type}_{action}",
                )
                tagged.append(updated)
            result[group] = tagged
        return result

    @field_validator("basis_refs")
    @classmethod
    def validate_basis_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values, provenance_only=True)

    @field_validator("expected_versions")
    @classmethod
    def validate_expected_versions(cls, values: dict[str, int]) -> dict[str, int]:
        for ref_id, version in values.items():
            if not is_public_ref(ref_id):
                raise ValueError("expected_versions keys must be typed public references")
            if version < 1:
                raise ValueError("expected versions must be positive")
        return values

    @model_validator(mode="after")
    def contains_a_change(self) -> ChangeSetContent:
        if not any(
            (
                self.registry_changes,
                self.preference_changes,
                self.lane_changes,
                self.event_changes,
                self.resolution_changes,
                self.provider_intents,
            )
        ):
            raise ValueError("a ChangeSet must contain at least one canonical or provider change")
        change_ids = [
            change.change_id
            for group in (
                self.registry_changes,
                self.preference_changes,
                self.lane_changes,
                self.event_changes,
                self.resolution_changes,
            )
            for change in group
        ]
        intent_ids = [intent.intent_id for intent in self.provider_intents]
        identifiers = [*change_ids, *intent_ids]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("change_id and intent_id values must be unique in a ChangeSet")
        return self


class SemanticOptionDraft(StrictModel):
    """Typed pending ChangeSet scope rendered and persisted by Docket."""

    option_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    action_kind: Literal["commit_changeset"] = "commit_changeset"
    selection_authority_ref: UtteranceRef
    content: ChangeSetContent
    explicit_exclusions: list[str] = Field(default_factory=list, max_length=25)

    @field_validator("explicit_exclusions")
    @classmethod
    def exclusions_are_bounded(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("explicit_exclusions must not contain duplicates")
        if any(not value.strip() or len(value) > 255 for value in values):
            raise ValueError("explicit exclusions must be 1..255 characters")
        return values

    @model_validator(mode="after")
    def selection_authority_slot_exists(self) -> SemanticOptionDraft:
        serialized = self.content.model_dump(mode="json")

        def contains(value: Any) -> bool:
            if value == self.selection_authority_ref:
                return True
            if isinstance(value, dict):
                return any(contains(item) for item in value.values())
            if isinstance(value, list):
                return any(contains(item) for item in value)
            return False

        if not contains(serialized):
            raise ValueError(
                "selection_authority_ref must occupy at least one provenance slot"
            )
        return self


class ChangeSetPrepare(StrictModel):
    intent_session_ref: SessionRef
    expected_session_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=512)
    content: ChangeSetContent
    semantic_request_ref: SemanticRequestRef | None = None
    authority_scope_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    precondition_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_binding: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def continuity_fields_are_complete(self) -> ChangeSetPrepare:
        values = (
            self.semantic_request_ref,
            self.authority_scope_hash,
            self.precondition_hash,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("continuity fields must be supplied together")
        return self


class ChangeSetRevise(StrictModel):
    changeset_ref: ChangeSetRef
    expected_version: int = Field(ge=1)
    content: ChangeSetContent


class ChangeSetCommit(StrictModel):
    changeset_ref: ChangeSetRef
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=512)
    authority_utterance_ref: UtteranceRef


class ConflictOpen(StrictModel):
    subject_refs: list[PublicRef] = Field(min_length=1, max_length=25)
    affected_fields: list[str] = Field(min_length=1, max_length=50)
    prior_statement_refs: list[StatementRef] = Field(min_length=1, max_length=100)
    incoming_statement_refs: list[StatementRef] = Field(min_length=1, max_length=100)
    conflicting_effects_json: dict[str, Any]

    @field_validator("subject_refs")
    @classmethod
    def validate_subject_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)


class ConflictResolve(StrictModel):
    conflict_ref: ConflictRef
    expected_version: int = Field(ge=1)
    authority_utterance_ref: UtteranceRef
    resolution: Literal[
        "resolved_supersession", "resolved_scoped_coexistence", "resolved_retraction"
    ]
    chosen_interpretation: dict[str, Any]
    statements_superseded: list[StatementRef] = Field(default_factory=list, max_length=100)
    statements_retained: list[StatementRef] = Field(default_factory=list, max_length=100)
    effective_scope: dict[str, Any]
    expected_versions: dict[PublicRef, int] = Field(default_factory=dict, max_length=100)
    canonical_effects: list[CanonicalChangeInput] = Field(default_factory=list, max_length=100)

    @field_validator("expected_versions")
    @classmethod
    def validate_expected_versions(cls, values: dict[str, int]) -> dict[str, int]:
        for ref_id, version in values.items():
            if not is_public_ref(ref_id):
                raise ValueError("expected_versions keys must be typed public references")
            if version < 1:
                raise ValueError("expected versions must be positive")
        return values
