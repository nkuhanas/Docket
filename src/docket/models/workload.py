"""Retained operational reminders and source-bound reconciliation receipts."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class OperationalReminder(Base):
    __tablename__ = "operational_reminders"
    reminder_ref: Mapped[str] = mapped_column(
        ForeignKey("reminder_plans.ref_id", ondelete="RESTRICT"), primary_key=True
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    label: Mapped[str | None] = mapped_column(String(512))


class WorkloadReconciliation(Base):
    __tablename__ = "workload_reconciliations"
    __table_args__ = (Index("ix_workload_reconciliation_owner", "operator_ref", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("wrec"), nullable=False
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    case_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def retain(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Operational snapshots are retained; retract the reminder or reconcile anew")


for model in (OperationalReminder, WorkloadReconciliation):
    event.listen(model, "before_update", retain)
    event.listen(model, "before_delete", retain)
