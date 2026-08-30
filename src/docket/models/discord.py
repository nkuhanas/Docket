import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
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


class ConversationalToolTrace(TimestampMixin, Base):
    __tablename__ = "conversational_tool_traces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'interrupted')",
            name="ck_conversational_tool_traces_status",
        ),
        CheckConstraint(
            "last_ordinal BETWEEN 0 AND 100",
            name="ck_conversational_tool_traces_last_ordinal",
        ),
        UniqueConstraint(
            "guild_id",
            "source_channel_id",
            "source_message_id",
            name="uq_conversational_tool_trace_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("trace")
    )
    guild_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_contract_version: Mapped[str] = mapped_column(
        String(128), default="pre-contract-bootstrap-2026-08-27", nullable=False
    )
    tool_contract_hash: Mapped[str] = mapped_column(
        String(64), default="0" * 64, nullable=False
    )
    caller_profile: Mapped[str] = mapped_column(
        String(32), default="interactive", nullable=False
    )
    gateway_instance_ref: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    last_ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
