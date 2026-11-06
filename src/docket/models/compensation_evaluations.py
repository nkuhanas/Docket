"""Immutable owner-bound derived calculations, independent of canonical claims."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class CompensationEvaluation(Base):
    __tablename__ = "compensation_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("ceval"), nullable=False
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    work_report_ref: Mapped[str] = mapped_column(
        ForeignKey("work_report_snapshots.ref_id", ondelete="RESTRICT"), nullable=False
    )
    calculation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Retained compensation calculations are immutable")


event.listen(CompensationEvaluation, "before_update", _immutable)
event.listen(CompensationEvaluation, "before_delete", _immutable)
