"""Typed performed work with immutable revisions and normalized time segments."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class Engagement(Base):
    __tablename__ = "engagements"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_engagement_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        default=lambda: new_public_ref("eng"),
        nullable=False,
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EngagementRevision(Base):
    __tablename__ = "engagement_revisions"
    __table_args__ = (
        UniqueConstraint("engagement_ref", "revision", name="uq_engagement_revision"),
        CheckConstraint("revision >= 1", name="ck_engagement_revision"),
        CheckConstraint(
            "kind IN ('unknown', 'employment', 'contract', 'research', 'volunteer', 'personal')",
            name="ck_engagement_kind",
        ),
        CheckConstraint(
            "effective_start IS NULL OR effective_end IS NULL OR effective_end >= effective_start",
            name="ck_engagement_dates",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        default=lambda: new_public_ref("erev"),
        nullable=False,
    )
    engagement_ref: Mapped[str] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT"),
    )
    payer_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    affiliation_ref: Mapped[str | None] = mapped_column(
        ForeignKey("affiliations.ref_id", ondelete="RESTRICT"),
    )
    effective_start: Mapped[date | None] = mapped_column(Date)
    effective_end: Mapped[date | None] = mapped_column(Date)
    role: Mapped[str | None] = mapped_column(String(512))
    reporting_notes: Mapped[str | None] = mapped_column(Text)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkSession(Base):
    __tablename__ = "work_sessions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_work_version"),
        CheckConstraint(
            "lifecycle IN ('running', 'paused', 'recorded', 'retracted')",
            name="ck_work_lifecycle",
        ),
        Index(
            "uq_work_primary_timer",
            "operator_ref",
            unique=True,
            postgresql_where=text("lifecycle IN ('running', 'paused') AND NOT superseded"),
            sqlite_where=text("lifecycle IN ('running', 'paused') AND NOT superseded"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        default=lambda: new_public_ref("work"),
        nullable=False,
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), default="recorded", nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkSessionRevision(Base):
    __tablename__ = "work_session_revisions"
    __table_args__ = (
        UniqueConstraint("work_ref", "revision", name="uq_work_revision"),
        CheckConstraint("revision >= 1", name="ck_work_revision"),
        CheckConstraint(
            "lifecycle IN ('running', 'paused', 'recorded', 'retracted')",
            name="ck_work_revision_lifecycle",
        ),
        CheckConstraint(
            "time_precision IN ('interval', 'duration', 'incomplete', 'timer', 'composite')",
            name="ck_work_precision",
        ),
        CheckConstraint("duration_us IS NULL OR duration_us >= 0", name="ck_work_duration"),
        CheckConstraint(
            "start_at IS NULL OR end_at IS NULL OR end_at >= start_at",
            name="ck_work_bounds",
        ),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end > period_start",
            name="ck_work_period",
        ),
        CheckConstraint(
            "reported_compensation_class IS NULL OR reported_compensation_class "
            "IN ('billable', 'paid_work', 'unpaid', 'unknown')",
            name="ck_work_reported_class",
        ),
        CheckConstraint(
            "work_mode IS NULL OR work_mode IN ('remote', 'onsite', 'travel')",
            name="ck_work_mode",
        ),
        CheckConstraint(
            "source_method IN ('report', 'import', 'inference', 'timer')",
            name="ck_work_source",
        ),
        Index("ix_work_revision_engagement_date", "engagement_ref", "work_date"),
        Index("ix_work_revision_project_date", "project_ref", "work_date"),
        Index("ix_work_revision_bounds", "start_at", "end_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        default=lambda: new_public_ref("wrev"),
        nullable=False,
    )
    work_ref: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(128))
    time_precision: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    end_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    duration_us: Mapped[int | None] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    work_date: Mapped[date | None] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    organization_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT"),
    )
    engagement_ref: Mapped[str | None] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT"),
    )
    project_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    item_ref: Mapped[str | None] = mapped_column(ForeignKey("items.ref_id", ondelete="RESTRICT"))
    task_ref: Mapped[str | None] = mapped_column(ForeignKey("tasks.ref_id", ondelete="RESTRICT"))
    event_ref: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_events.ref_id", ondelete="RESTRICT"),
    )
    place_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    attribution_label: Mapped[str | None] = mapped_column(String(512))
    reported_compensation_class: Mapped[str | None] = mapped_column(String(32))
    work_mode: Mapped[str | None] = mapped_column(String(32))
    reported_location: Mapped[str | None] = mapped_column(String(1000))
    source_method: Mapped[str] = mapped_column(String(32), default="report", nullable=False)
    uncertainty: Mapped[str | None] = mapped_column(Text)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkSegment(Base):
    __tablename__ = "work_segments"
    __table_args__ = (
        UniqueConstraint("revision_id", "ordinal", name="uq_work_segment_order"),
        CheckConstraint("ordinal >= 0", name="ck_work_segment_order"),
        CheckConstraint("kind IN ('active', 'break')", name="ck_work_segment_kind"),
        CheckConstraint("end_at IS NULL OR end_at >= start_at", name="ck_work_segment_bounds"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_session_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset_seconds: Mapped[int | None] = mapped_column(Integer)


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Work revisions and segments are immutable")


for _model in (EngagementRevision, WorkSessionRevision, WorkSegment):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

for _head in (Engagement, WorkSession):
    event.listen(_head, "before_delete", _immutable)
