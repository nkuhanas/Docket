import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, TimestampMixin


class CanonicalEvent(TimestampMixin, Base):
    __tablename__ = "canonical_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'active', 'cancelled', 'archived')",
            name="ck_canonical_events_status",
        ),
        CheckConstraint(
            "authority IN ('explicit_user', 'canonical', 'inferred')",
            name="ck_canonical_events_authority",
        ),
        CheckConstraint(
            "calendar_lane IN ('academic', 'work', 'organizations', 'personal', 'unsorted')",
            name="ck_canonical_events_calendar_lane",
        ),
        UniqueConstraint("canonical_key", name="uq_canonical_events_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    event_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reminder_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    calendar_lane: Mapped[str] = mapped_column(String(32), default="unsorted", nullable=False)
    entity_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    context_labels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class EventObservation(TimestampMixin, Base):
    __tablename__ = "event_observations"
    __table_args__ = (
        CheckConstraint(
            "mutation IN ('create', 'update', 'cancel', 'none')",
            name="ck_event_observations_mutation",
        ),
        CheckConstraint(
            "correlation_state IN ('new', 'matched', 'ambiguous', 'unresolved')",
            name="ck_event_observations_correlation",
        ),
        UniqueConstraint("semantic_candidate_id", name="uq_event_observations_semantic_candidate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_events.id", ondelete="SET NULL")
    )
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_items.id", ondelete="SET NULL")
    )
    semantic_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("semantic_candidates.id", ondelete="SET NULL")
    )
    mutation: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    correlation_state: Mapped[str] = mapped_column(String(16), nullable=False)
    candidate_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderEventBinding(TimestampMixin, Base):
    __tablename__ = "provider_event_bindings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'cancelled', 'diverged')",
            name="ck_provider_event_bindings_status",
        ),
        UniqueConstraint(
            "account_id",
            "calendar_id",
            "provider_event_id",
            name="uq_provider_event_bindings_target",
        ),
        UniqueConstraint(
            "canonical_event_id",
            "account_id",
            "calendar_id",
            name="uq_provider_event_bindings_canonical_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_events.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    provider_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    independently_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
