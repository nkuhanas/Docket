from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from docket.agent_auth import require_principal
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import AuditEvent, AuthenticatedRequest, ConversationRecord


class AgentRequestService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def admit(
        self,
        *,
        request_key: str,
        conversation_ref: str | None = None,
        source_utterance_ref: str | None = None,
    ) -> AuthenticatedRequest:
        principal = require_principal("stage")
        if not request_key or len(request_key) > 512:
            raise DocketError(
                code="invalid_request_identity", message="A bounded request identity is required."
            )
        if self.session.get_bind().dialect.name == "postgresql":
            digest = hashlib.sha256(f"{principal.principal_ref}\0{request_key}".encode()).digest()
            lock_key = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        row = self.session.scalar(
            select(AuthenticatedRequest)
            .where(
                AuthenticatedRequest.principal_ref == principal.principal_ref,
                AuthenticatedRequest.request_key == request_key,
            )
            .with_for_update()
        )
        if row is not None:
            self.require(row.ref_id, permission="stage", allow_committed=True)
            return row
        row = AuthenticatedRequest(
            principal_ref=principal.principal_ref,
            operator_ref=principal.operator_ref,
            role=principal.role,
            permissions=sorted(principal.permissions),
            request_key=request_key,
            conversation_ref=conversation_ref or request_key,
            source_utterance_ref=source_utterance_ref,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="request.authenticated",
                actor_type="agent",
                actor_id=principal.principal_ref,
                primary_ref=row.ref_id,
                affected_refs=[row.ref_id],
                basis_refs=[row.ref_id],
                data={"role": row.role, "operator_ref": row.operator_ref},
            )
        )
        return row

    def require(
        self,
        request_ref: str,
        *,
        permission: str,
        allow_committed: bool = False,
    ) -> AuthenticatedRequest:
        principal = require_principal(permission)
        row = self.session.scalar(
            select(AuthenticatedRequest)
            .where(
                AuthenticatedRequest.ref_id == request_ref,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or (
            row.principal_ref != principal.principal_ref
            or row.operator_ref != principal.operator_ref
            or row.role != "interactive"
            or permission not in row.permissions
            or row.state == "cancelled"
            or (row.state == "committed" and not allow_committed)
        ):
            raise DocketError(
                code="request_authority_denied",
                message="This request is unavailable to the authenticated caller.",
            )
        return row

    def record(
        self,
        *,
        request_ref: str,
        record_key: str,
        record_kind: str,
        text: str | None = None,
        gap_code: str | None = None,
    ) -> dict[str, object]:
        """Called in its own transaction; optional capture never wraps an action."""
        self.require(request_ref, permission="stage", allow_committed=True)
        if record_kind not in {"operator_transcript", "agent_response", "source_context"}:
            raise DocketError(
                code="invalid_record_kind", message="Unknown conversation record kind."
            )
        if not record_key or len(record_key) > 255 or (text is None) == (gap_code is None):
            raise DocketError(
                code="invalid_conversation_record", message="Supply text or a capture gap."
            )
        raw = text.encode("utf-8") if text is not None else None
        if raw is not None and len(raw) > 64 * 1024:
            raise DocketError(
                code="conversation_record_too_large", message="Conversation capture exceeds 64 KiB."
            )
        if gap_code is not None and (not gap_code or len(gap_code) > 128):
            raise DocketError(code="invalid_capture_gap", message="A bounded gap code is required.")
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        prior = self.session.scalar(
            select(ConversationRecord).where(
                ConversationRecord.request_ref == request_ref,
                ConversationRecord.record_key == record_key,
            )
        )
        if prior is not None:
            if (prior.content_hash, prior.gap_code, prior.record_kind) != (
                digest,
                gap_code,
                record_kind,
            ):
                raise DocketError(
                    code="record_idempotency_mismatch",
                    message="Append corrections with a new record identity.",
                )
            return {
                "ok": True,
                "ref": prior.ref_id,
                "capture_method": prior.capture_method,
                "replayed": True,
            }
        settings = get_settings()
        nonce = os.urandom(12) if raw is not None else None
        ciphertext = (
            AESGCM(settings.attachment_encryption_key()).encrypt(
                nonce,
                raw,
                request_ref.encode(),
            )
            if raw is not None and nonce is not None
            else None
        )
        row = ConversationRecord(
            request_ref=request_ref,
            record_key=record_key,
            record_kind=record_kind,
            capture_method="agent_reported" if raw is not None else "gap",
            content_hash=digest,
            ciphertext=ciphertext,
            nonce=nonce,
            encryption_key_ref=settings.attachment_encryption_key_ref if raw is not None else None,
            gap_code=gap_code,
        )
        self.session.add(row)
        self.session.flush()
        return {
            "ok": True,
            "ref": row.ref_id,
            "capture_method": row.capture_method,
            "replayed": False,
        }
