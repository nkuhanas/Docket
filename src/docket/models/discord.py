import uuid
from datetime import date, datetime

from sqlalchemy import (
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

from docket.models.base import Base, TimestampMixin


class DiscordDailyThread(TimestampMixin, Base):
    __tablename__ = "discord_daily_threads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'archived', 'failed')",
            name="ck_discord_daily_threads_status",
        ),
        UniqueConstraint(
            "guild_id", "channel_id", "local_date", name="uq_discord_daily_thread_date"
        ),
        UniqueConstraint("thread_id", name="uq_discord_daily_threads_thread_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    thread_name: Mapped[str] = mapped_column(String(100), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    auto_archive_minutes: Mapped[int | None] = mapped_column(Integer)
    lifecycle_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class DiscordProjection(TimestampMixin, Base):
    __tablename__ = "discord_projections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_discord_projections_status",
        ),
        CheckConstraint(
            "view_mode IN ('summary', 'schedule_review', 'decision', 'schedule_failures')",
            name="ck_discord_projections_view_mode",
        ),
        CheckConstraint(
            "((view_mode IN ('schedule_review', 'schedule_failures') "
            "AND view_page BETWEEN 1 AND 5) OR "
            "(view_mode IN ('summary', 'decision') AND view_page IS NULL))",
            name="ck_discord_projections_view_page",
        ),
        CheckConstraint(
            "reviewed_through_page BETWEEN 0 AND 5",
            name="ck_discord_projections_reviewed_through_page",
        ),
        UniqueConstraint(
            "queue_item_id", "daily_thread_id", name="uq_discord_projection_item_thread"
        ),
        UniqueConstraint("message_id", name="uq_discord_projections_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    queue_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queue_items.id", ondelete="RESTRICT"), nullable=False
    )
    daily_thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discord_daily_threads.id", ondelete="RESTRICT"), nullable=False
    )
    projection_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(64))
    render_schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    render_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    component_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    view_action_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("action_revisions.id", ondelete="SET NULL")
    )
    view_mode: Mapped[str] = mapped_column(String(32), default="summary", nullable=False)
    view_page: Mapped[int | None] = mapped_column(Integer)
    reviewed_through_page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
