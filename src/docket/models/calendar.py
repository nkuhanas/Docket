from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.enums import OperationStatus
from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, TimestampMixin, utc_now


class Operation(TimestampMixin, Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial_failed', "
            "'failed', 'reconciliation_required')",
            name="ck_operations_status",
        ),
        CheckConstraint(
            "operation_type IN ('calendar_create_event', 'calendar_update_event', "
            "'calendar_update_reminders', 'calendar_cancel_event', "
            "'calendar_configure_lane', 'calendar_delete_lane')",
            name="ck_operations_type",
        ),
        Index("ix_operations_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("op")
    )
    originating_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    canonical_target_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    predecessor_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=OperationStatus.PENDING.value, nullable=False
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_correlation: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    last_error_message: Mapped[str | None] = mapped_column(String(1000))


class OperationTarget(TimestampMixin, Base):
    __tablename__ = "operation_targets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'reconciliation_required')",
            name="ck_operation_targets_status",
        ),
        UniqueConstraint("operation_id", "target_key", name="uq_operation_targets_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    target_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_target_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        CheckConstraint("kind IN ('execute', 'reconcile')", name="ck_attempts_kind"),
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'unknown')",
            name="ck_attempts_status",
        ),
        Index(
            "uq_attempts_parent_number", "operation_id", "attempt_number", unique=True,
            postgresql_where=text("operation_target_id IS NULL"),
            sqlite_where=text("operation_target_id IS NULL"),
        ),
        Index(
            "uq_attempts_target_number", "operation_target_id", "attempt_number", unique=True,
            postgresql_where=text("operation_target_id IS NOT NULL"),
            sqlite_where=text("operation_target_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    operation_target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operation_targets.id", ondelete="CASCADE")
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarSyncState(TimestampMixin, Base):
    __tablename__ = "calendar_sync_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'syncing', 'current', 'stale', 'failed')",
            name="ck_calendar_sync_states_status",
        ),
        UniqueConstraint("account_id", "calendar_id", name="uq_calendar_sync_states_target"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_generation: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarEventCache(TimestampMixin, Base):
    __tablename__ = "calendar_event_cache"
    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed', 'tentative', 'cancelled')",
            name="ck_calendar_event_cache_status",
        ),
        UniqueConstraint(
            "account_id", "calendar_id", "provider_event_id",
            name="uq_calendar_event_cache_provider_event",
        ),
        Index("ix_calendar_event_cache_timed", "account_id", "calendar_id", "start_at"),
        Index("ix_calendar_event_cache_all_day", "account_id", "calendar_id", "start_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    snapshot_generation: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    recurring_event_id: Mapped[str | None] = mapped_column(String(1024))
    original_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(1000))
    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str | None] = mapped_column(String(128))
    has_attendees: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    organizer_is_self: Mapped[bool | None] = mapped_column(Boolean)
    recurrence_kind: Mapped[str] = mapped_column(String(16), default="one_time", nullable=False)
    system_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    operator_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    priority_basis: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    provider_reminders: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(1024))
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalendarLane(TimestampMixin, Base):
    __tablename__ = "calendar_lanes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('unprovisioned', 'provisioning', 'active', 'failed', "
            "'deleting', 'deleted')",
            name="ck_calendar_lanes_status",
        ),
        CheckConstraint(
            "color_hex LIKE '#______' AND length(color_hex) = 7",
            name="ck_calendar_lanes_color_hex",
        ),
        UniqueConstraint("account_id", "lane", name="uq_calendar_lanes_account_lane"),
        UniqueConstraint("account_id", "calendar_id", name="uq_calendar_lanes_calendar"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("lane")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    lane: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    color_hex: Mapped[str] = mapped_column(String(7), nullable=False)
    calendar_id: Mapped[str | None] = mapped_column(String(1024))
    operator_policy_text: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unprovisioned", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ScheduledNotification(TimestampMixin, Base):
    __tablename__ = "scheduled_notifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'cancelled', 'failed')",
            name="ck_scheduled_notifications_status",
        ),
        UniqueConstraint(
            "reminder_plan_ref", "trigger_key",
            name="uq_scheduled_notifications_plan_trigger",
        ),
        Index("ix_scheduled_notifications_due", "status", "scheduled_for"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reminder_plan_ref: Mapped[str] = mapped_column(
        ForeignKey("reminder_plans.ref_id", ondelete="RESTRICT"), nullable=False
    )
    trigger_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    outbox_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outbox_events.id", ondelete="RESTRICT")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
