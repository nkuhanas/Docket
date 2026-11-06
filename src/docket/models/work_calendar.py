"""Versioned visibility with immutable links to actual-work revisions."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class WorkCalendarProjection(Base):
    __tablename__ = "work_calendar_projections"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_work_calendar_version"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("wcal")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    work_ref: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    observed_work_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    event_ref: Mapped[str] = mapped_column(
        ForeignKey("canonical_events.ref_id", ondelete="RESTRICT"), nullable=False
    )
    calendar_ref: Mapped[str] = mapped_column(
        ForeignKey("calendar_lanes.ref_id", ondelete="RESTRICT"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    renderer_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    needs_refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkCalendarRevision(Base):
    __tablename__ = "work_calendar_revisions"
    __table_args__ = (
        UniqueConstraint("projection_ref", "version", name="uq_work_calendar_revision"),
        CheckConstraint("version >= 1", name="ck_work_calendar_revision_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("wcrv")
    )
    projection_ref: Mapped[str] = mapped_column(
        ForeignKey("work_calendar_projections.ref_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    observed_work_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    event_ref: Mapped[str] = mapped_column(
        ForeignKey("canonical_events.ref_id", ondelete="RESTRICT"), nullable=False
    )
    calendar_ref: Mapped[str] = mapped_column(
        ForeignKey("calendar_lanes.ref_id", ondelete="RESTRICT"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    renderer_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    needs_refresh: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def retain(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Work Calendar revisions are retained")


event.listen(WorkCalendarRevision, "before_update", retain)
event.listen(WorkCalendarRevision, "before_delete", retain)
event.listen(WorkCalendarProjection, "before_delete", retain)
