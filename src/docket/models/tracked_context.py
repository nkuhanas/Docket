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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now

_CANONICAL_STATUS = "canonical_status IN ('active', 'historical', 'retracted')"


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint(_CANONICAL_STATUS, name="ck_items_canonical_status"),
        CheckConstraint("version >= 1", name="ck_items_version"),
        Index("ix_items_kind_status", "kind", "canonical_status"),
        Index("ix_items_parent", "parent_item_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("item")
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    context_entity_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    parent_item_ref: Mapped[str | None] = mapped_column(
        ForeignKey("items.ref_id", ondelete="RESTRICT")
    )
    canonical_status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ItemSourceBinding(Base):
    __tablename__ = "item_source_bindings"
    __table_args__ = (
        UniqueConstraint(
            "source_ref",
            "source_revision_key",
            "locator_hash",
            "semantic_role",
            name="uq_item_source_bindings_fragment_role",
        ),
        Index("ix_item_source_bindings_item", "item_ref", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_ref: Mapped[str] = mapped_column(
        ForeignKey("items.ref_id", ondelete="RESTRICT"), nullable=False
    )
    source_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    source_revision_key: Mapped[str] = mapped_column(String(256), nullable=False)
    source_fragment_locator: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    locator_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_role: Mapped[str] = mapped_column(String(128), nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class TemporalBinding(Base):
    __tablename__ = "temporal_bindings"
    __table_args__ = (
        CheckConstraint(
            "role IN ('scheduled_on', 'occurs_at', 'due_by', 'opens_at', "
            "'closes_at', 'available_from', 'available_until', 'expected_at', "
            "'effective_from', 'effective_until', 'window')",
            name="ck_temporal_bindings_role",
        ),
        CheckConstraint(_CANONICAL_STATUS, name="ck_temporal_bindings_canonical_status"),
        CheckConstraint(
            "subject_ref LIKE 'item\\_%' ESCAPE '\\' OR "
            "subject_ref LIKE 'task\\_%' ESCAPE '\\'",
            name="ck_temporal_bindings_subject_ref",
        ),
        CheckConstraint("version >= 1", name="ck_temporal_bindings_version"),
        UniqueConstraint(
            "subject_ref",
            "role",
            "binding_key",
            "version",
            name="uq_temporal_bindings_subject_role_key_version",
        ),
        Index(
            "ix_temporal_bindings_subject_effective",
            "subject_ref",
            "role",
            "binding_key",
            "canonical_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("time")
    )
    subject_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_key: Mapped[str] = mapped_column(String(128), default="default", nullable=False)
    temporal_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    supersedes_ref: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "task_state IN ('not_started', 'in_progress', 'blocked', 'completed', "
            "'cancelled')",
            name="ck_tasks_state",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_tasks_priority",
        ),
        CheckConstraint(_CANONICAL_STATUS, name="ck_tasks_canonical_status"),
        CheckConstraint(
            "(task_state = 'completed' AND completed_at IS NOT NULL) OR "
            "(task_state <> 'completed' AND completed_at IS NULL)",
            name="ck_tasks_completed_at",
        ),
        CheckConstraint("version >= 1", name="ck_tasks_version"),
        Index("ix_tasks_item_state", "item_ref", "task_state", "canonical_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("task")
    )
    item_ref: Mapped[str] = mapped_column(
        ForeignKey("items.ref_id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_state: Mapped[str] = mapped_column(String(32), default="not_started", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    canonical_status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class EventItemLink(Base):
    __tablename__ = "event_item_links"
    __table_args__ = (
        UniqueConstraint("event_ref", "item_ref", name="uq_event_item_links_pair"),
        Index("ix_event_item_links_item", "item_ref", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    item_ref: Mapped[str] = mapped_column(
        ForeignKey("items.ref_id", ondelete="RESTRICT"), nullable=False
    )
    realizes_temporal_binding_ref: Mapped[str | None] = mapped_column(String(40))
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class TemporalCalendarProjection(Base):
    __tablename__ = "temporal_calendar_projections"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_temporal_calendar_projections_version"),
        Index(
            "uq_temporal_calendar_projections_active_binding",
            "temporal_binding_ref",
            unique=True,
            postgresql_where=text("enabled"),
            sqlite_where=text("enabled = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("tproj")
    )
    temporal_binding_ref: Mapped[str] = mapped_column(
        ForeignKey("temporal_bindings.ref_id", ondelete="RESTRICT"), nullable=False
    )
    lane_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    display_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reminder_plan_ref: Mapped[str | None] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ReminderPlan(Base):
    __tablename__ = "reminder_plans"
    __table_args__ = (
        CheckConstraint(_CANONICAL_STATUS, name="ck_reminder_plans_canonical_status"),
        CheckConstraint(
            "subject_ref LIKE 'evt\\_%' ESCAPE '\\' OR "
            "subject_ref LIKE 'time\\_%' ESCAPE '\\'",
            name="ck_reminder_plans_subject_ref",
        ),
        CheckConstraint("version >= 1", name="ck_reminder_plans_version"),
        Index("ix_reminder_plans_subject_status", "subject_ref", "canonical_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("rem")
    )
    subject_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    delivery_channels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    lead_seconds: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    date_trigger_local_time: Mapped[str | None] = mapped_column(String(8))
    timezone: Mapped[str | None] = mapped_column(String(128))
    canonical_status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AttachmentEvidence(Base):
    __tablename__ = "attachment_evidence_metadata"
    __table_args__ = (
        CheckConstraint(
            "ingest_state IN ('pending', 'available', 'failed', 'rejected')",
            name="ck_attachment_evidence_ingest_state",
        ),
        CheckConstraint(
            "retention_disposition IN ('pending', 'retained_encrypted', 'derived_only', "
            "'metadata_only', 'rejected')",
            name="ck_attachment_evidence_retention",
        ),
        CheckConstraint(
            "(ingest_state = 'pending' AND retention_disposition = 'pending' "
            "AND content_hash IS NULL) OR "
            "(ingest_state = 'available' AND retention_disposition IN "
            "('retained_encrypted', 'derived_only') AND content_hash IS NOT NULL) OR "
            "(ingest_state = 'failed' AND retention_disposition = 'metadata_only' "
            "AND content_hash IS NULL) OR "
            "(ingest_state = 'rejected' AND retention_disposition = 'rejected' "
            "AND content_hash IS NULL)",
            name="ck_attachment_evidence_state_retention_hash",
        ),
        UniqueConstraint(
            "transport",
            "transport_attachment_ref",
            "source_message_ref",
            name="uq_attachment_evidence_transport_binding",
        ),
        Index("ix_attachment_evidence_utterance", "operator_utterance_ref", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        ForeignKey("sources.ref_id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    transport_attachment_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_message_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    operator_utterance_ref: Mapped[str] = mapped_column(
        ForeignKey("operator_utterances.ref_id", ondelete="RESTRICT"), nullable=False
    )
    filename: Mapped[str | None] = mapped_column(String(512))
    media_type: Mapped[str | None] = mapped_column(String(255))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    ingest_state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    retention_disposition: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    derived_content_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class EncryptedAttachmentBlob(Base):
    __tablename__ = "encrypted_attachment_blobs"
    __table_args__ = (
        UniqueConstraint("attachment_source_ref", name="uq_encrypted_attachment_blob_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    attachment_source_ref: Mapped[str] = mapped_column(
        ForeignKey("attachment_evidence_metadata.ref_id", ondelete="RESTRICT"), nullable=False
    )
    encryption_key_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


def _guard_attachment_enrichment(
    _mapper: object, _connection: object, target: AttachmentEvidence
) -> None:
    state = inspect(target)
    immutable_fields = (
        "ref_id",
        "transport",
        "transport_attachment_ref",
        "source_message_ref",
        "operator_utterance_ref",
        "filename",
        "media_type",
        "byte_size",
        "received_at",
        "recorded_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable_fields):
        raise ValueError("AttachmentEvidence identity and manifest fields are immutable")
    prior_state = state.attrs.ingest_state.history.deleted
    if prior_state and prior_state[0] != "pending":
        raise ValueError("terminal AttachmentEvidence ingest state is immutable")
    prior_hash = state.attrs.content_hash.history.deleted
    if prior_hash and prior_hash[0] is not None:
        raise ValueError("AttachmentEvidence content hash is immutable once available")
    valid_outcome = (
        (
            target.ingest_state == "pending"
            and target.retention_disposition == "pending"
            and target.content_hash is None
        )
        or (
            target.ingest_state == "available"
            and target.retention_disposition in {"retained_encrypted", "derived_only"}
            and target.content_hash is not None
        )
        or (
            target.ingest_state == "failed"
            and target.retention_disposition == "metadata_only"
            and target.content_hash is None
        )
        or (
            target.ingest_state == "rejected"
            and target.retention_disposition == "rejected"
            and target.content_hash is None
        )
    )
    if not valid_outcome:
        raise ValueError("AttachmentEvidence state, retention, and hash are inconsistent")


def _reject_attachment_delete(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} is append-only")


event.listen(AttachmentEvidence, "before_update", _guard_attachment_enrichment)
event.listen(AttachmentEvidence, "before_delete", _reject_attachment_delete)
event.listen(EncryptedAttachmentBlob, "before_update", _reject_attachment_delete)
event.listen(EncryptedAttachmentBlob, "before_delete", _reject_attachment_delete)
event.listen(ItemSourceBinding, "before_update", _reject_attachment_delete)
event.listen(ItemSourceBinding, "before_delete", _reject_attachment_delete)
