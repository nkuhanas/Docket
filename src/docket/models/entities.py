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

from docket.models.base import Base, TimestampMixin, utc_now


class Entity(TimestampMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "entity_class IN ('institution', 'organization', 'course', 'person', "
            "'location', 'project', 'service')",
            name="ck_entities_class",
        ),
        CheckConstraint(
            "status IN ('active', 'provisional', 'merged', 'archived')",
            name="ck_entities_status",
        ),
        Index(
            "uq_entities_active_identity",
            "entity_class",
            "normalized_name",
            unique=True,
            postgresql_where=text("status IN ('active', 'provisional')"),
            sqlite_where=text("status IN ('active', 'provisional')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_class: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
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


class EntityRelation(TimestampMixin, Base):
    __tablename__ = "entity_relations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'retracted')",
            name="ck_entity_relations_status",
        ),
        CheckConstraint(
            "predicate IN ('works_for', 'member_of', 'affiliated_with', 'advises', "
            "'instructs', 'reports_to', 'collaborates_with', 'knows', 'friend_of', "
            "'classmate_of', 'leads', 'participates_in', 'located_at', 'uses', 'supports')",
            name="ck_entity_relations_predicate",
        ),
        UniqueConstraint(
            "subject_entity_id",
            "predicate",
            "object_entity_id",
            name="uq_entity_relations_triple",
        ),
        Index("ix_entity_relations_subject_predicate", "subject_entity_id", "predicate"),
        Index("ix_entity_relations_object_predicate", "object_entity_id", "predicate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    object_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class EntityResolution(Base):
    __tablename__ = "entity_resolutions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('resolved', 'unresolved', 'ambiguous', 'provisional')",
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
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_items.id", ondelete="SET NULL")
    )
    semantic_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("semantic_candidates.id", ondelete="SET NULL")
    )
    corrected_resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity_resolutions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
