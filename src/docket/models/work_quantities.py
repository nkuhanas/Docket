"""Stable work-unit coordinates and immutable split/merge lineage."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class WorkQuantityOrigin(Base):
    __tablename__ = "work_quantity_origins"
    __table_args__ = (CheckConstraint("next_offset_us >= 0", name="ck_work_origin_offset"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, default=lambda: new_public_ref("worg")
    )
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_work_ref: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.ref_id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    next_offset_us: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkQuantityBasis(Base):
    __tablename__ = "work_quantity_bases"
    __table_args__ = (
        CheckConstraint("status IN ('resolved', 'unknown', 'review')", name="ck_work_basis_status"),
    )

    revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )


class WorkQuantityAllocation(Base):
    __tablename__ = "work_quantity_allocations"
    __table_args__ = (
        CheckConstraint("start_us >= 0 AND end_us > start_us", name="ck_work_allocation_bounds"),
        CheckConstraint("ordinal >= 0", name="ck_work_allocation_order"),
        Index("ix_work_allocation_origin", "origin_ref", "start_us", "end_us"),
    )

    revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_quantity_bases.revision_ref", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin_ref: Mapped[str] = mapped_column(
        ForeignKey("work_quantity_origins.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class WorkLineageEdge(Base):
    __tablename__ = "work_lineage_edges"
    __table_args__ = (
        CheckConstraint("relation IN ('split', 'merge')", name="ck_work_lineage_relation"),
        UniqueConstraint("parent_revision_ref", "child_revision_ref", name="uq_work_lineage_edge"),
        Index("ix_work_lineage_child", "child_revision_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    child_revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    relation: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Work quantity and lineage history is immutable")


def _origin_growth(_mapper: object, _connection: object, target: WorkQuantityOrigin) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    prior = state.attrs.next_offset_us.history.deleted
    if changed - {"next_offset_us"} or (prior and target.next_offset_us < prior[0]):
        _immutable(_mapper, _connection, target)


def _seal_basis(_mapper: object, _connection: object, target: WorkQuantityBasis) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    prior = state.attrs.sealed.history.deleted
    if changed != {"sealed"} or not target.sealed or (prior and prior[0]):
        _immutable(_mapper, _connection, target)


event.listen(WorkQuantityOrigin, "before_update", _origin_growth)
event.listen(WorkQuantityOrigin, "before_delete", _immutable)
event.listen(WorkQuantityBasis, "before_update", _seal_basis)
event.listen(WorkQuantityBasis, "before_delete", _immutable)
for _model in (WorkQuantityAllocation, WorkLineageEdge):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
