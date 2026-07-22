import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'consumed', 'rejected', 'expired', "
            "'superseded')",
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
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'reconciliation_required')",
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
        UniqueConstraint("operation_id", "attempt_number", name="uq_attempts_operation_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
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
        UniqueConstraint(
            "record_id",
            "meeting_id",
            "account_id",
            "calendar_id",
            name="uq_calendar_links_target",
        ),
        UniqueConstraint(
            "account_id",
            "calendar_id",
            "external_event_id",
            name="uq_calendar_links_external_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("records.id", ondelete="RESTRICT"), nullable=False
    )
    meeting_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_etag: Mapped[str | None] = mapped_column(String(1024))
    provider_correlation: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    last_synced_version: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
