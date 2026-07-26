import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, TimestampMixin, utc_now


class ConnectorCheckpoint(TimestampMixin, Base):
    __tablename__ = "connector_checkpoints"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_connector_checkpoints_version"),
        UniqueConstraint(
            "account_id",
            "stream",
            name="uq_connector_checkpoints_account_stream",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    stream: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    observed_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceItem(TimestampMixin, Base):
    __tablename__ = "source_items"
    __table_args__ = (
        CheckConstraint("provider = 'gmail'", name="ck_source_items_provider"),
        CheckConstraint(
            "status IN ('staged', 'claimed', 'classified', 'ignored', 'failed')",
            name="ck_source_items_status",
        ),
        CheckConstraint("failure_count >= 0", name="ck_source_items_failure_count"),
        UniqueConstraint(
            "account_id",
            "provider",
            "external_object_id",
            "source_version",
            name="uq_source_items_external_version",
        ),
        UniqueConstraint(
            "account_id",
            "provider",
            "source_fingerprint",
            name="uq_source_items_fingerprint",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_object_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    external_parent_id: Mapped[str | None] = mapped_column(String(1024))
    source_version: Mapped[str] = mapped_column(String(255), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    minimal_headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="staged", nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QueueItemSource(Base):
    __tablename__ = "queue_item_sources"
    __table_args__ = (
        CheckConstraint(
            "relationship IN ('primary', 'supporting', 'update')",
            name="ck_queue_item_sources_relationship",
        ),
    )

    queue_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queue_items.id", ondelete="CASCADE"), primary_key=True
    )
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_items.id", ondelete="RESTRICT"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
