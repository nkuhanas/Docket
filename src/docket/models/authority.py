from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class IntentSession(Base):
    __tablename__ = "intent_sessions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'needs_clarification', 'ready', 'committed', "
            "'cancelled', 'superseded')",
            name="ck_intent_sessions_state",
        ),
        Index("ix_intent_sessions_conversation_state", "conversation_ref", "state"),
        Index("ix_intent_sessions_source_utterance", "source_utterance_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("ses")
    )
    conversation_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_utterance_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    case_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    case_revision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    brief_ref: Mapped[str | None] = mapped_column(String(40))
    trusted_context_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    resolved_intent_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    blocking_clarifications: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    committed_changeset_ref: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class IntentTurn(Base):
    __tablename__ = "intent_turns"
    __table_args__ = (
        CheckConstraint(
            "response_disposition IN ('pending', 'final_response', 'no_response')",
            name="ck_intent_turns_response_disposition",
        ),
        UniqueConstraint(
            "intent_session_id", "utterance_ref", name="uq_intent_turns_session_utterance"
        ),
        Index("ix_intent_turns_session_created", "intent_session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("turn")
    )
    intent_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intent_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    intent_session_ref: Mapped[str] = mapped_column(
        ForeignKey("intent_sessions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    utterance_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    statement_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tool_call_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    agent_response_ref: Mapped[str | None] = mapped_column(String(40))
    resulting_semantic_refs: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    response_disposition: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ChangeSet(Base):
    __tablename__ = "change_sets"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draft', 'validated', 'committed', 'invalidated', "
            "'cancelled', 'superseded')",
            name="ck_change_sets_state",
        ),
        Index("ix_change_sets_session_state", "intent_session_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("chg")
    )
    intent_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intent_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    intent_session_ref: Mapped[str] = mapped_column(
        ForeignKey("intent_sessions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_versions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    registry_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    preference_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    lane_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    event_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    resolution_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    provider_intents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangeSetRevision(Base):
    __tablename__ = "change_set_revisions"
    __table_args__ = (
        UniqueConstraint(
            "change_set_id", "revision", name="uq_change_set_revisions_number"
        ),
        Index("ix_change_set_revisions_changeset", "change_set_id", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_versions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    registry_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    preference_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    lane_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    event_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    resolution_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    provider_intents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Conflict(Base):
    __tablename__ = "conflicts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved_supersession', "
            "'resolved_scoped_coexistence', 'resolved_retraction', 'cancelled')",
            name="ck_conflicts_status",
        ),
        Index("ix_conflicts_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("cnf")
    )
    subject_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    affected_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    prior_statement_refs: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    incoming_statement_refs: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    conflicting_effects_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(String(48), default="open", nullable=False)
    resolution_decision_ref: Mapped[str | None] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _reject_immutable(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} is immutable")


for _immutable_model in (ChangeSetRevision,):
    event.listen(_immutable_model, "before_update", _reject_immutable)
    event.listen(_immutable_model, "before_delete", _reject_immutable)


def _guard_committed_changeset(
    _mapper: object,
    _connection: object,
    target: ChangeSet,
) -> None:
    state = inspect(target)
    previous = state.attrs.state.history.deleted
    committing_now = bool(previous and previous[0] != "committed")
    if target.state == "committed" and not committing_now:
        raise ValueError("Committed ChangeSet is immutable")


event.listen(ChangeSet, "before_update", _guard_committed_changeset)


def _guard_changeset_delete(
    _mapper: object,
    _connection: object,
    target: ChangeSet,
) -> None:
    if target.state == "committed":
        raise ValueError("Committed ChangeSet is immutable")


event.listen(ChangeSet, "before_delete", _guard_changeset_delete)


def _guard_intent_turn_enrichment(
    _mapper: object,
    _connection: object,
    target: IntentTurn,
) -> None:
    state = inspect(target)
    mutable_fields = {
        "tool_call_refs",
        "agent_response_ref",
        "resulting_semantic_refs",
        "response_disposition",
    }
    changed = {
        attribute.key for attribute in state.attrs if attribute.history.has_changes()
    }
    if changed - mutable_fields:
        raise ValueError("IntentTurn semantic source fields are immutable")
    prior_disposition = state.attrs.response_disposition.history.deleted
    if prior_disposition and prior_disposition[0] != "pending":
        raise ValueError("Finalized IntentTurn is immutable")


event.listen(IntentTurn, "before_update", _guard_intent_turn_enrichment)
event.listen(IntentTurn, "before_delete", _reject_immutable)


def _guard_conflict_semantics(
    _mapper: object,
    _connection: object,
    target: Conflict,
) -> None:
    state = inspect(target)
    mutable_fields = {"status", "resolution_decision_ref", "version", "resolved_at"}
    changed = {
        attribute.key for attribute in state.attrs if attribute.history.has_changes()
    }
    semantic_changes = changed - mutable_fields
    if semantic_changes:
        raise ValueError(
            "Conflict semantic fields are immutable: " + ", ".join(sorted(semantic_changes))
        )


event.listen(Conflict, "before_update", _guard_conflict_semantics)
