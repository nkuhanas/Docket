from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class TriageActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: GmailActionType


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
