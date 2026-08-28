import json
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from docket.schemas.authority import PublicRef, StrictModel

TriageRunRef = Annotated[str, Field(pattern=r"^tri_[0-9A-HJKMNP-TV-Z]{26}$")]
ContextRef = Annotated[str, Field(pattern=r"^ctx_[0-9A-HJKMNP-TV-Z]{26}$")]
SourceRef = Annotated[str, Field(pattern=r"^src_[0-9A-HJKMNP-TV-Z]{26}$")]
CaseRef = Annotated[str, Field(pattern=r"^case_[0-9A-HJKMNP-TV-Z]{26}$")]
CaseRevisionRef = Annotated[
    str, Field(pattern=r"^caserev_[0-9A-HJKMNP-TV-Z]{26}$")
]

SemanticClass = Literal[
    "noise",
    "informational",
    "action_request",
    "event_invitation",
    "deadline_or_required_response",
    "relationship_context",
    "registry_candidate",
]
CaseItemType = Literal[
    "person_resolution",
    "organization_resolution",
    "identity_resolution",
    "affiliation_candidate",
    "relationship_candidate",
    "fact_candidate",
    "event_candidate",
    "lane_resolution",
    "preference_match",
    "decision_required",
]


class CaseItemInput(StrictModel):
    item_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    item_type: CaseItemType
    resolution_role: Literal["required", "supporting"]
    payload: dict[str, Any] = Field(default_factory=dict)
    candidate_refs: list[PublicRef] = Field(default_factory=list, max_length=25)

    @field_validator("candidate_refs")
    @classmethod
    def unique_candidates(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("candidate_refs must not contain duplicates")
        return values

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")) > 4096:
            raise ValueError("CaseItem payload exceeds 4 KiB serialized UTF-8")
        return value


class TriageAnalysisInput(StrictModel):
    triage_run_ref: TriageRunRef
    context_ref: ContextRef
    source_ref: SourceRef
    claim_token: str = Field(min_length=36, max_length=36)
    semantic_classes: list[SemanticClass] = Field(min_length=1, max_length=7)
    title: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=2000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    entity_candidate_refs: list[PublicRef] = Field(default_factory=list, max_length=25)
    case_items: list[CaseItemInput] = Field(default_factory=list, max_length=25)
    explanation: str = Field(min_length=1, max_length=2000)

    @field_validator("semantic_classes")
    @classmethod
    def unique_classes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("semantic_classes must not contain duplicates")
        return values

    @field_validator("entity_candidate_refs")
    @classmethod
    def unique_entities(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("entity_candidate_refs must not contain duplicates")
        return values
