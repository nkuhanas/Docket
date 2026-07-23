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
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.mapper import Mapper

from docket.domain.enums import (
    ActionStatus,
    ApprovalStatus,
    OperationStatus,
    QueueItemStatus,
)
from docket.models.base import Base, TimestampMixin, utc_now


class QueueItem(TimestampMixin, Base):
    __tablename__ = "queue_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'awaiting_approval', 'executing', 'completed', "
            "'failed', 'reconciliation_required', 'snoozed', 'ignored')",
            name="ck_queue_items_status",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_queue_items_priority",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    primary_source_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    deduplication_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    material_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=QueueItemStatus.PENDING.value, nullable=False
    )
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snooze_local_date: Mapped[date | None] = mapped_column(Date)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_code: Mapped[str | None] = mapped_column(String(128))
    resolution_note: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Action(TimestampMixin, Base):
    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint(
            "queue_item_id IS NOT NULL OR record_id IS NOT NULL",
            name="ck_actions_has_entity",
        ),
        CheckConstraint(
            "status IN ('available', 'approval_pending', 'ready', 'executing', "
            "'succeeded', 'rejected', 'expired', 'superseded', 'failed', "
            "'reconciliation_required')",
            name="ck_actions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    queue_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("queue_items.id", ondelete="RESTRICT")
    )
    record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("records.id", ondelete="RESTRICT")
    )
    action_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=ActionStatus.AVAILABLE.value, nullable=False
    )
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ActionRevision(Base):
    __tablename__ = "action_revisions"
    __table_args__ = (
        UniqueConstraint("action_id", "revision", name="uq_action_revisions_action_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False)
    target_versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by_actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_actor_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


@event.listens_for(ActionRevision, "before_update")
def _reject_action_revision_update(
    _mapper: Mapper[ActionRevision], _connection: Connection, _target: ActionRevision
) -> None:
    raise ValueError("Action revisions are immutable; create a new revision instead")


def _calendar_link_logical_key(context: Any) -> str:
    parameters = context.get_current_parameters()
    return f"course:{parameters['record_id']}:{parameters['meeting_id']}"


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'consumed', 'rejected', 'expired', 'superseded')",
            name="ck_approvals_status",
        ),
        Index(
            "uq_approvals_pending_revision",
            "action_revision_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_revisions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=ApprovalStatus.PENDING.value, nullable=False
    )
    interaction_token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    short_code_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    control_projection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discord_projections.id", name="fk_approvals_control_projection")
    )
    authorized_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_user_id: Mapped[str | None] = mapped_column(String(64))
    response_guild_id: Mapped[str | None] = mapped_column(String(64))
    response_channel_id: Mapped[str | None] = mapped_column(String(64))
    response_parent_channel_id: Mapped[str | None] = mapped_column(String(64))
    response_projection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discord_projections.id", name="fk_approvals_response_projection")
    )
    response_message_id: Mapped[str | None] = mapped_column(String(64))
    discord_interaction_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    response_note: Mapped[str | None] = mapped_column(String(1000))
    consumed_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id", name="fk_approvals_consumed_operation", use_alter=True)
    )


class Operation(TimestampMixin, Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial_failed', "
            "'failed', 'reconciliation_required')",
            name="ck_operations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
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


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        CheckConstraint("kind IN ('execute', 'reconcile')", name="ck_attempts_kind"),
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'unknown')",
            name="ck_attempts_status",
        ),
        Index(
            "uq_attempts_parent_number",
            "operation_id",
            "attempt_number",
            unique=True,
            postgresql_where=text("operation_item_id IS NULL"),
            sqlite_where=text("operation_item_id IS NULL"),
        ),
        Index(
            "uq_attempts_item_number",
            "operation_item_id",
            "attempt_number",
            unique=True,
            postgresql_where=text("operation_item_id IS NOT NULL"),
            sqlite_where=text("operation_item_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    operation_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operation_items.id", ondelete="CASCADE")
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


class CalendarLink(TimestampMixin, Base):
    __tablename__ = "calendar_links"
    __table_args__ = (
        CheckConstraint(
            "origin_kind IN ('course_meeting', 'standalone', 'adopted_provider_event')",
            name="ck_calendar_links_origin_kind",
        ),
        CheckConstraint(
            "recurrence_kind IN ('one_time', 'recurring')",
            name="ck_calendar_links_recurrence_kind",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_calendar_links_priority",
        ),
        CheckConstraint(
            "priority_basis IN ('default', 'explicit_operator')",
            name="ck_calendar_links_priority_basis",
        ),
        CheckConstraint(
            "(origin_kind = 'course_meeting' AND record_id IS NOT NULL "
            "AND meeting_id IS NOT NULL) OR "
            "(origin_kind IN ('standalone', 'adopted_provider_event') "
            "AND meeting_id IS NULL)",
            name="ck_calendar_links_origin_target",
        ),
        UniqueConstraint(
            "account_id",
            "calendar_id",
            "logical_key",
            name="uq_calendar_links_logical_target",
        ),
        UniqueConstraint(
            "account_id",
            "calendar_id",
            "external_event_id",
            name="uq_calendar_links_external_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("records.id", ondelete="RESTRICT")
    )
    meeting_id: Mapped[str | None] = mapped_column(String(128))
    origin_kind: Mapped[str] = mapped_column(
        String(32), default="course_meeting", nullable=False
    )
    logical_key: Mapped[str] = mapped_column(
        String(512), default=_calendar_link_logical_key, nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(1024))
    provider_correlation: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    last_synced_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recurrence_kind: Mapped[str] = mapped_column(
        String(16), default="recurring", nullable=False
    )
    system_tags: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["recurring", "timed", "course_meeting"], nullable=False
    )
    operator_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    priority_basis: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    reminder_plan_sha256: Mapped[str | None] = mapped_column(String(64))
    synced_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CalendarScheduleSnapshot(TimestampMixin, Base):
    __tablename__ = "calendar_schedule_snapshots"
    __table_args__ = (
        CheckConstraint(
            "item_count BETWEEN 1 AND 50",
            name="ck_calendar_schedule_snapshots_item_count",
        ),
        UniqueConstraint(
            "command_request_id",
            name="uq_calendar_schedule_snapshots_command_request",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    command_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("command_requests.id", ondelete="RESTRICT"), nullable=False
    )
    term_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("records.id", ondelete="RESTRICT"), nullable=False
    )
    term_record_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)


class OperationItem(TimestampMixin, Base):
    __tablename__ = "operation_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'reconciliation_required')",
            name="ck_operation_items_status",
        ),
        UniqueConstraint("operation_id", "item_key", name="uq_operation_items_operation_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(String(512), nullable=False)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
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
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
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
        CheckConstraint(
            "recurrence_kind IN ('one_time', 'recurring')",
            name="ck_calendar_event_cache_recurrence_kind",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_calendar_event_cache_priority",
        ),
        CheckConstraint(
            "priority_basis IN ('default', 'explicit_operator')",
            name="ck_calendar_event_cache_priority_basis",
        ),
        UniqueConstraint(
            "account_id",
            "calendar_id",
            "provider_event_id",
            name="uq_calendar_event_cache_provider_event",
        ),
        Index("ix_calendar_event_cache_timed", "account_id", "calendar_id", "start_at"),
        Index("ix_calendar_event_cache_all_day", "account_id", "calendar_id", "start_date"),
        Index(
            "ix_calendar_event_cache_generation",
            "account_id",
            "calendar_id",
            "snapshot_generation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
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


class ReminderRule(TimestampMixin, Base):
    __tablename__ = "reminder_rules"
    __table_args__ = (
        CheckConstraint("scope IN ('calendar', 'event')", name="ck_reminder_rules_scope"),
        CheckConstraint(
            "(scope = 'calendar' AND provider_event_id IS NULL) OR "
            "(scope = 'event' AND provider_event_id IS NOT NULL)",
            name="ck_reminder_rules_scope_event",
        ),
        CheckConstraint("lead_seconds >= 0", name="ck_reminder_rules_lead_seconds"),
        CheckConstraint(
            "source_kind IN ('legacy_explicit', 'canonical_plan')",
            name="ck_reminder_rules_source_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(1024))
    lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(
        String(32), default="legacy_explicit", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(String(64), nullable=False)


class CalendarReminderPlan(TimestampMixin, Base):
    __tablename__ = "calendar_reminder_plans"
    __table_args__ = (
        CheckConstraint(
            "lead_seconds BETWEEN 0 AND 2419200 AND lead_seconds % 60 = 0",
            name="ck_calendar_reminder_plans_lead_seconds",
        ),
        CheckConstraint(
            "status IN ('planned', 'activated', 'cancelled', 'reconciliation_required')",
            name="ck_calendar_reminder_plans_status",
        ),
        UniqueConstraint(
            "action_revision_id",
            "manifest_item_key",
            "lead_seconds",
            name="uq_calendar_reminder_plans_revision_item_lead",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_revisions.id", ondelete="CASCADE"), nullable=False
    )
    manifest_item_key: Mapped[str | None] = mapped_column(String(512))
    lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_channels: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    reminder_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reminder_rules.id", ondelete="RESTRICT")
    )
    provider_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarProfile(TimestampMixin, Base):
    __tablename__ = "calendar_profiles"
    __table_args__ = (
        CheckConstraint(
            "proposal_mode IN ('explicit_only', 'suggest', 'off')",
            name="ck_calendar_profiles_proposal_mode",
        ),
        CheckConstraint(
            "conflict_policy IN ('warn', 'block')",
            name="ck_calendar_profiles_conflict_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operator_user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    proposal_mode: Mapped[str] = mapped_column(String(32), default="suggest", nullable=False)
    default_reminder_lead_seconds: Mapped[list[int]] = mapped_column(
        JSON, default=lambda: [600], nullable=False
    )
    default_reminder_delivery_channels: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["google_popup", "docket_queue"], nullable=False
    )
    conflict_policy: Mapped[str] = mapped_column(String(16), default="warn", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ScheduledNotification(TimestampMixin, Base):
    __tablename__ = "scheduled_notifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'cancelled', 'failed')",
            name="ck_scheduled_notifications_status",
        ),
        UniqueConstraint(
            "reminder_rule_id",
            "provider_event_id",
            "event_start_key",
            name="uq_scheduled_notifications_occurrence",
        ),
        Index("ix_scheduled_notifications_due", "status", "scheduled_for"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reminder_rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_rules.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calendar_event_cache.id", ondelete="SET NULL")
    )
    daily_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discord_daily_threads.id", ondelete="RESTRICT"), index=True
    )
    provider_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    event_start_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    outbox_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outbox_events.id", ondelete="RESTRICT")
    )
    discord_message_id: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
