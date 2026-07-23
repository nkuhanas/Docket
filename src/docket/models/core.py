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
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docket.domain.enums import CommandStatus, OutboxStatus, RecordStatus
from docket.models.base import Base, TimestampMixin, utc_now


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("provider IN ('google', 'discord')", name="ck_accounts_provider"),
        UniqueConstraint("provider", "external_account_id", name="uq_accounts_provider_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    email_address: Mapped[str | None] = mapped_column(String(320))
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(512))


class Record(TimestampMixin, Base):
    __tablename__ = "records"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_records_status"),
        UniqueConstraint("record_type", "canonical_key", name="uq_records_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(512), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RecordStatus.ACTIVE.value, nullable=False
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    valid_from_date: Mapped[date | None] = mapped_column(Date)
    valid_until_date: Mapped[date | None] = mapped_column(Date)

    sources: Mapped[list["RecordSource"]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )


class RecordSource(Base):
    __tablename__ = "record_sources"
    __table_args__ = (
        UniqueConstraint(
            "record_id",
            "source_request_key",
            name="uq_record_sources_record_request",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    source_object_id: Mapped[str | None] = mapped_column(String(255))
    source_request_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(255))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    record: Mapped[Record] = relationship(back_populates="sources")


class CommandRequest(Base):
    __tablename__ = "command_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'succeeded', 'failed')",
            name="ck_command_requests_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    operation_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default=CommandStatus.IN_PROGRESS.value, nullable=False
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'failed')",
            name="ck_outbox_events_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=OutboxStatus.PENDING.value, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
