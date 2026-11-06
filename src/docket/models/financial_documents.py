"""Formal work reports and shared work-quantity claims, with retained transitions."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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


class FinancialDocument(Base):
    __tablename__ = "financial_documents"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_financial_document_version"),
        CheckConstraint("kind IN ('timesheet', 'invoice')", name="ck_financial_document_kind"),
        CheckConstraint(
            "status IN ('draft', 'verified', 'submitted', 'approved', 'rejected', "
            "'issued', 'cancelled')",
            name="ck_financial_document_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("fdoc")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    engagement_ref: Mapped[str] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT"), nullable=False
    )
    component_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FinancialDocumentRevision(Base):
    __tablename__ = "financial_document_revisions"
    __table_args__ = (
        UniqueConstraint("document_ref", "revision", name="uq_financial_document_revision"),
        CheckConstraint("revision >= 1", name="ck_financial_document_revision"),
        CheckConstraint("date_to > date_from", name="ck_financial_document_period"),
        CheckConstraint(
            "status IN ('draft', 'verified', 'submitted', 'approved', 'rejected', "
            "'issued', 'cancelled')",
            name="ck_financial_revision_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("fdrev")
    )
    document_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_documents.ref_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_ref: Mapped[str] = mapped_column(
        ForeignKey("compensation_evaluations.ref_id", ondelete="RESTRICT"), nullable=False
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observation: Mapped[str | None] = mapped_column(Text)
    observation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    amends_ref: Mapped[str | None] = mapped_column(
        ForeignKey("financial_documents.ref_id", ondelete="RESTRICT")
    )
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkClaim(Base):
    """One economic component over stable raw work units, shared by later documents.

    Engagement is attribution, not a uniqueness escape hatch: reattributing a
    corrected session cannot make previously claimed base hours fresh again.
    """

    __tablename__ = "work_claims"
    __table_args__ = (
        CheckConstraint("start_us >= 0 AND end_us > start_us", name="ck_work_claim_bounds"),
        CheckConstraint("version >= 1", name="ck_work_claim_version"),
        CheckConstraint("evaluation_line >= 0", name="ck_work_claim_evaluation_line"),
        CheckConstraint(
            "reportable_us IS NULL OR reportable_us >= 0", name="ck_work_claim_reportable"
        ),
        CheckConstraint(
            "status IN ('reserved', 'reported', 'issued', 'released')", name="ck_work_claim_status"
        ),
        CheckConstraint(
            "(gross_numerator IS NULL) = (gross_denominator IS NULL)",
            name="ck_work_claim_money_pair",
        ),
        Index("ix_work_claim_availability", "origin_ref", "component_key", "start_us", "end_us"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("clm")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_ref: Mapped[str] = mapped_column(
        ForeignKey("work_quantity_origins.ref_id", ondelete="RESTRICT"), nullable=False
    )
    component_key: Mapped[str] = mapped_column(String(64), nullable=False)
    engagement_ref: Mapped[str] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT"), nullable=False
    )
    start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    evaluation_ref: Mapped[str] = mapped_column(
        ForeignKey("compensation_evaluations.ref_id", ondelete="RESTRICT"), nullable=False
    )
    evaluation_line: Mapped[int] = mapped_column(Integer, nullable=False)
    document_ref: Mapped[str] = mapped_column(
        ForeignKey("financial_documents.ref_id", ondelete="RESTRICT"), nullable=False
    )
    reportable_us: Mapped[int | None] = mapped_column(BigInteger)
    pay_term_ref: Mapped[str | None] = mapped_column(
        ForeignKey("compensation_terms.ref_id", ondelete="RESTRICT")
    )
    gross_numerator: Mapped[str | None] = mapped_column(String(128))
    gross_denominator: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkClaimEvent(Base):
    __tablename__ = "work_claim_events"
    __table_args__ = (
        UniqueConstraint("claim_ref", "version", name="uq_work_claim_event"),
        CheckConstraint("version >= 1", name="ck_work_claim_event_version"),
        CheckConstraint(
            "status IN ('reserved', 'reported', 'issued', 'released')",
            name="ck_work_claim_event_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    claim_ref: Mapped[str] = mapped_column(
        ForeignKey("work_claims.ref_id", ondelete="RESTRICT"), nullable=False
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


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Financial history and claim quantities are immutable")


def _head_update(_mapper: object, _connection: object, target: Any) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    allowed = (
        {"version", "status", "updated_at"}
        if isinstance(target, FinancialDocument)
        else {"version", "status"}
    )
    old = state.attrs.version.history.deleted
    if changed - allowed or "version" not in changed or not old or target.version != old[0] + 1:
        _immutable(_mapper, _connection, target)


for model in (FinancialDocumentRevision, WorkClaimEvent):
    event.listen(model, "before_update", _immutable)
    event.listen(model, "before_delete", _immutable)
for head_model in (FinancialDocument, WorkClaim):
    event.listen(head_model, "before_update", _head_update)
    event.listen(head_model, "before_delete", _immutable)
