from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.domain.public_refs import is_public_ref
from docket.schemas.common import ProviderAccountRef, PublicRef, StrictModel, validate_refs
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
from docket.schemas.tracked_context import (
    ItemInput,
    ItemPatchInput,
    ReminderPlanInput,
    ReminderPlanPatchInput,
    TaskInput,
    TaskPatchInput,
    TemporalBindingInput,
    TemporalBindingPatchInput,
    TemporalCalendarProjectionInput,
    TemporalCalendarProjectionPatchInput,
)

UtteranceRef = Annotated[str, Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")]
StatementRef = Annotated[str, Field(pattern=r"^stm_[0-9A-HJKMNP-TV-Z]{26}$")]
SessionRef = Annotated[str, Field(pattern=r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")]
ChangeSetRef = Annotated[str, Field(pattern=r"^chg_[0-9A-HJKMNP-TV-Z]{26}$")]
SemanticRequestRef = Annotated[str, Field(pattern=r"^sreq_[0-9A-HJKMNP-TV-Z]{26}$")]
ConflictRef = Annotated[str, Field(pattern=r"^conf_[0-9A-HJKMNP-TV-Z]{26}$")]
AttentionCaseRef = Annotated[str, Field(pattern=r"^case_[0-9A-HJKMNP-TV-Z]{26}$")]
AttentionCaseRevisionRef = Annotated[
    str, Field(pattern=r"^caserev_[0-9A-HJKMNP-TV-Z]{26}$")
]
CaseItemRef = Annotated[str, Field(pattern=r"^citem_[0-9A-HJKMNP-TV-Z]{26}$")]
SourceRef = Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]
CURRENT_IMPORT_AUTHORITY_STATEMENT = "current_import_authority_statement"
ImportAuthorityStatementRef = StatementRef | Literal[
    "current_import_authority_statement"
]

ImportEffect = Literal[
    "entity",
    "identity_handle",
    "identity_binding",
    "affiliation",
    "relationship",
    "fact",
    "interaction",
    "preference",
    "calendar_lane",
    "lane_routing_decision",
    "canonical_event",
    "item",
    "temporal_binding",
    "task",
    "temporal_calendar_projection",
    "reminder_plan",
    "attention_case_resolution",
]


def _context_import_effects() -> list[ImportEffect]:
    return ["fact", "item", "temporal_binding"]


class ImportScope(StrictModel):
    """Exact authority boundary for canonical effects derived from source evidence."""

    mode: Literal["context_only", "operator_explicit"] = "context_only"
    source_refs: list[SourceRef] = Field(min_length=1, max_length=25)
    authorized_effects: list[ImportEffect] = Field(
        default_factory=_context_import_effects,
        min_length=1,
        max_length=18,
    )
    authority_statement_refs: list[ImportAuthorityStatementRef] = Field(
        default_factory=list,
        max_length=25,
    )
    partition_key: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")

    @field_validator("source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str]) -> list[str]:
        return _validate_refs(values)

    @field_validator("authority_statement_refs")
    @classmethod
    def authority_refs_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("authority_statement_refs must not contain duplicates")
        return values

    @field_validator("authorized_effects")
    @classmethod
    def effects_are_unique_and_canonical(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("authorized_effects must not contain duplicates")
        return sorted(values)

    @model_validator(mode="after")
    def mode_has_exact_authority_shape(self) -> ImportScope:
        context_effects = ["fact", "item", "temporal_binding"]
        if self.mode == "context_only":
            if self.authorized_effects != context_effects:
                raise ValueError(
                    "context_only import scope has exactly fact, item, and "
                    "temporal_binding effects"
                )
            if self.authority_statement_refs:
                raise ValueError(
                    "context_only import scope does not accept authority statements"
                )
        elif not self.authority_statement_refs:
            raise ValueError(
                "operator_explicit import scope requires an Operator-derived authority statement"
            )
        return self


def _validate_refs(values: list[str], *, provenance_only: bool = False) -> list[str]:
    return validate_refs(values, provenance_only=provenance_only)


def _validate_structural_locator(value: Any, *, depth: int = 0) -> int:
    if depth > 8:
        raise ValueError("source_fragment_locator exceeds maximum nesting depth")
    if isinstance(value, dict):
        forbidden = {
            "body",
            "content",
            "excerpt",
            "quote",
            "raw",
            "text",
            "transcript",
        }
        if any(str(key).casefold() in forbidden for key in value):
            raise ValueError("source_fragment_locator must contain structural coordinates only")
        return 1 + sum(
            _validate_structural_locator(item, depth=depth + 1) for item in value.values()
        )
    if isinstance(value, list):
        return 1 + sum(
            _validate_structural_locator(item, depth=depth + 1) for item in value
        )
    if isinstance(value, str) and len(value.encode("utf-8")) > 256:
        raise ValueError("source_fragment_locator string coordinate is too large")
    return 1


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
    source_ref: SourceRef | None = None
    source_fragment_locator: dict[str, Any] | None = None
    source_fragment_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    extractor_identifier: str | None = Field(default=None, min_length=1, max_length=255)
    extractor_version: str | None = Field(default=None, min_length=1, max_length=128)

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
        source_fields = (
            self.source_fragment_locator,
            self.extractor_identifier,
            self.extractor_version,
        )
        if self.source_ref is None and any(value is not None for value in source_fields):
            raise ValueError("source extraction metadata requires source_ref")
        if self.source_ref is not None and any(value is None for value in source_fields):
            raise ValueError(
                "source_ref requires a fragment locator, extractor identifier, "
                "and extractor version"
            )
        if self.source_fragment_locator is not None:
            if _validate_structural_locator(self.source_fragment_locator) > 100:
                raise ValueError("source_fragment_locator contains too many coordinates")
            encoded = json.dumps(
                self.source_fragment_locator,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > 2048:
                raise ValueError("source_fragment_locator exceeds 2048 UTF-8 bytes")
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
    gateway_instance_ref: str | None = Field(
        default=None, pattern=r"^gwy_[0-9A-HJKMNP-TV-Z]{26}$"
    )

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


class ItemCreate(MutationBase):
    mutation_type: Literal["item_create"] = "item_create"
    action: Literal["create"]
    object_type: Literal["item"]
    object_ref: None = None
    create_spec: ItemInput
    payload: None = None


class ItemModify(MutationBase):
    mutation_type: Literal["item_modify"] = "item_modify"
    action: Literal["update"]
    object_type: Literal["item"]
    object_ref: Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: ItemPatchInput


class ItemRetract(MutationBase):
    mutation_type: Literal["item_retract"] = "item_retract"
    action: Literal["retract"]
    object_type: Literal["item"]
    object_ref: Annotated[str, Field(pattern=r"^item_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class TemporalBindingCreate(MutationBase):
    mutation_type: Literal["temporal_binding_create"] = "temporal_binding_create"
    action: Literal["create"]
    object_type: Literal["temporal_binding"]
    object_ref: None = None
    create_spec: TemporalBindingInput
    payload: None = None


class TemporalBindingModify(MutationBase):
    mutation_type: Literal["temporal_binding_modify"] = "temporal_binding_modify"
    action: Literal["update"]
    object_type: Literal["temporal_binding"]
    object_ref: Annotated[str, Field(pattern=r"^time_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: TemporalBindingPatchInput


class TemporalBindingSupersede(MutationBase):
    mutation_type: Literal["temporal_binding_supersede"] = "temporal_binding_supersede"
    action: Literal["supersede"]
    object_type: Literal["temporal_binding"]
    object_ref: Annotated[str, Field(pattern=r"^time_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: TemporalBindingInput
    payload: None = None


class TemporalBindingRetract(MutationBase):
    mutation_type: Literal["temporal_binding_retract"] = "temporal_binding_retract"
    action: Literal["retract"]
    object_type: Literal["temporal_binding"]
    object_ref: Annotated[str, Field(pattern=r"^time_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class TaskCreate(MutationBase):
    mutation_type: Literal["task_create"] = "task_create"
    action: Literal["create"]
    object_type: Literal["task"]
    object_ref: None = None
    create_spec: TaskInput
    payload: None = None


class TaskModify(MutationBase):
    mutation_type: Literal["task_modify"] = "task_modify"
    action: Literal["update"]
    object_type: Literal["task"]
    object_ref: Annotated[str, Field(pattern=r"^task_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: TaskPatchInput


class TaskRetract(MutationBase):
    mutation_type: Literal["task_retract"] = "task_retract"
    action: Literal["retract"]
    object_type: Literal["task"]
    object_ref: Annotated[str, Field(pattern=r"^task_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class TemporalCalendarProjectionCreate(MutationBase):
    mutation_type: Literal["temporal_calendar_projection_create"] = (
        "temporal_calendar_projection_create"
    )
    action: Literal["create"]
    object_type: Literal["temporal_calendar_projection"]
    object_ref: None = None
    create_spec: TemporalCalendarProjectionInput
    payload: None = None


class TemporalCalendarProjectionModify(MutationBase):
    mutation_type: Literal["temporal_calendar_projection_modify"] = (
        "temporal_calendar_projection_modify"
    )
    action: Literal["update"]
    object_type: Literal["temporal_calendar_projection"]
    object_ref: Annotated[str, Field(pattern=r"^tproj_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: TemporalCalendarProjectionPatchInput


class TemporalCalendarProjectionRetract(MutationBase):
    mutation_type: Literal["temporal_calendar_projection_retract"] = (
        "temporal_calendar_projection_retract"
    )
    action: Literal["retract"]
    object_type: Literal["temporal_calendar_projection"]
    object_ref: Annotated[str, Field(pattern=r"^tproj_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)


class ReminderPlanCreate(MutationBase):
    mutation_type: Literal["reminder_plan_create"] = "reminder_plan_create"
    action: Literal["create"]
    object_type: Literal["reminder_plan"]
    object_ref: None = None
    create_spec: ReminderPlanInput
    payload: None = None


class ReminderPlanModify(MutationBase):
    mutation_type: Literal["reminder_plan_modify"] = "reminder_plan_modify"
    action: Literal["update"]
    object_type: Literal["reminder_plan"]
    object_ref: Annotated[str, Field(pattern=r"^rem_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: ReminderPlanPatchInput


class ReminderPlanRetract(MutationBase):
    mutation_type: Literal["reminder_plan_retract"] = "reminder_plan_retract"
    action: Literal["retract"]
    object_type: Literal["reminder_plan"]
    object_ref: Annotated[str, Field(pattern=r"^rem_[0-9A-HJKMNP-TV-Z]{26}$")]
    create_spec: None = None
    payload: EmptyMutationSpec = Field(default_factory=EmptyMutationSpec)



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
    object_type: Literal["identity_handle"]
    object_ref: None = None
    create_spec: IdentityHandleOnlyCreateSpec
    payload: None = None


class IdentityHandleModify(MutationBase):
    mutation_type: Literal["identity_handle_modify"] = "identity_handle_modify"
    action: Literal["update", "supersede"]
    object_type: Literal["identity_handle"]
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
    object_type: Literal["identity_handle"]
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
type TrackedContextMutation = (
    ItemCreate
    | ItemModify
    | ItemRetract
    | TemporalBindingCreate
    | TemporalBindingModify
    | TemporalBindingSupersede
    | TemporalBindingRetract
    | TaskCreate
    | TaskModify
    | TaskRetract
    | TemporalCalendarProjectionCreate
    | TemporalCalendarProjectionModify
    | TemporalCalendarProjectionRetract
    | ReminderPlanCreate
    | ReminderPlanModify
    | ReminderPlanRetract
)
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
    | ItemCreate
    | ItemModify
    | ItemRetract
    | TemporalBindingCreate
    | TemporalBindingModify
    | TemporalBindingSupersede
    | TemporalBindingRetract
    | TaskCreate
    | TaskModify
    | TaskRetract
    | TemporalCalendarProjectionCreate
    | TemporalCalendarProjectionModify
    | TemporalCalendarProjectionRetract
    | ReminderPlanCreate
    | ReminderPlanModify
    | ReminderPlanRetract
)

type RegistryChangeInput = Annotated[RegistryMutation, Field(discriminator="mutation_type")]
type PreferenceChangeInput = Annotated[
    PreferenceMutation, Field(discriminator="mutation_type")
]
type LaneChangeInput = Annotated[LaneMutation, Field(discriminator="mutation_type")]
type EventChangeInput = Annotated[EventMutation, Field(discriminator="mutation_type")]
type TrackedContextChangeInput = Annotated[
    TrackedContextMutation, Field(discriminator="mutation_type")
]
type CanonicalChangeInput = Annotated[
    CanonicalMutation, Field(discriminator="mutation_type")
]


class AttentionCaseItemDisposition(StrictModel):
    case_item_ref: CaseItemRef
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
        refs = [item.case_item_ref for item in values]
        if len(refs) != len(set(refs)):
            raise ValueError("item_dispositions must not contain duplicate CaseItems")
        return values


type ResolutionChangeInput = AttentionCaseResolutionInput


type ProviderOperationType = Literal[
    "calendar_configure_lane",
    "calendar_delete_lane",
    "calendar_create_event",
    "calendar_update_event",
    "calendar_update_reminders",
    "calendar_cancel_event",
]


class ProviderIntentInput(StrictModel):
    intent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    operation_type: ProviderOperationType
    account_ref: ProviderAccountRef
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
        if not self.canonical_target_refs and not self.canonical_target_change_ids:
            raise ValueError("provider intent requires at least one canonical target")
        if len(self.canonical_target_change_ids) != len(
            set(self.canonical_target_change_ids)
        ):
            raise ValueError("canonical_target_change_ids must not contain duplicates")
        return self


class OperatorChangeSetContent(StrictModel):
    """Model-facing canonical effects; provider projection is compiler-owned."""

    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)
    import_scope: ImportScope | None = None
    expected_versions: dict[PublicRef, int] = Field(default_factory=dict, max_length=100)
    registry_changes: list[RegistryChangeInput] = Field(default_factory=list, max_length=100)
    preference_changes: list[PreferenceChangeInput] = Field(
        default_factory=list, max_length=100
    )
    lane_changes: list[LaneChangeInput] = Field(default_factory=list, max_length=100)
    event_changes: list[EventChangeInput] = Field(default_factory=list, max_length=100)
    tracked_context_changes: list[TrackedContextChangeInput] = Field(
        default_factory=list, max_length=250
    )
    resolution_changes: list[ResolutionChangeInput] = Field(
        default_factory=list, max_length=100
    )

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
    def contains_a_change(self) -> OperatorChangeSetContent:
        if not any(
            (
                self.registry_changes,
                self.preference_changes,
                self.lane_changes,
                self.event_changes,
                self.tracked_context_changes,
                self.resolution_changes,
            )
        ):
            raise ValueError("a ChangeSet must contain at least one canonical change")
        change_ids = [
            change.change_id
            for group in (
                self.registry_changes,
                self.preference_changes,
                self.lane_changes,
                self.event_changes,
                self.tracked_context_changes,
                self.resolution_changes,
            )
            for change in group
        ]
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("change_id values must be unique in a ChangeSet")
        return self

    def to_internal(self) -> ChangeSetContent:
        return ChangeSetContent.model_validate(self.model_dump(mode="json"))


class ChangeSetContent(StrictModel):
    """Internal canonical effects plus deterministic provider projection."""

    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)
    import_scope: ImportScope | None = None
    expected_versions: dict[PublicRef, int] = Field(default_factory=dict, max_length=100)
    registry_changes: list[RegistryChangeInput] = Field(default_factory=list, max_length=100)
    preference_changes: list[PreferenceChangeInput] = Field(
        default_factory=list, max_length=100
    )
    lane_changes: list[LaneChangeInput] = Field(default_factory=list, max_length=100)
    event_changes: list[EventChangeInput] = Field(default_factory=list, max_length=100)
    tracked_context_changes: list[TrackedContextChangeInput] = Field(
        default_factory=list, max_length=250
    )
    resolution_changes: list[ResolutionChangeInput] = Field(
        default_factory=list, max_length=100
    )
    provider_intents: list[ProviderIntentInput] = Field(default_factory=list, max_length=100)

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
                self.tracked_context_changes,
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
                self.tracked_context_changes,
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
    content: OperatorChangeSetContent
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
    semantic_request_ref: SemanticRequestRef | None = None
    authority_scope_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    precondition_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_binding: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def continuity_fields_are_complete(self) -> ChangeSetRevise:
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
