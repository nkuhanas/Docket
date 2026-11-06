"""Immutable receipts and ledger entries; versioned heads serialize receipt use."""

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
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_payment_version"),
        CheckConstraint("source_method IN ('report','import')", name="ck_payment_source_method"),
        CheckConstraint(
            "category IN ('unknown','wages','contract','reimbursement','other')",
            name="ck_payment_category",
        ),
        CheckConstraint(
            "(external_source IS NULL AND external_id IS NULL AND external_key_hash IS NULL) OR "
            "(external_source IS NOT NULL AND external_id IS NOT NULL "
            "AND external_key_hash IS NOT NULL)",
            name="ck_payment_external_identity",
        ),
        UniqueConstraint("operator_ref", "external_key_hash", name="uq_payment_external_identity"),
        UniqueConstraint("replaces_ref", name="uq_payment_replacement"),
        Index("ix_payment_owner_received", "operator_ref", "received_on"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("pay")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount: Mapped[str] = mapped_column(String(48), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    received_on: Mapped[date | None] = mapped_column(Date)
    engagement_ref: Mapped[str | None] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT")
    )
    payer_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    source_method: Mapped[str] = mapped_column(String(32), nullable=False, default="report")
    source_ref: Mapped[str | None] = mapped_column(
        ForeignKey("sources.ref_id", ondelete="RESTRICT")
    )
    evidence_gap: Mapped[str | None] = mapped_column(String(1000))
    external_source: Mapped[str | None] = mapped_column(String(128))
    external_id: Mapped[str | None] = mapped_column(String(512))
    external_key_hash: Mapped[str | None] = mapped_column(String(64))
    replaces_ref: Mapped[str | None] = mapped_column(
        ForeignKey("payments.ref_id", ondelete="RESTRICT")
    )
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FinancialTransaction(Base):
    __tablename__ = "financial_transactions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('receipt','allocation','credit','charge','reversal')",
            name="ck_financial_transaction_kind",
        ),
        UniqueConstraint("payment_ref", "payment_version", name="uq_financial_payment_version"),
        Index("ix_financial_transaction_owner_created", "operator_ref", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("ftxn")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_ref: Mapped[str | None] = mapped_column(
        ForeignKey("payments.ref_id", ondelete="RESTRICT")
    )
    payment_version: Mapped[int | None] = mapped_column(Integer)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    outcome_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("payment_ref", "version", name="uq_payment_event_version"),
        CheckConstraint("version >= 1", name="ck_payment_event_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_ref: Mapped[str] = mapped_column(
        ForeignKey("payments.ref_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_transactions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FinancialEntry(Base):
    __tablename__ = "financial_entries"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('receipt','cash','deduction','credit','charge','reversal')",
            name="ck_financial_entry_kind",
        ),
        CheckConstraint(
            "(kind = 'reversal') = (reverses_ref IS NOT NULL)", name="ck_financial_entry_reversal"
        ),
        Index("ix_financial_entry_owner_created", "operator_ref", "created_at"),
        Index("ix_financial_entry_payment", "payment_ref", "kind"),
        Index("ix_financial_entry_target", "target_ref", "kind"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("fent")
    )
    transaction_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_transactions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[str] = mapped_column(String(48), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_ref: Mapped[str | None] = mapped_column(
        ForeignKey("payments.ref_id", ondelete="RESTRICT")
    )
    target_ref: Mapped[str | None] = mapped_column(String(40))
    target_basis_ref: Mapped[str | None] = mapped_column(String(40))
    scopes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    matches_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    deduction_kind: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    reverses_ref: Mapped[str | None] = mapped_column(
        ForeignKey("financial_entries.ref_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FinancialReportSnapshot(Base):
    __tablename__ = "financial_report_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("frep")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Financial receipts and ledger history are immutable")


def payment_update(_mapper: object, _connection: object, target: Payment) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    prior = state.attrs.version.history.deleted
    if changed - {"version", "updated_at"} or not prior or target.version != prior[0] + 1:
        immutable(_mapper, _connection, target)


event.listen(Payment, "before_update", payment_update)
event.listen(Payment, "before_delete", immutable)
for model in (FinancialEntry, FinancialTransaction, FinancialReportSnapshot, PaymentEvent):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)
