"""Small immutable receipts for native workflow commands without a prepared handle."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, utc_now


class NativeOutcome(Base):
    __tablename__ = "native_outcomes"
    request_ref: Mapped[str] = mapped_column(
        ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    command: Mapped[str] = mapped_column(String(128), nullable=False)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(NativeOutcome, "before_update")
@event.listens_for(NativeOutcome, "before_delete")
def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Native command outcomes are immutable")
