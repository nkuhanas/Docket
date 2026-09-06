from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from docket.schemas.authority import (
    CanonicalChangeInput,
    OperatorChangeSetContent,
    SemanticOptionDraft,
    StatementInput,
    StatementRelationInput,
    UtteranceRef,
)
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.common import PublicRef, StrictModel, validate_refs
from docket.schemas.tracked_context import (
    ItemInput,
    TemporalRole,
    TemporalValue,
)

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
_MUTATION_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"
_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"
_HASH_PATTERN = r"^[0-9a-f]{64}$"
RequestKey = Annotated[str, Field(min_length=1, max_length=512)]


class AssemblyAuthorityScopeInput(StrictModel):
    """Stable semantic authority boundary for one incrementally assembled request."""

    resolved_intent: dict[str, Any]
    allowed_mutation_types: list[str] = Field(min_length=1, max_length=64)
    target_refs: list[PublicRef] = Field(default_factory=list, max_length=100)
    source_refs: list[Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list, max_length=25
    )
    planned_create_types: list[str] = Field(default_factory=list, max_length=32)
    explicit_exclusions: list[str] = Field(default_factory=list, max_length=25)

    @field_validator("target_refs", "source_refs")
    @classmethod
    def refs_are_unique(cls, values: list[str]) -> list[str]:
        return validate_refs(values)

    @field_validator("allowed_mutation_types")
    @classmethod
    def mutations_are_bounded(cls, values: list[str]) -> list[str]:
        normalized = sorted(set(values))
        if len(normalized) != len(values) or any(
            re.fullmatch(_MUTATION_PATTERN, value) is None for value in normalized
        ):
            raise ValueError("allowed_mutation_types must be unique canonical mutation names")
        return normalized

    @field_validator("planned_create_types")
    @classmethod
    def create_types_are_bounded(cls, values: list[str]) -> list[str]:
        normalized = sorted(set(values))
        if len(normalized) != len(values) or any(
            re.fullmatch(_TYPE_PATTERN, value) is None for value in normalized
        ):
            raise ValueError("planned_create_types must be unique canonical type names")
        return normalized

    @field_validator("explicit_exclusions")
    @classmethod
    def exclusions_are_bounded(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if len(normalized) != len(set(normalized)) or any(
            not value or len(value) > 255 for value in normalized
        ):
            raise ValueError("explicit_exclusions must be unique 1..255 character strings")
        return sorted(normalized)

    @model_validator(mode="after")
    def payload_is_bounded(self) -> AssemblyAuthorityScopeInput:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("assembly authority scope exceeds 64 KiB")
        return self


class NormalizedEntryEvidence(StrictModel):
    source_ref: Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]
    source_fragment_locator: dict[str, Any]
    source_fragment_hash: str = Field(pattern=_HASH_PATTERN)
    extractor_identifier: str = Field(min_length=1, max_length=255)
    extractor_version: str = Field(min_length=1, max_length=128)

    @field_validator("source_fragment_locator")
    @classmethod
    def locator_is_structural_and_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"body", "content", "excerpt", "quote", "raw", "text", "transcript"}

        def visit(item: Any, depth: int = 0) -> int:
            if depth > 8:
                raise ValueError("source_fragment_locator exceeds maximum nesting depth")
            if isinstance(item, dict):
                if any(str(key).casefold() in forbidden for key in item):
                    raise ValueError("source_fragment_locator contains copied source content")
                return 1 + sum(visit(nested, depth + 1) for nested in item.values())
            if isinstance(item, list):
                return 1 + sum(visit(nested, depth + 1) for nested in item)
            if isinstance(item, str) and len(item.encode("utf-8")) > 256:
                raise ValueError("source_fragment_locator coordinate is too large")
            return 1

        if visit(value) > 100:
            raise ValueError("source_fragment_locator contains too many coordinates")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 2048:
            raise ValueError("source_fragment_locator exceeds 2048 bytes")
        return value


class NormalizedTemporalFacet(StrictModel):
    role: TemporalRole
    binding_key: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    temporal_value: TemporalValue


class NoCalendarRepresentation(StrictModel):
    kind: Literal["none"] = "none"


class CanonicalEventRepresentation(StrictModel):
    kind: Literal["canonical_event"] = "canonical_event"
    lane_ref: Annotated[str, Field(pattern=r"^lane_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    lane_change_id: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    event_spec: StandaloneCalendarEventInput
    entity_refs: list[Annotated[str, Field(pattern=r"^ent_[0-9A-HJKMNP-TV-Z]{26}$")]] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def lane_is_exact(self) -> CanonicalEventRepresentation:
        if (self.lane_ref is None) == (self.lane_change_id is None):
            raise ValueError("canonical event representation requires one lane ref or change id")
        return self


CalendarRepresentation = Annotated[
    NoCalendarRepresentation | CanonicalEventRepresentation,
    Field(discriminator="kind"),
]


class NormalizedEntryBase(StrictModel):
    import_entry_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    evidence: NormalizedEntryEvidence
    item: ItemInput
    temporal: NormalizedTemporalFacet

    @model_validator(mode="after")
    def item_source_matches_evidence(self) -> NormalizedEntryBase:
        if self.item.source_refs and self.item.source_refs != [self.evidence.source_ref]:
            raise ValueError("normalized entry Item source must match its evidence source")
        return self


class TrackedTemporalEntry(NormalizedEntryBase):
    entry_type: Literal["tracked_temporal_entry"] = "tracked_temporal_entry"
    calendar: Literal[None] = None


class ScheduledOccurrenceEntry(NormalizedEntryBase):
    entry_type: Literal["scheduled_occurrence_entry"] = "scheduled_occurrence_entry"
    calendar: CanonicalEventRepresentation


class ScheduleExceptionEntry(NormalizedEntryBase):
    entry_type: Literal["schedule_exception_entry"] = "schedule_exception_entry"
    exception_disposition: Literal["no_occurrence", "closure", "cancelled_occurrence"]
    calendar: NoCalendarRepresentation = Field(default_factory=NoCalendarRepresentation)


NormalizedEntryInput = Annotated[
    TrackedTemporalEntry | ScheduledOccurrenceEntry | ScheduleExceptionEntry,
    Field(discriminator="entry_type"),
]


class StageActionUpsert(StrictModel):
    operation: Literal["action_upsert"] = "action_upsert"
    action: CanonicalChangeInput


class StageActionRemove(StrictModel):
    operation: Literal["action_remove"] = "action_remove"
    change_id: str = Field(pattern=_IDENTIFIER_PATTERN)


class StageNormalizedEntryUpsert(StrictModel):
    operation: Literal["normalized_entry_upsert"] = "normalized_entry_upsert"
    entry: NormalizedEntryInput


class StageNormalizedEntryRemove(StrictModel):
    operation: Literal["normalized_entry_remove"] = "normalized_entry_remove"
    import_entry_id: str = Field(pattern=_IDENTIFIER_PATTERN)


StagePatchOperation = Annotated[
    StageActionUpsert | StageActionRemove | StageNormalizedEntryUpsert | StageNormalizedEntryRemove,
    Field(discriminator="operation"),
]


class StagePatchInput(StrictModel):
    operations: list[StagePatchOperation] = Field(
        min_length=1,
        max_length=50,
        description=(
            "One bounded patch. At most 25 operations may be normalized_entry_upsert; "
            "split larger imports across multiple stage calls."
        ),
    )

    @model_validator(mode="after")
    def targets_are_unique(self) -> StagePatchInput:
        entry_ops = [
            operation.entry.import_entry_id
            if isinstance(operation, StageNormalizedEntryUpsert)
            else operation.import_entry_id
            for operation in self.operations
            if isinstance(operation, StageNormalizedEntryUpsert | StageNormalizedEntryRemove)
        ]
        action_ops = [
            operation.action.change_id
            if isinstance(operation, StageActionUpsert)
            else operation.change_id
            for operation in self.operations
            if isinstance(operation, StageActionUpsert | StageActionRemove)
        ]
        if len(entry_ops) != len(set(entry_ops)) or len(action_ops) != len(set(action_ops)):
            raise ValueError("one stage patch may affect each entry or action at most once")
        if (
            sum(isinstance(operation, StageNormalizedEntryUpsert) for operation in self.operations)
            > 25
        ):
            raise ValueError("one stage patch accepts at most 25 normalized entries")
        return self


class StageChangesInput(StrictModel):
    utterance_ref: UtteranceRef
    request_key: RequestKey
    assembly_scope: AssemblyAuthorityScopeInput | None = None
    expected_versions: dict[PublicRef, int] = Field(default_factory=dict, max_length=100)
    patch: StagePatchInput

    @field_validator("expected_versions")
    @classmethod
    def versions_are_positive(cls, values: dict[str, int]) -> dict[str, int]:
        if any(version < 1 for version in values.values()):
            raise ValueError("expected versions must be positive")
        return values

    @model_validator(mode="after")
    def request_is_bounded(self) -> StageChangesInput:
        encoded = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > 512 * 1024:
            raise ValueError("normalized stage request exceeds 512 KiB")
        return self


class ReviewChangesInput(StrictModel):
    utterance_ref: UtteranceRef
    request_key: RequestKey
    view: Literal["summary", "actions", "entries", "diagnostics", "diff"] = "summary"
    mutation_types: list[str] = Field(default_factory=list, max_length=64)
    normalized_entry_types: list[str] = Field(default_factory=list, max_length=3)
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=25, ge=1, le=100)


class DirectChangeSetSubmission(StrictModel):
    commit_mode: Literal["direct"] = "direct"
    statements: list[StatementInput] = Field(default_factory=list, max_length=100)
    relations: list[StatementRelationInput] = Field(default_factory=list, max_length=100)
    resolved_intent: dict[str, Any]
    blocking_clarifications: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    content: OperatorChangeSetContent | None
    intent_session_ref: Annotated[str, Field(pattern=r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    expected_session_version: int | None = Field(default=None, ge=1)
    changeset_ref: Annotated[str, Field(pattern=r"^chg_[0-9A-HJKMNP-TV-Z]{26}$")] | None = None
    expected_changeset_version: int | None = Field(default=None, ge=1)
    semantic_options: list[SemanticOptionDraft] | None = Field(default=None, max_length=10)
    semantic_request_ref: Annotated[str, Field(pattern=r"^sreq_[0-9A-HJKMNP-TV-Z]{26}$")] | None = (
        None
    )
    authority_scope_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)
    precondition_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)


class AssembledChangeSetSubmission(StrictModel):
    commit_mode: Literal["assembled"] = "assembled"


ChangeSetSubmission = Annotated[
    DirectChangeSetSubmission | AssembledChangeSetSubmission,
    Field(discriminator="commit_mode"),
]
