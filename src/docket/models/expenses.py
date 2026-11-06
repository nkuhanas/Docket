"""Reported costs, separately versioned reimbursement observations and query basis."""

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


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_expense_version"),
        CheckConstraint(
            "(external_source IS NULL AND external_id IS NULL AND external_key_hash IS NULL) "
            "OR (external_source IS NOT NULL AND external_id IS NOT NULL "
            "AND external_key_hash IS NOT NULL)",
            name="ck_expense_external_identity",
        ),
        UniqueConstraint("operator_ref", "external_key_hash", name="uq_expense_external_identity"),
        Index("ix_expense_owner_updated", "operator_ref", "updated_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("exp")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retracted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_source: Mapped[str | None] = mapped_column(String(128))
    external_id: Mapped[str | None] = mapped_column(String(512))
    external_key_hash: Mapped[str | None] = mapped_column(String(64))
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExpenseRevision(Base):
    __tablename__ = "expense_revisions"
    __table_args__ = (
        UniqueConstraint("expense_ref", "revision", name="uq_expense_revision"),
        CheckConstraint("revision >= 1", name="ck_expense_revision"),
        CheckConstraint(
            "ownership IN ('unknown','operator','organization','shared')",
            name="ck_expense_ownership",
        ),
        CheckConstraint(
            "receipt_status IN ('not_provided','available','unavailable')",
            name="ck_expense_receipt_status",
        ),
        CheckConstraint(
            "source_method IN ('report','import','inference')", name="ck_expense_source_method"
        ),
        Index("ix_expense_revision_engagement_date", "engagement_ref", "incurred_on"),
        Index("ix_expense_revision_currency_date", "currency", "incurred_on"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("exrev")
    )
    expense_ref: Mapped[str] = mapped_column(
        ForeignKey("expenses.ref_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    retracted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    amount: Mapped[str] = mapped_column(String(48), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    incurred_on: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(128))
    engagement_ref: Mapped[str | None] = mapped_column(
        ForeignKey("engagements.ref_id", ondelete="RESTRICT")
    )
    organization_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    project_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    payer_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    attribution_label: Mapped[str | None] = mapped_column(String(512))
    reimbursable: Mapped[bool | None] = mapped_column(Boolean)
    reimbursable_amount: Mapped[str | None] = mapped_column(String(48))
    receipt_ref: Mapped[str | None] = mapped_column(
        ForeignKey("sources.ref_id", ondelete="RESTRICT")
    )
    receipt_status: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    receipt_note: Mapped[str | None] = mapped_column(String(1000))
    ownership: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(
        ForeignKey("entities.ref_id", ondelete="RESTRICT")
    )
    source_method: Mapped[str] = mapped_column(String(32), nullable=False)
    uncertainty: Mapped[str | None] = mapped_column(String(1000))
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExpenseObservation(Base):
    __tablename__ = "expense_observations"
    __table_args__ = (
        UniqueConstraint("expense_ref", "version", name="uq_expense_observation_version"),
        CheckConstraint(
            "status IN ('submitted','approved','rejected')", name="ck_expense_observation_status"
        ),
        CheckConstraint("version >= 1", name="ck_expense_observation_version"),
        Index("ix_expense_observation_status", "expense_ref", "status", "version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("exobs")
    )
    expense_ref: Mapped[str] = mapped_column(
        ForeignKey("expenses.ref_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("expense_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[str | None] = mapped_column(String(48))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExpenseReportSnapshot(Base):
    __tablename__ = "expense_report_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("exrep")
    )
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    totals_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExpenseReportMember(Base):
    __tablename__ = "expense_report_members"
    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("expense_report_snapshots.id", ondelete="RESTRICT"), primary_key=True
    )
    expense_ref: Mapped[str] = mapped_column(
        ForeignKey("expenses.ref_id", ondelete="RESTRICT"), primary_key=True
    )
    revision_ref: Mapped[str] = mapped_column(
        ForeignKey("expense_revisions.ref_id", ondelete="RESTRICT"), nullable=False
    )
    report_observation_ref: Mapped[str | None] = mapped_column(
        ForeignKey("expense_observations.ref_id", ondelete="RESTRICT")
    )
    approval_observation_ref: Mapped[str | None] = mapped_column(
        ForeignKey("expense_observations.ref_id", ondelete="RESTRICT")
    )


def immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Expense history and report membership are immutable")


def _head_update(_mapper: object, _connection: object, target: Expense) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    old = state.attrs.version.history.deleted
    if changed - {"version", "retracted", "updated_at"} or not old or target.version != old[0] + 1:
        immutable(_mapper, _connection, target)


def _seal(_mapper: object, _connection: object, target: ExpenseReportSnapshot) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    old = state.attrs.sealed_at.history.deleted
    if (
        changed - {"sealed_at", "totals_json"}
        or "sealed_at" not in changed
        or target.sealed_at is None
        or target.totals_json is None
        or (old and old[0] is not None)
    ):
        immutable(_mapper, _connection, target)


for history_model in (ExpenseRevision, ExpenseObservation, ExpenseReportMember):
    event.listen(history_model, "before_update", immutable)
    event.listen(history_model, "before_delete", immutable)
event.listen(Expense, "before_update", _head_update)
event.listen(Expense, "before_delete", immutable)
event.listen(ExpenseReportSnapshot, "before_update", _seal)
event.listen(ExpenseReportSnapshot, "before_delete", immutable)
