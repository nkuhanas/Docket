"""Durable evidence intake and separately revocable collector authority."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class FinancialCollector(Base):
    __tablename__ = "financial_collectors"
    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("fcol")
    )
    operator_ref: Mapped[str] = mapped_column(String(255))
    identity: Mapped[str] = mapped_column(String(255), unique=True)
    credential_digest: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=1)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class FinancialIntake(Base):
    __tablename__ = "financial_evidence_intake"
    __table_args__ = (UniqueConstraint("operator_ref", "origin_key", "delivery_key"),)
    source_ref: Mapped[str] = mapped_column(ForeignKey("sources.ref_id"), primary_key=True)
    operator_ref: Mapped[str] = mapped_column(String(255), index=True)
    origin_key: Mapped[str] = mapped_column(String(255))
    delivery_key: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    request_ref: Mapped[str | None] = mapped_column(ForeignKey("authenticated_requests.ref_id"))
    collector_ref: Mapped[str | None] = mapped_column(ForeignKey("financial_collectors.ref_id"))
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    declarations: Mapped[dict[str, Any]] = mapped_column(JSON)
    media_type: Mapped[str] = mapped_column(String(64))
    byte_count: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    inference: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    inference_attempts: Mapped[int] = mapped_column(default=0)
    next_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FinancialCandidate(Base):
    __tablename__ = "financial_evidence_candidates"
    __table_args__ = (UniqueConstraint("source_ref", "ordinal"),)
    ref_id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_public_ref("fcand")
    )
    source_ref: Mapped[str] = mapped_column(ForeignKey("financial_evidence_intake.source_ref"))
    ordinal: Mapped[int]
    facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    purchase_key: Mapped[str | None] = mapped_column(String(64), index=True)
    disposition: Mapped[str] = mapped_column(String(40))
    expense_ref: Mapped[str | None] = mapped_column(ForeignKey("expenses.ref_id"))
    activity_ref: Mapped[str | None] = mapped_column(ForeignKey("funding_activity.ref_id"))
    instrument_revision_ref: Mapped[str | None] = mapped_column(
        ForeignKey("funding_revisions.ref_id")
    )
    changeset_ref: Mapped[str | None] = mapped_column(ForeignKey("change_sets.ref_id"))
    version: Mapped[int] = mapped_column(default=1)
    review_reason: Mapped[str | None] = mapped_column(String(96))
