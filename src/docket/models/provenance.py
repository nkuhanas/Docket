from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class OperatorUtterance(Base):
    __tablename__ = "operator_utterances"
    __table_args__ = (
        CheckConstraint("transport IN ('discord')", name="ck_operator_utterances_transport"),
        UniqueConstraint(
            "transport",
            "source_message_ref",
            name="uq_operator_utterances_transport_message",
        ),
        Index("ix_operator_utterances_conversation_said", "conversation_ref", "said_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("utt")
    )
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    conversation_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    reply_to_source_ref: Mapped[str | None] = mapped_column(String(512))
    said_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    verbatim_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("records.id", ondelete="SET NULL")
    )


class AgentResponse(Base):
    __tablename__ = "agent_responses"
    __table_args__ = (
        CheckConstraint("generation_state = 'complete'", name="ck_agent_responses_generation"),
        CheckConstraint(
            "delivery_state IN ('pending', 'delivered', 'failed')",
            name="ck_agent_responses_delivery",
        ),
        Index("ix_agent_responses_conversation_generated", "conversation_ref", "generated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("rsp")
    )
    response_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    conversation_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    intent_session_ref: Mapped[str | None] = mapped_column(String(40))
    responds_to_utterance_refs: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    verbatim_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    context_packet_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tool_call_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    generation_state: Mapped[str] = mapped_column(String(16), default="complete", nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    projection_ref: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_error_code: Mapped[str | None] = mapped_column(String(128))


class AgentResponseProjection(Base):
    __tablename__ = "agent_response_projections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_agent_response_projections_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    response_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_responses.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    projection_ref: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_public_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    projection_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    case_revision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    brief_ref: Mapped[str | None] = mapped_column(String(40))
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    destination_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_message_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class InterpretedStatement(Base):
    __tablename__ = "interpreted_statements"
    __table_args__ = (
        Index("ix_interpreted_statements_utterance", "utterance_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("stm")
    )
    utterance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operator_utterances.id", ondelete="RESTRICT"), nullable=False
    )
    statement_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    affected_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    interpretation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    interpreter_version: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class StatementRelation(Base):
    __tablename__ = "statement_relations"
    __table_args__ = (
        CheckConstraint(
            "relation_kind IN ('affirms', 'amends', 'supersedes', 'contradicts', "
            "'retracts', 'scopes')",
            name="ck_statement_relations_kind",
        ),
        UniqueConstraint(
            "source_statement_id",
            "target_statement_id",
            "relation_kind",
            name="uq_statement_relations_edge",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interpreted_statements.id", ondelete="RESTRICT"), nullable=False
    )
    target_statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interpreted_statements.id", ondelete="RESTRICT"), nullable=False
    )
    relation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_document_kind", "document_ref", "decision_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("dec")
    )
    decision_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_ref: Mapped[str | None] = mapped_column(String(255))
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    document_ref: Mapped[str | None] = mapped_column(String(255))
    frozen_artifact_hash: Mapped[str | None] = mapped_column(String(64))
    authorized_scope: Mapped[str | None] = mapped_column(String(128))
    architecture_authority: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    implementation_authority: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        CheckConstraint(
            "caller_profile IN ('interactive', 'triage')",
            name="ck_tool_invocations_profile",
        ),
        CheckConstraint(
            "status IN ('received', 'rejected_validation', 'rejected_authority', "
            "'rejected_conflict', 'succeeded', 'failed')",
            name="ck_tool_invocations_status",
        ),
        UniqueConstraint("trace_id", "trace_call_id", name="uq_tool_invocations_trace_call"),
        Index("ix_tool_invocations_name_started", "tool_name", "started_at"),
        Index("ix_tool_invocations_trace", "trace_id", "trace_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("call")
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_contract_hash: Mapped[str] = mapped_column(
        String(64), default="0" * 64, nullable=False
    )
    caller_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_ref: Mapped[str | None] = mapped_column(String(255))
    utterance_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    intent_session_ref: Mapped[str | None] = mapped_column(String(40))
    case_ref: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    received_argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_argument_hash: Mapped[str | None] = mapped_column(String(64))
    result_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    result_disposition: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    mcp_request_id: Mapped[str | None] = mapped_column(String(255))
    trace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    trace_call_id: Mapped[str | None] = mapped_column(String(255))
    trace_ordinal: Mapped[int | None] = mapped_column(Integer)


class RuntimeLogEntry(Base):
    __tablename__ = "runtime_log_entries"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('debug', 'info', 'warning', 'error', 'critical')",
            name="ck_runtime_log_entries_severity",
        ),
        Index(
            "ix_runtime_log_entries_component_occurred",
            "component",
            "occurred_at",
        ),
        Index("ix_runtime_log_entries_event_occurred", "event_code", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("log")
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    event_code: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    related_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


def _reject_immutable(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} is immutable")


for _immutable_model in (
    OperatorUtterance,
    InterpretedStatement,
    StatementRelation,
    Decision,
    RuntimeLogEntry,
):
    event.listen(_immutable_model, "before_update", _reject_immutable)
    event.listen(_immutable_model, "before_delete", _reject_immutable)


def _guard_agent_response_semantics(
    _mapper: object,
    _connection: object,
    target: AgentResponse,
) -> None:
    state = inspect(target)
    mutable_fields = {"delivered_at", "delivery_error_code", "delivery_state"}
    changed = {
        attribute.key
        for attribute in state.attrs
        if attribute.history.has_changes()
    }
    semantic_changes = changed - mutable_fields
    if semantic_changes:
        raise ValueError(
            "AgentResponse semantic fields are immutable: "
            + ", ".join(sorted(semantic_changes))
        )


event.listen(AgentResponse, "before_update", _guard_agent_response_semantics)
