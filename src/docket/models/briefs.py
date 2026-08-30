import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, TimestampMixin


class TriageWindow(TimestampMixin, Base):
    __tablename__ = "triage_windows"
    __table_args__ = (
        CheckConstraint(
            "window_kind IN ('overnight', 'waking')",
            name="ck_triage_windows_kind",
        ),
        CheckConstraint(
            "status IN ('open', 'sealed', 'published')",
            name="ck_triage_windows_status",
        ),
        UniqueConstraint("window_kind", "local_date", name="uq_triage_windows_kind_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    window_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class TriageWindowMembership(Base):
    __tablename__ = "triage_window_memberships"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('include', 'suppress')",
            name="ck_triage_window_memberships_disposition",
        ),
    )

    window_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_windows.id", ondelete="CASCADE"), primary_key=True
    )
    brief_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brief_entries.id", ondelete="CASCADE"), primary_key=True
    )
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(256))


class DailyBrief(TimestampMixin, Base):
    __tablename__ = "daily_briefs"
    __table_args__ = (
        CheckConstraint(
            "brief_kind IN ('morning', 'night')",
            name="ck_daily_briefs_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_daily_briefs_status",
        ),
        UniqueConstraint("brief_kind", "local_date", name="uq_daily_briefs_kind_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("brief")
    )
    brief_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_windows.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interval_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    case_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    projection_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    projection_ref: Mapped[str | None] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class DailyBriefEntryMembership(Base):
    __tablename__ = "daily_brief_entry_memberships"
    __table_args__ = (
        UniqueConstraint(
            "brief_id", "brief_entry_id", name="uq_daily_brief_entry_membership"
        ),
    )

    brief_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_briefs.id", ondelete="CASCADE"), primary_key=True
    )
    brief_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brief_entries.id", ondelete="RESTRICT"), primary_key=True
    )
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
