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
from docket.models.base import Base, TimestampMixin


class CanonicalEvent(TimestampMixin, Base):
    __tablename__ = "canonical_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'active', 'cancelled', 'archived')",
            name="ck_canonical_events_status",
        ),
        CheckConstraint(
            "authority IN ('explicit_operator', 'deterministic_rule')",
            name="ck_canonical_events_authority",
        ),
        UniqueConstraint("canonical_key", name="uq_canonical_events_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("evt")
    )
    canonical_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    event_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    entity_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_labels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lane_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calendar_lanes.id", ondelete="RESTRICT")
    )
    lane_ref: Mapped[str | None] = mapped_column(String(40))
    routing_decision_ref: Mapped[str | None] = mapped_column(String(40))
    operator_policy_text: Mapped[str | None] = mapped_column(Text)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)


class ProviderEventBinding(TimestampMixin, Base):
    __tablename__ = "provider_event_bindings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'cancelled', 'diverged')",
            name="ck_provider_event_bindings_status",
        ),
        CheckConstraint(
            "target_kind IN ('event', 'temporal_projection')",
            name="ck_provider_event_bindings_target_kind",
        ),
        UniqueConstraint(
            "account_id",
            "calendar_id",
            "provider_event_id",
            name="uq_provider_event_bindings_target",
        ),
        UniqueConstraint(
            "canonical_target_ref",
            "account_id",
            "calendar_id",
            name="uq_provider_event_bindings_canonical_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_target_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    provider_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    independently_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
