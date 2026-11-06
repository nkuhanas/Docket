"""Reported delivery is independent of immutable issued content and settlement."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class FinancialDocumentObservation(Base):
    __tablename__ = "financial_document_observations"
    __table_args__ = (
        CheckConstraint("kind = 'reported_delivery'", name="ck_financial_observation_kind"),
        Index("ix_financial_observation_document", "document_ref", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("fobs")
    )
    document_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_documents.ref_id", ondelete="RESTRICT"), nullable=False
    )
    document_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_document_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="reported_delivery")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Financial delivery observations are immutable")


event.listen(FinancialDocumentObservation, "before_update", immutable)
event.listen(FinancialDocumentObservation, "before_delete", immutable)
