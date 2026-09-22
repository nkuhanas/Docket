"""Immutable reply context, separate from the verbatim utterance and its authority."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, event
from sqlalchemy.orm import Mapped, mapped_column

from docket.models.base import Base, utc_now


class ClarificationReply(Base):
    __tablename__ = "clarification_replies"

    utterance_ref: Mapped[str] = mapped_column(
        ForeignKey("operator_utterances.ref_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    projection_ref: Mapped[str] = mapped_column(
        ForeignKey("operator_projections.ref_id", ondelete="RESTRICT"),
        nullable=False,
    )
    intent_session_ref: Mapped[str] = mapped_column(
        ForeignKey("intent_sessions.ref_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    selected_option_ref: Mapped[str | None] = mapped_column(
        ForeignKey("persisted_semantic_options.ref_id", ondelete="RESTRICT"),
    )
    evidence_utterance_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("ClarificationReply is immutable")


event.listen(ClarificationReply, "before_update", _immutable)
event.listen(ClarificationReply, "before_delete", _immutable)
