import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, TimestampMixin


class EventOccurrence(TimestampMixin, Base):
    """Canonical series child addressed by (series_ref, original_start_key)."""

    __tablename__ = "event_occurrences"
    __table_args__ = (
        UniqueConstraint("series_ref", "original_start_key", name="uq_event_occurrences_identity"),
        UniqueConstraint("replacement_event_ref", name="uq_event_occurrences_replacement"),
        CheckConstraint("status IN ('cancelled', 'replaced')", name="ck_event_occurrences_status"),
        CheckConstraint("version >= 1", name="ck_event_occurrences_version"),
        CheckConstraint(
            "status <> 'replaced' OR replacement_event_ref IS NOT NULL",
            name="ck_event_occurrences_replacement_required",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    series_ref: Mapped[str] = mapped_column(
        ForeignKey("canonical_events.ref_id", ondelete="RESTRICT"), nullable=False
    )
    original_start_key: Mapped[str] = mapped_column(String(128), nullable=False)
    original_local_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    original_timing_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    replacement_event_ref: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_events.ref_id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    basis_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    last_changeset_ref: Mapped[str] = mapped_column(
        ForeignKey("change_sets.ref_id", ondelete="RESTRICT"), nullable=False
    )


class CalendarDateBinding(TimestampMixin, Base):
    """Immutable relative-date resolution; internal, not another world primitive."""

    __tablename__ = "calendar_date_bindings"
    __table_args__ = (
        UniqueConstraint("utterance_ref", "relative_day", name="uq_calendar_date_binding"),
        CheckConstraint("relative_day IN ('today', 'tomorrow')", name="ck_calendar_relative_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    utterance_ref: Mapped[str] = mapped_column(
        ForeignKey("operator_utterances.ref_id", ondelete="RESTRICT"), nullable=False
    )
    relative_day: Mapped[str] = mapped_column(String(16), nullable=False)
    resolved_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)


@event.listens_for(CalendarDateBinding, "before_update")
@event.listens_for(CalendarDateBinding, "before_delete")
def _preserve_calendar_date(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("CalendarDateBinding is immutable")


@event.listens_for(EventOccurrence, "before_update")
def _preserve_identity(_mapper: object, _connection: object, target: EventOccurrence) -> None:
    state = inspect(target)
    if any(
        state.attrs[name].history.has_changes()
        for name in (
            "series_ref",
            "original_start_key",
            "original_local_date",
            "original_timezone",
            "identity_json",
            "original_timing_json",
        )
    ):
        raise ValueError("EventOccurrence original identity is immutable")
