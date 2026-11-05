"""Small clarification, conflict and optional reported-context contracts."""

from typing import Any, Literal

from pydantic import Field

from docket.schemas.authority import (
    AgentClarificationChoice,
    AttentionCaseItemDisposition,
    AttentionCaseRef,
    ConflictRef,
    RequestRef,
    StatementRef,
)
from docket.schemas.common import StrictModel
from docket.schemas.native_commands import ChangeId, ContextId, OperationId


class RecordContext(StrictModel):
    operation_id: OperationId
    record_kind: Literal["operator_transcript", "agent_response", "source_context"]
    text: str | None = Field(default=None, max_length=65536)
    gap_code: str | None = Field(default=None, min_length=1, max_length=128)
    for_request: RequestRef | None = Field(
        default=None,
        description="Optional owned req_ from a prior receipt.",
    )


class RequestClarification(StrictModel):
    operation_id: OperationId
    question: str = Field(min_length=1, max_length=1000)
    choices: list[AgentClarificationChoice] = Field(min_length=1, max_length=4)


class ResolveConflict(StrictModel):
    context_id: ContextId
    operation_id: OperationId
    conflict_ref: ConflictRef
    resolution: Literal[
        "resolved_supersession",
        "resolved_scoped_coexistence",
        "resolved_retraction",
    ]
    chosen_interpretation: dict[str, Any]
    statements_superseded: list[StatementRef] = Field(default_factory=list, max_length=100)
    statements_retained: list[StatementRef] = Field(default_factory=list, max_length=100)
    effective_scope: dict[str, Any] = Field(default_factory=dict)
    change_id: ChangeId | None = Field(
        default=None,
        description="Optional exact owned preview whose effects resolve this conflict.",
    )


class ResolveAttentionCase(StrictModel):
    context_id: ContextId
    operation_id: OperationId
    case_ref: AttentionCaseRef
    outcome: Literal["keep_open", "resolved", "suppressed", "cancelled"]
    items: list[AttentionCaseItemDisposition] = Field(default_factory=list, max_length=25)
