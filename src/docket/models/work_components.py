"""Immutable time composition; source revisions retain their own precision and placement."""

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base


class WorkTimeComponent(Base):
    __tablename__ = "work_time_components"
    __table_args__ = (
        CheckConstraint("ordinal >= 0 AND ordinal < 25", name="ck_work_component_order"),
        CheckConstraint("revision_ref <> source_revision_ref", name="ck_work_component_not_self"),
        UniqueConstraint("revision_ref", "source_revision_ref", name="uq_work_component_source"),
    )

    revision_ref: Mapped[str] = mapped_column(
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_revision_ref: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("work_session_revisions.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Work time composition is immutable")


event.listen(WorkTimeComponent, "before_update", _immutable)
event.listen(WorkTimeComponent, "before_delete", _immutable)
