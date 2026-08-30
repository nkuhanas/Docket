import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, TimestampMixin, utc_now


class Entity(TimestampMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "entity_class IN ('institution', 'organization', 'course', 'person', "
            "'course_section', 'place', 'location', 'project', 'service')",
            name="ck_entities_class",
        ),
        CheckConstraint(
            "status IN ('active', 'merged', 'historical', 'retracted')",
            name="ck_entities_status",
        ),
        CheckConstraint(
            "registration_state IN ('registered', 'historical')",
            name="ck_entities_registration_state",
        ),
        Index(
            "uq_entities_active_identity",
            "entity_class",
            "normalized_name",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("ent")
    )
    entity_class: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    registration_state: Mapped[str] = mapped_column(
        String(32), default="registered", nullable=False
    )
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class EntityAlias(TimestampMixin, Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "normalized_alias", name="uq_entity_aliases_identity"),
        Index("ix_entity_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(512), nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class EntityResolution(Base):
    __tablename__ = "entity_resolutions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('resolved', 'unresolved', 'ambiguous')",
            name="ck_entity_resolutions_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_class: Mapped[str] = mapped_column(String(32), nullable=False)
    mention: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_mention: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    resolved_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    candidate_entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(40))
    corrected_resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity_resolutions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
