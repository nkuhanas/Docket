from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from docket.domain.public_refs import is_public_ref, parse_public_ref

PublicRef = Annotated[str, Field(min_length=30, max_length=40)]
UtteranceRef = Annotated[str, Field(pattern=r"^utt_[0-9A-HJKMNP-TV-Z]{26}$")]
StatementRef = Annotated[str, Field(pattern=r"^stm_[0-9A-HJKMNP-TV-Z]{26}$")]
SessionRef = Annotated[str, Field(pattern=r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")]
ChangeSetRef = Annotated[str, Field(pattern=r"^chg_[0-9A-HJKMNP-TV-Z]{26}$")]
ConflictRef = Annotated[str, Field(pattern=r"^cnf_[0-9A-HJKMNP-TV-Z]{26}$")]

_PROVENANCE_PREFIXES = frozenset(
    {
        "utt",
        "src",
        "stm",
        "dec",
        "cnf",
        "chg",
        "ent",
        "idn",
        "aff",
        "rel",
        "fact",
        "int",
        "pref",
        "lane",
        "route",
        "evt",
        "rsp",
        "tri",
        "case",
        "item",
        "brief",
        "ctx",
        "ses",
        "turn",
        "call",
        "aud",
        "op",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_refs(values: list[str], *, provenance_only: bool = False) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("public reference lists must not contain duplicates")
    for value in values:
        if not is_public_ref(value):
            raise ValueError("value must be a typed Docket public reference")
        prefix, _payload = parse_public_ref(value)
        if provenance_only and prefix not in _PROVENANCE_PREFIXES:
            raise ValueError("value is not an allowed ProvenanceRef")
    return values


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


class CanonicalChangeInput(StrictModel):
    change_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    action: Literal["create", "update", "supersede", "retract", "bind", "unbind"]
    object_type: Literal[
        "entity",
        "identity_binding",
        "affiliation",
        "relationship",
        "fact",
        "interaction",
        "preference",
        "calendar_lane",
        "lane_routing_decision",
        "canonical_event",
        "conflict_resolution",
        "attention_case_resolution",
    ]
    object_ref: PublicRef | None = None
    create_spec: dict[str, Any] | None = None
    affected_fields: list[str] = Field(min_length=1, max_length=50)
    basis_refs: list[PublicRef] = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("basis_refs")
    @classmethod
    def validate_basis_refs(cls, values: list[str]) -> list[str]:
        return _validate_refs(values, provenance_only=True)

    @model_validator(mode="after")
    def target_is_exact(self) -> CanonicalChangeInput:
        if self.action == "create":
            if self.create_spec is None or self.object_ref is not None:
                raise ValueError("create requires create_spec and no object_ref")
        elif self.object_ref is None or self.create_spec is not None:
            raise ValueError("non-create change requires object_ref and no create_spec")
        return self


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
    registry_changes: list[CanonicalChangeInput] = Field(default_factory=list, max_length=100)
    preference_changes: list[CanonicalChangeInput] = Field(default_factory=list, max_length=100)
    lane_changes: list[CanonicalChangeInput] = Field(default_factory=list, max_length=100)
    event_changes: list[CanonicalChangeInput] = Field(default_factory=list, max_length=100)
    resolution_changes: list[CanonicalChangeInput] = Field(default_factory=list, max_length=100)
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


class ChangeSetPrepare(StrictModel):
    intent_session_ref: SessionRef
    expected_session_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=512)
    content: ChangeSetContent


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
    canonical_effects: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
