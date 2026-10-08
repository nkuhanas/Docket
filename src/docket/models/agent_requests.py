from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from docket.domain.public_refs import new_public_ref
from docket.models.base import Base, utc_now


class AuthenticatedRequest(Base):
    """Mandatory action attribution; never a substitute human utterance."""

    __tablename__ = "authenticated_requests"
    __table_args__ = (
        UniqueConstraint("principal_ref", "request_key", name="uq_agent_requests_identity"),
        CheckConstraint("role = 'interactive'", name="ck_agent_requests_role"),
        CheckConstraint(
            "state IN ('active', 'cancelled', 'committed')", name="ck_agent_requests_state"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("req")
    )
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    request_key: Mapped[str] = mapped_column(String(512), nullable=False)
    conversation_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_utterance_ref: Mapped[str | None] = mapped_column(
        ForeignKey("operator_utterances.ref_id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    committed_changeset_ref: Mapped[str | None] = mapped_column(String(40), unique=True)
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    @property
    def actor_ref(self) -> str:
        return self.principal_ref

    @property
    def attachment_source_refs(self) -> list[str]:
        return []


class ConversationRecord(Base):
    """Optional encrypted, append-only agent-reported context or capture gap."""

    __tablename__ = "conversation_records"
    __table_args__ = (
        UniqueConstraint("request_ref", "record_key", name="uq_conversation_records_key"),
        CheckConstraint(
            "capture_method IN ('agent_reported', 'direct', 'gap')",
            name="ck_conversation_records_method",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ref_id: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=lambda: new_public_ref("rec")
    )
    request_ref: Mapped[str] = mapped_column(
        ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"), nullable=False
    )
    record_key: Mapped[str] = mapped_column(String(255), nullable=False)
    record_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    capture_method: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_key_ref: Mapped[str | None] = mapped_column(String(128))
    gap_code: Mapped[str | None] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


def _immutable_record(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("ConversationRecord is immutable")


def _guard_request(_mapper: object, _connection: object, target: AuthenticatedRequest) -> None:
    changed = {attr.key for attr in inspect(target).attrs if attr.history.has_changes()}
    if changed - {"state", "committed_changeset_ref"}:
        raise ValueError("AuthenticatedRequest attribution is immutable")
    prior = inspect(target).attrs.state.history.deleted
    if prior and prior[0] in {"committed", "cancelled"}:
        raise ValueError("A completed or cancelled request cannot regain authority")


event.listen(ConversationRecord, "before_update", _immutable_record)
event.listen(ConversationRecord, "before_delete", _immutable_record)
event.listen(AuthenticatedRequest, "before_update", _guard_request)
event.listen(AuthenticatedRequest, "before_delete", _immutable_record)
