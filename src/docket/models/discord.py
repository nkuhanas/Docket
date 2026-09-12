import uuid
from datetime import date, datetime
from typing import Any

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
    event,
    inspect,
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
        UniqueConstraint(
            "guild_id",
            "source_channel_id",
            "source_message_id",
            name="uq_conversational_tool_trace_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("trace")
    )
    guild_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Source-wide projection state. Execution bindings and observations live
    # in distinct segments; this row is never rebound to a replacement gateway.
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TraceExecutionSegment(Base):
    """An immutable execution binding with append-only observation evolution.

    Existing trace evidence is moved losslessly to a retained_trace segment by
    the cutover. Only admitted_execution segments may receive new gateway work.
    No lease is guessed for previously captured evidence.
    """

    __tablename__ = "trace_execution_segments"
    __table_args__ = (
        UniqueConstraint("trace_ref", "execution_index", name="uq_trace_execution_index"),
        UniqueConstraint("execution_lease_id", name="uq_trace_execution_lease"),
        CheckConstraint("execution_index >= 1", name="ck_trace_execution_index"),
        CheckConstraint("last_ordinal >= 0", name="ck_trace_execution_ordinal"),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'interrupted')",
            name="ck_trace_execution_status",
        ),
        CheckConstraint(
            "(binding_basis = 'admitted_execution' AND execution_lease_id IS NOT NULL "
            "AND gateway_instance_ref IS NOT NULL) OR "
            "(binding_basis = 'retained_trace' AND execution_lease_id IS NULL)",
            name="ck_trace_execution_binding",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trace_ref: Mapped[str] = mapped_column(
        ForeignKey("conversational_tool_traces.ref_id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    execution_index: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_lease_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("execution_leases.id", ondelete="RESTRICT"),
    )
    binding_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    gateway_instance_ref: Mapped[str | None] = mapped_column(String(40), index=True)
    tool_contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    last_ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


def _protect_execution_binding(_mapper: object, _connection: object, target: object) -> None:
    state = inspect(target)
    assert state is not None
    if any(state.attrs[name].history.has_changes() for name in (
        "id", "trace_ref", "execution_index", "execution_lease_id", "binding_basis",
        "gateway_instance_ref", "tool_contract_version", "tool_contract_hash",
        "caller_profile", "started_at",
    )):
        raise ValueError("TraceExecutionSegment binding is immutable")


event.listen(TraceExecutionSegment, "before_update", _protect_execution_binding)


def _protect_execution_delete(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("TraceExecutionSegment binding is immutable")


event.listen(TraceExecutionSegment, "before_delete", _protect_execution_delete)


class TraceTimingObservation(Base):
    """Immutable, payload-free closed intervals observed by the trusted gateway."""

    __tablename__ = "trace_timing_observations"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('model_request', 'context_schema', 'local_validation')",
            name="ck_trace_timing_observations_phase",
        ),
        CheckConstraint("ended_at >= started_at", name="ck_trace_timing_observations_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    trace_ref: Mapped[str] = mapped_column(
        ForeignKey("conversational_tool_traces.ref_id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    trace_execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trace_execution_segments.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _reject_timing_rewrite(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} is immutable")


event.listen(TraceTimingObservation, "before_update", _reject_timing_rewrite)
event.listen(TraceTimingObservation, "before_delete", _reject_timing_rewrite)
