from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.queue import QueuePriority

TriageDecision = Literal["actionable", "ignore"]
TriageCategory = Literal[
    "application_update",
    "scholarship",
    "invitation",
    "deadline",
    "calendar_change",
    "required_response",
    "account_alert",
    "school_notice",
    "financial_notice",
    "general_action",
]
GmailActionType = Literal["gmail_archive_message", "gmail_mark_read"]
SemanticCandidateKind = Literal[
    "event",
    "deadline",
    "response",
    "task",
    "information",
    "noise",
]
SemanticMutation = Literal["create", "update", "cancel", "none"]
EntityClass = Literal[
    "institution",
    "organization",
    "course",
    "person",
    "location",
    "project",
    "service",
]


class TriageActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: GmailActionType


class ProposeClassifiedGmailActionInput(BaseModel):
    """Operator-authorized proposal against one exact classified source version."""

    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$",
    )
    source_id: UUID
    expected_source_version: str = Field(min_length=1, max_length=255)
    action_type: GmailActionType
    actor_id: str = Field(min_length=1, max_length=255)


class EntityMentionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_class: EntityClass
    name: str = Field(min_length=1, max_length=512)
    role: str | None = Field(default=None, min_length=1, max_length=128)


class CandidateCorrelationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_event_id: str | None = Field(default=None, min_length=1, max_length=1024)
    title_hint: str | None = Field(default=None, min_length=1, max_length=512)
    date_hint: str | None = Field(default=None, min_length=1, max_length=64)
    sender_event_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def at_least_one_hint(self) -> "CandidateCorrelationInput":
        if not any((self.provider_event_id, self.title_hint, self.date_hint, self.sender_event_id)):
            raise ValueError("correlation requires at least one stable hint")
        return self


class SemanticCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    kind: SemanticCandidateKind
    mutation: SemanticMutation = "none"
    title: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=2000)
    event: StandaloneCalendarEventInput | None = None
    correlation: CandidateCorrelationInput | None = None
    entity_mentions: list[EntityMentionInput] = Field(default_factory=list, max_length=20)
    context_labels: list[str] = Field(default_factory=list, max_length=20)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_candidate_shape(self) -> "SemanticCandidateInput":
        if self.kind in {"information", "noise"} and self.mutation != "none":
            raise ValueError("information and noise cannot request a mutation")
        if self.mutation in {"update", "cancel"} and self.correlation is None:
            raise ValueError("update and cancel candidates require correlation hints")
        if (
            self.kind == "event"
            and self.mutation == "create"
            and self.event is None
            and not self.missing_fields
        ):
            raise ValueError("an event create needs a complete event or explicit missing fields")
        if self.event is not None and self.kind != "event":
            raise ValueError("structured event details are valid only for event candidates")
        return self


class SubmitSemanticCandidatesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    claim_token: str
    candidates: list[SemanticCandidateInput] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def candidate_keys_are_unique(self) -> "SubmitSemanticCandidatesInput":
        keys = [candidate.candidate_key for candidate in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate_key values must be unique within one source")
        return self


class SubmitTriageDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    claim_token: str
    decision: TriageDecision
    category: TriageCategory | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    priority: QueuePriority | None = None
    semantic_event_type: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{0,127}$",
    )
    action_proposals: list[TriageActionProposal] = Field(
        default_factory=list,
        max_length=5,
    )

    @model_validator(mode="after")
    def validate_decision_shape(self) -> "SubmitTriageDecisionInput":
        derived = (
            self.category,
            self.title,
            self.summary,
            self.priority,
            self.semantic_event_type,
        )
        if self.decision == "actionable" and any(value is None for value in derived):
            raise ValueError(
                "Actionable triage requires category, title, summary, priority, "
                "and semantic_event_type"
            )
        if self.decision == "ignore" and self.action_proposals:
            raise ValueError("Ignored sources cannot propose actions")
        action_types = [proposal.action_type for proposal in self.action_proposals]
        if len(action_types) != len(set(action_types)):
            raise ValueError("Each Gmail action type may be proposed at most once")
        return self
