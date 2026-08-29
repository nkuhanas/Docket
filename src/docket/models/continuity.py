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
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class PersistedSemanticOption(Base):
    __tablename__ = "persisted_semantic_options"
    __table_args__ = (
        UniqueConstraint(
            "prompt_projection_ref",
            "prompt_projection_version",
            "option_id",
            name="uq_persisted_semantic_options_binding",
        ),
        UniqueConstraint(
            "prompt_projection_ref",
            "prompt_projection_version",
            "option_id",
            "authority_scope_hash",
            "precondition_hash",
            name="uq_persisted_semantic_options_exact_binding",
        ),
        Index(
            "ix_persisted_semantic_options_session",
            "intent_session_ref",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    prompt_projection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("semantic_prompt_projections.id", ondelete="RESTRICT"), nullable=False
    )
    prompt_projection_ref: Mapped[str] = mapped_column(
        ForeignKey("semantic_prompt_projections.ref_id", ondelete="RESTRICT"), nullable=False
    )
    prompt_projection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    option_id: Mapped[str] = mapped_column(String(128), nullable=False)
    visible_text: Mapped[str] = mapped_column(Text, nullable=False)
    action_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    authority_scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    execution_preconditions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    compilation_template_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    case_ref: Mapped[str | None] = mapped_column(String(40))
    case_revision_ref: Mapped[str | None] = mapped_column(String(40))
    intent_session_ref: Mapped[str] = mapped_column(
        ForeignKey("intent_sessions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    authority_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    precondition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SemanticPromptProjection(Base):
    __tablename__ = "semantic_prompt_projections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'selected', 'superseded')",
            name="ck_semantic_prompt_projections_status",
        ),
        UniqueConstraint(
            "intent_session_ref",
            "projection_version",
            name="uq_semantic_prompt_projections_session_version",
        ),
        UniqueConstraint("message_id", name="uq_semantic_prompt_projections_message"),
        Index(
            "ix_semantic_prompt_projections_status_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("proj")
    )
    intent_session_ref: Mapped[str] = mapped_column(
        ForeignKey("intent_sessions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    guild_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_channel_id: Mapped[str | None] = mapped_column(String(64))
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    case_ref: Mapped[str | None] = mapped_column(String(40))
    case_revision_ref: Mapped[str | None] = mapped_column(String(40))
    render_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    component_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GatewayLifetime(Base):
    __tablename__ = "gateway_lifetimes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'draining', 'clean_shutdown', 'expired')",
            name="ck_gateway_lifetimes_status",
        ),
        UniqueConstraint(
            "instance_kind",
            "lease_generation",
            name="uq_gateway_lifetimes_generation",
        ),
        UniqueConstraint("registration_key", name="uq_gateway_lifetimes_registration"),
        Index("ix_gateway_lifetimes_status_expiry", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("gwy")
    )
    registration_key: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    instance_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clean_shutdown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)


class DrainBarrier(Base):
    __tablename__ = "drain_barriers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'draining', 'released', 'aborted')",
            name="ck_drain_barriers_status",
        ),
        Index("ix_drain_barriers_status_requested", "status", "requested_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("drain")
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeout_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="requested", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class ExecutionLease(Base):
    __tablename__ = "execution_leases"
    __table_args__ = (
        CheckConstraint(
            "lease_kind IN ('interactive_turn', 'triage_turn', 'tool_invocation', "
            "'provider_call', 'outbox_delivery', 'cron_execution')",
            name="ck_execution_leases_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'completed', 'expired', 'cancelled')",
            name="ck_execution_leases_status",
        ),
        UniqueConstraint("lease_key", name="uq_execution_leases_key"),
        Index("ix_execution_leases_status_expiry", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("lease")
    )
    lease_key: Mapped[str] = mapped_column(String(512), nullable=False)
    lease_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ref: Mapped[str | None] = mapped_column(String(40))
    gateway_instance_ref: Mapped[str | None] = mapped_column(String(40))
    claimed_before_drain_ref: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DeferredIngress(Base):
    __tablename__ = "deferred_ingress"
    __table_args__ = (
        CheckConstraint(
            "ingress_kind IN ('typed_message', 'button_selection', "
            "'select_selection', 'modal_submission')",
            name="ck_deferred_ingress_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'completed', 'rejected')",
            name="ck_deferred_ingress_status",
        ),
        UniqueConstraint("source_key", name="uq_deferred_ingress_source_key"),
        UniqueConstraint("utterance_ref", name="uq_deferred_ingress_utterance"),
        Index("ix_deferred_ingress_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("ing")
    )
    source_key: Mapped[str] = mapped_column(String(512), nullable=False)
    ingress_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    utterance_ref: Mapped[str] = mapped_column(
        ForeignKey("operator_utterances.ref_id", ondelete="RESTRICT"), nullable=False
    )
    selected_option_binding_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    drain_ref: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    claimed_by_gateway_ref: Mapped[str | None] = mapped_column(String(40))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


def _reject_immutable(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} is immutable")


event.listen(PersistedSemanticOption, "before_update", _reject_immutable)
event.listen(PersistedSemanticOption, "before_delete", _reject_immutable)
