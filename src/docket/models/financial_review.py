"""Append-only review facts. Each correction names exactly one preceding revision."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, utc_now
from docket.models.settlement import immutable


class FinancialReviewFact(Base):
    __tablename__ = "financial_review_facts"
    __table_args__ = (
        UniqueConstraint("operator_ref", "kind", "identity_key", "revision"),
        Index("ix_financial_review_owner_kind", "operator_ref", "kind"),
    )
    ref_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    operator_ref: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(40))
    identity_key: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int]
    prior_ref: Mapped[str | None] = mapped_column(
        ForeignKey("financial_review_facts.ref_id"), unique=True
    )
    fields: Mapped[dict[str, Any]] = mapped_column(JSON)
    changeset_ref: Mapped[str] = mapped_column(ForeignKey("change_sets.ref_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


event.listen(FinancialReviewFact, "before_update", immutable)
event.listen(FinancialReviewFact, "before_delete", immutable)
