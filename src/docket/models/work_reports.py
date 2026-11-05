"""Owner-bound derived query snapshots, independent of financial claims."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class WorkReportSnapshot(Base):
    __tablename__ = "work_report_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        nullable=False,
        default=lambda: new_public_ref("wrep"),
    )
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkReportMember(Base):
    __tablename__ = "work_report_members"

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_report_snapshots.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    work_ref: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    engagement_revision_ref: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_revisions.ref_id", ondelete="RESTRICT"),
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Retained work query snapshots are immutable")


def _seal_only(_mapper: object, _connection: object, target: WorkReportSnapshot) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    prior = state.attrs.sealed_at.history.deleted
    if changed != {"sealed_at"} or target.sealed_at is None or (prior and prior[0] is not None):
        _immutable(_mapper, _connection, target)


event.listen(WorkReportSnapshot, "before_update", _seal_only)
event.listen(WorkReportSnapshot, "before_delete", _immutable)
event.listen(WorkReportMember, "before_update", _immutable)
event.listen(WorkReportMember, "before_delete", _immutable)
