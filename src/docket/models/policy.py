import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
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


class Preference(TimestampMixin, Base):
    __tablename__ = "preferences"
    __table_args__ = (
        CheckConstraint(
            "policy_kind IN ('behavior', 'suppression', 'calendar_route')",
            name="ck_preferences_policy_kind",
        ),
        CheckConstraint(
            "target_type IN ('global', 'entity', 'identity', 'source', "
            "'semantic_class')",
            name="ck_preferences_target_type",
        ),
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_preferences_status",
        ),
        UniqueConstraint("preference_key", name="uq_preferences_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("pref")
    )
    preference_key: Mapped[str] = mapped_column(String(255), nullable=False)
    policy_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(40))
    target_key: Mapped[str | None] = mapped_column(String(1024))
    semantic_class: Mapped[str | None] = mapped_column(String(64))
    policy_text: Mapped[str] = mapped_column(Text, nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class LaneRoutingDecision(TimestampMixin, Base):
    __tablename__ = "lane_routing_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision_kind IN ('explicit_operator', 'structured_preference', "
            "'entity_rule', 'historical_precedent', 'semantic_inference')",
            name="ck_lane_routing_decisions_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'historical', 'retracted')",
            name="ck_lane_routing_decisions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("route")
    )
    lane_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calendar_lanes.id", ondelete="RESTRICT"), nullable=False
    )
    lane_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    event_ref: Mapped[str | None] = mapped_column(String(40))
    organization_ref: Mapped[str | None] = mapped_column(String(40))
    recurring_identity: Mapped[str | None] = mapped_column(String(512))
    decision_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    applicability_scope: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    operator_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
