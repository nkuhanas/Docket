"""Immutable receipts recording which exact previews were applied by one batch."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, utc_now


class PreparedConsumption(Base):
    __tablename__ = "prepared_consumptions"
    member_ref: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("prepared_commands.ref_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    batch_ref: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("prepared_commands.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(PreparedConsumption, "before_update")
@event.listens_for(PreparedConsumption, "before_delete")
def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Prepared batch consumption is immutable")
