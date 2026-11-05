"""Purpose-bound native snapshots and exact prepared proposals, separate from triage."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class NativeContext(Base):
    __tablename__ = "native_contexts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("nctx")
    )
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PreparedCommand(Base):
    __tablename__ = "prepared_commands"
    __table_args__ = (
        UniqueConstraint("principal_ref", "operation_id", name="uq_prepared_command_operation"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("prep")
    )
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command: Mapped[str] = mapped_column(String(128), nullable=False)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_ref: Mapped[str] = mapped_column(
        ForeignKey("native_contexts.ref_id", ondelete="RESTRICT"), nullable=False
    )
    request_ref: Mapped[str] = mapped_column(
        ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"), nullable=False
    )
    changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Native context and prepared proposals are immutable")


for _model in (NativeContext, PreparedCommand):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
