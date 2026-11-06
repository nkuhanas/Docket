"""Document-purpose allocations over the shared work claim and expense sources."""

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
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class DocumentAllocation(Base):
    __tablename__ = "document_allocations"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_document_allocation_version"),
        CheckConstraint(
            "status IN ('reserved','issued','released')", name="ck_document_allocation_status"
        ),
        CheckConstraint(
            "(work_claim_ref IS NOT NULL AND expense_ref IS NULL "
            "AND expense_revision_ref IS NULL) OR "
            "(work_claim_ref IS NULL AND expense_ref IS NOT NULL "
            "AND expense_revision_ref IS NOT NULL)",
            name="ck_document_allocation_source",
        ),
        CheckConstraint(
            "(gross_numerator IS NULL) = (gross_denominator IS NULL)",
            name="ck_document_allocation_money",
        ),
        CheckConstraint("money_decimals BETWEEN 0 AND 9", name="ck_document_allocation_precision"),
        Index("ix_document_allocation_source", "operator_ref", "source_key", "status"),
        Index("ix_document_allocation_document", "document_ref", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("alloc")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    document_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_documents.ref_id", ondelete="RESTRICT"), nullable=False
    )
    work_claim_ref: Mapped[str | None] = mapped_column(
        ForeignKey("work_claims.ref_id", ondelete="RESTRICT")
    )
    expense_ref: Mapped[str | None] = mapped_column(
        ForeignKey("expenses.ref_id", ondelete="RESTRICT")
    )
    expense_revision_ref: Mapped[str | None] = mapped_column(
        ForeignKey("expense_revisions.ref_id", ondelete="RESTRICT")
    )
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    start_units: Mapped[str] = mapped_column(String(48), nullable=False)
    end_units: Mapped[str] = mapped_column(String(48), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    money_decimals: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    gross_numerator: Mapped[str | None] = mapped_column(String(160))
    gross_denominator: Mapped[str | None] = mapped_column(String(160))
    quote_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DocumentAllocationEvent(Base):
    __tablename__ = "document_allocation_events"
    __table_args__ = (
        UniqueConstraint("allocation_ref", "version", name="uq_document_allocation_event"),
        CheckConstraint("version >= 1", name="ck_document_allocation_event_version"),
        CheckConstraint(
            "status IN ('reserved','issued','released')", name="ck_document_allocation_event_status"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    allocation_ref: Mapped[str] = mapped_column(
        ForeignKey("document_allocations.ref_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    document_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_document_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InvoiceNumberSequence(Base):
    __tablename__ = "invoice_number_sequences"
    __table_args__ = (CheckConstraint("next_ordinal >= 1", name="ck_invoice_sequence_ordinal"),)
    operator_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    series: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class InvoiceNumber(Base):
    __tablename__ = "invoice_numbers"
    __table_args__ = (
        UniqueConstraint("operator_ref", "number", name="uq_invoice_number"),
        UniqueConstraint("operator_ref", "series", "ordinal", name="uq_invoice_ordinal"),
        CheckConstraint("ordinal >= 1", name="ck_invoice_ordinal"),
    )
    document_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_documents.ref_id", ondelete="RESTRICT"), primary_key=True
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    series: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[str] = mapped_column(String(100), nullable=False)
    issued_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_document_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Document allocation and numbering history is immutable")


def transition(_mapper: object, _connection: object, target: DocumentAllocation) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    old_version = state.attrs.version.history.deleted
    old_status = state.attrs.status.history.deleted
    if (
        changed - {"status", "version"}
        or not old_version
        or not old_status
        or target.version != old_version[0] + 1
        or old_status[0] != "reserved"
        or target.status not in {"issued", "released"}
    ):
        immutable(_mapper, _connection, target)


event.listen(DocumentAllocation, "before_update", transition)
event.listen(DocumentAllocation, "before_delete", immutable)
for model in (DocumentAllocationEvent, InvoiceNumber):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)
