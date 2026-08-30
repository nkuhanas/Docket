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


class OperatorProjection(Base):
    """One immutable transport-independent presentation revision."""

    __tablename__ = "operator_projections"
    __table_args__ = (
        CheckConstraint(
            "projection_kind IN ('attention_case', 'daily_brief', 'clarification', "
            "'agent_response', 'reminder', 'operational_status')",
            name="ck_operator_projections_kind",
        ),
        Index(
            "ix_operator_projections_primary_created",
            "primary_public_ref",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("proj")
    )
    projection_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_public_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    primary_revision_ref: Mapped[str | None] = mapped_column(String(40))
    supersedes_projection_ref: Mapped[str | None] = mapped_column(String(40))
    intent_session_ref: Mapped[str | None] = mapped_column(String(40))
    case_ref: Mapped[str | None] = mapped_column(String(40))
    case_revision_ref: Mapped[str | None] = mapped_column(String(40))
    brief_ref: Mapped[str | None] = mapped_column(String(40))
    semantic_content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    visible_text: Mapped[str] = mapped_column(Text, nullable=False)
    render_schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    render_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    component_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ProjectionDelivery(Base):
    """Mutable delivery state subordinate to an immutable projection."""

    __tablename__ = "projection_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'failed')",
            name="ck_projection_deliveries_status",
        ),
        UniqueConstraint(
            "projection_ref",
            "transport",
            "destination_ref",
            name="uq_projection_deliveries_target",
        ),
        UniqueConstraint(
            "transport",
            "external_message_ref",
            name="uq_projection_deliveries_message",
        ),
        Index("ix_projection_deliveries_status", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    projection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operator_projections.id", ondelete="RESTRICT"), nullable=False
    )
    projection_ref: Mapped[str] = mapped_column(
        ForeignKey("operator_projections.ref_id", ondelete="RESTRICT"), nullable=False
    )
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    destination_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_message_ref: Mapped[str | None] = mapped_column(String(512))
    external_message_ref: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


def _reject_projection_mutation(
    _mapper: object,
    _connection: object,
    _target: OperatorProjection,
) -> None:
    raise ValueError("OperatorProjection is immutable; create a new revision")


event.listen(OperatorProjection, "before_update", _reject_projection_mutation)
event.listen(OperatorProjection, "before_delete", _reject_projection_mutation)
