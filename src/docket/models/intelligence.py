import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, TimestampMixin, utc_now


class TriageRun(Base):
    __tablename__ = "triage_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_triage_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("tri")
    )
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    claimed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))


class ContextPacket(Base):
    __tablename__ = "context_packets"
    __table_args__ = (
        UniqueConstraint(
            "triage_run_id", "source_ref", name="uq_context_packets_run_source"
        ),
        CheckConstraint(
            "serialized_bytes <= 32768", name="ck_context_packets_byte_budget"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("ctx")
    )
    triage_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_runs.id", ondelete="RESTRICT"), nullable=False
    )
    triage_run_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    trusted_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    serialized_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AttentionCase(TimestampMixin, Base):
    __tablename__ = "attention_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved', 'suppressed', 'cancelled')",
            name="ck_attention_cases_status",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_attention_cases_priority",
        ),
        UniqueConstraint("situation_key", name="uq_attention_cases_situation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("case")
    )
    situation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    semantic_classes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    entity_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    latest_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    queue_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("queue_items.id", ondelete="SET NULL")
    )
    resolution_decision_ref: Mapped[str | None] = mapped_column(String(40))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AttentionCaseRevision(Base):
    __tablename__ = "attention_case_revisions"
    __table_args__ = (
        UniqueConstraint(
            "attention_case_id", "revision", name="uq_attention_case_revision"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("caserev")
    )
    legacy_ref_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    attention_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attention_cases.id", ondelete="RESTRICT"), nullable=False
    )
    case_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_classes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    item_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CaseItem(TimestampMixin, Base):
    __tablename__ = "case_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('person_resolution', 'organization_resolution', "
            "'identity_resolution', 'affiliation_candidate', "
            "'relationship_candidate', 'fact_candidate', 'event_candidate', "
            "'lane_resolution', 'preference_match', 'decision_required')",
            name="ck_case_items_type",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved', 'rejected', 'not_pursued')",
            name="ck_case_items_status",
        ),
        CheckConstraint(
            "resolution_role IN ('required', 'supporting', 'legacy_unspecified')",
            name="ck_case_items_resolution_role",
        ),
        UniqueConstraint(
            "attention_case_id", "item_key", name="uq_case_items_case_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("item")
    )
    attention_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attention_cases.id", ondelete="CASCADE"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution_role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    candidate_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class CaseSource(Base):
    __tablename__ = "case_sources"

    attention_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attention_cases.id", ondelete="CASCADE"), primary_key=True
    )
    source_ref: Mapped[str] = mapped_column(String(40), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class TriageBriefEntry(Base):
    __tablename__ = "triage_brief_entries"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('include', 'suppress')",
            name="ck_triage_brief_entries_disposition",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("item")
    )
    triage_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    semantic_classes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    included_brief_ref: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DailyBriefCaseItem(Base):
    __tablename__ = "daily_brief_case_items"
    __table_args__ = (
        UniqueConstraint(
            "brief_id", "attention_case_id", name="uq_daily_brief_case_item"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brief_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_briefs.id", ondelete="CASCADE"), nullable=False
    )
    attention_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attention_cases.id", ondelete="RESTRICT"), nullable=False
    )
    case_revision_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    section: Mapped[str] = mapped_column(String(64), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
