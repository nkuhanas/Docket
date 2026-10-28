"""Durable inbox and independently acknowledged webhook deliveries."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class PluginNotification(Base):
    __tablename__ = "plugin_notifications"
    __table_args__ = (Index("ix_plugin_notifications_owner", "operator_ref", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        default=lambda: new_public_ref("notice"),
        nullable=False,
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MCPEventSubscription(Base):
    __tablename__ = "mcp_event_subscriptions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'stopped', 'expired')", name="ck_mcp_sub_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    principal_ref: Mapped[str] = mapped_column(
        ForeignKey("oauth_principals.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    grant_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    configuration: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MCPEventDelivery(Base):
    __tablename__ = "mcp_event_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_ref", "subscription_ref", name="uq_mcp_delivery_target"),
        CheckConstraint(
            "status IN ('pending', 'running', 'accepted', 'failed', 'stopped')",
            name="ck_mcp_delivery_status",
        ),
        Index("ix_mcp_delivery_due", "status", "next_attempt_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        default=lambda: new_public_ref("ndel"),
        nullable=False,
    )
    notification_ref: Mapped[str] = mapped_column(
        ForeignKey("plugin_notifications.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_ref: Mapped[str] = mapped_column(
        ForeignKey("mcp_event_subscriptions.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))


class MCPEventAttempt(Base):
    __tablename__ = "mcp_event_attempts"
    __table_args__ = (
        UniqueConstraint("delivery_ref", "attempt_number", name="uq_mcp_attempt_number"),
        CheckConstraint(
            "status IN ('running', 'accepted', 'transient', 'failed', 'unknown')",
            name="ck_mcp_attempt_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    delivery_ref: Mapped[str] = mapped_column(
        ForeignKey("mcp_event_deliveries.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))


def _retain_notice(_mapper: object, _connection: object, _target: PluginNotification) -> None:
    raise ValueError("PluginNotification is immutable")


event.listen(PluginNotification, "before_update", _retain_notice)
event.listen(PluginNotification, "before_delete", _retain_notice)


def _retain_row(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Notification delivery history is retained")


@event.listens_for(MCPEventSubscription, "before_update")
def _retain_subscription(
    _mapper: object, _connection: object, target: MCPEventSubscription
) -> None:
    if any(
        inspect(target).attrs[key].history.has_changes()
        for key in (
            "id",
            "ref_id",
            "principal_ref",
            "identity_hash",
            "event_name",
            "arguments",
            "created_at",
        )
    ):
        raise ValueError("Subscription identity is immutable")


@event.listens_for(MCPEventDelivery, "before_update")
def _retain_delivery(_mapper: object, _connection: object, target: MCPEventDelivery) -> None:
    if any(
        inspect(target).attrs[key].history.has_changes()
        for key in (
            "id",
            "ref_id",
            "notification_ref",
            "subscription_ref",
        )
    ):
        raise ValueError("Delivery identity is immutable")


@event.listens_for(MCPEventAttempt, "before_update")
def _retain_attempt(_mapper: object, _connection: object, target: MCPEventAttempt) -> None:
    state = inspect(target)
    history = state.attrs.status.history
    before = history.deleted[0] if history.deleted else target.status
    if before != "running" or any(
        state.attrs[key].history.has_changes()
        for key in (
            "id",
            "delivery_ref",
            "attempt_number",
            "started_at",
        )
    ):
        raise ValueError("Completed delivery attempts are immutable")


for _model in (MCPEventSubscription, MCPEventDelivery, MCPEventAttempt):
    event.listen(_model, "before_delete", _retain_row)
