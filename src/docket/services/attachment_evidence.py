from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import new_public_ref
from docket.models import AttachmentEvidence, EncryptedAttachmentBlob, OperatorUtterance, Source


@dataclass(frozen=True)
class AttachmentCapture:
    transport_attachment_ref: str
    filename: str | None
    media_type: str | None
    byte_size: int | None
    received_at: datetime
    plaintext: bytes | None = None
    ingest_error_code: str | None = None


class AttachmentEvidenceService:
    """Ledger bounded Operator attachments without treating their content as authority."""

    def __init__(
        self,
        session: Session,
        *,
        encryption_key: bytes,
        encryption_key_ref: str,
        max_attachment_bytes: int,
        max_total_bytes: int,
    ) -> None:
        if len(encryption_key) != 32:
            raise ValueError("attachment encryption key must contain exactly 32 bytes")
        self.session = session
        self.encryption_key = encryption_key
        self.encryption_key_ref = encryption_key_ref
        self.max_attachment_bytes = max_attachment_bytes
        self.max_total_bytes = max_total_bytes

    @staticmethod
    def _external_ref(source_message_ref: str, transport_attachment_ref: str) -> str:
        binding = f"{source_message_ref}\0{transport_attachment_ref}".encode()
        return f"discord_attachment:{hashlib.sha256(binding).hexdigest()}"

    @staticmethod
    def _manifest_hash(
        *,
        utterance_ref: str,
        source_message_ref: str,
        capture: AttachmentCapture,
    ) -> str:
        return sha256_json(
            {
                "operator_utterance_ref": utterance_ref,
                "source_message_ref": source_message_ref,
                "transport": "discord",
                "transport_attachment_ref": capture.transport_attachment_ref,
                "filename": capture.filename,
                "media_type": capture.media_type,
                "byte_size": capture.byte_size,
                "received_at": capture.received_at.isoformat(),
            }
        )

    def _outcome(
        self,
        capture: AttachmentCapture,
        *,
        total_plaintext_bytes: int,
    ) -> tuple[str, str, str | None]:
        if capture.ingest_error_code == "attachment_too_large":
            return "rejected", "rejected", None
        if capture.ingest_error_code is not None:
            return "failed", "metadata_only", None
        if capture.plaintext is None:
            return "pending", "pending", None
        if (
            len(capture.plaintext) > self.max_attachment_bytes
            or total_plaintext_bytes > self.max_total_bytes
        ):
            return "rejected", "rejected", None
        return (
            "available",
            "retained_encrypted",
            hashlib.sha256(capture.plaintext).hexdigest(),
        )

    @staticmethod
    def _validate_capture(capture: AttachmentCapture) -> None:
        if capture.byte_size is not None and capture.byte_size < 0:
            raise DocketError(
                code="invalid_attachment_size",
                message="Attachment byte size must not be negative.",
            )
        if (
            capture.plaintext is not None
            and capture.byte_size is not None
            and capture.byte_size != len(capture.plaintext)
        ):
            raise DocketError(
                code="attachment_size_mismatch",
                message="Attachment manifest size does not match its received bytes.",
            )

    def _blob(self, source_ref: str, plaintext: bytes) -> EncryptedAttachmentBlob:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.encryption_key).encrypt(
            nonce,
            plaintext,
            source_ref.encode("ascii"),
        )
        return EncryptedAttachmentBlob(
            attachment_source_ref=source_ref,
            encryption_key_ref=self.encryption_key_ref,
            nonce=nonce,
            ciphertext=ciphertext,
            ciphertext_hash=hashlib.sha256(ciphertext).hexdigest(),
        )

    def plan_new(
        self,
        utterance: OperatorUtterance,
        captures: list[AttachmentCapture],
    ) -> list[str]:
        if len(captures) > 10:
            raise DocketError(
                code="attachment_manifest_limit_exceeded",
                message="An Operator message may contain at most ten attachment manifests.",
            )
        total_plaintext_bytes = sum(
            len(capture.plaintext) for capture in captures if capture.plaintext is not None
        )
        refs: list[str] = []
        seen_transport_refs: set[str] = set()
        for capture in captures:
            self._validate_capture(capture)
            if capture.transport_attachment_ref in seen_transport_refs:
                raise DocketError(
                    code="duplicate_attachment_manifest",
                    message="An attachment manifest appeared more than once.",
                )
            seen_transport_refs.add(capture.transport_attachment_ref)
            existing = self.session.scalar(
                select(AttachmentEvidence).where(
                    AttachmentEvidence.transport == "discord",
                    AttachmentEvidence.transport_attachment_ref
                    == capture.transport_attachment_ref,
                    AttachmentEvidence.source_message_ref == utterance.source_message_ref,
                )
            )
            if existing is not None:
                raise IdempotencyConflict(utterance.request_key)
            source_ref = new_public_ref("src")
            ingest_state, retention, content_hash = self._outcome(
                capture,
                total_plaintext_bytes=total_plaintext_bytes,
            )
            source = Source(
                ref_id=source_ref,
                source_kind="attachment",
                external_ref=self._external_ref(
                    utterance.source_message_ref,
                    capture.transport_attachment_ref,
                ),
                observed_at=capture.received_at,
                content_hash=self._manifest_hash(
                    utterance_ref=utterance.ref_id,
                    source_message_ref=utterance.source_message_ref,
                    capture=capture,
                ),
                metadata_json={
                    "operator_utterance_ref": utterance.ref_id,
                    "transport": "discord",
                    "transport_attachment_ref": capture.transport_attachment_ref,
                    "untrusted_content": True,
                },
            )
            evidence = AttachmentEvidence(
                ref_id=source_ref,
                transport="discord",
                transport_attachment_ref=capture.transport_attachment_ref,
                source_message_ref=utterance.source_message_ref,
                operator_utterance_ref=utterance.ref_id,
                filename=capture.filename,
                media_type=capture.media_type,
                byte_size=capture.byte_size,
                content_hash=content_hash,
                received_at=capture.received_at,
                ingest_state=ingest_state,
                retention_disposition=retention,
                derived_content_refs=[],
            )
            self.session.add_all([source, evidence])
            if ingest_state == "available":
                assert capture.plaintext is not None
                self.session.add(self._blob(source_ref, capture.plaintext))
            refs.append(source_ref)
        utterance.attachment_source_refs = refs
        return refs

    def reconcile_existing(
        self,
        utterance: OperatorUtterance,
        captures: list[AttachmentCapture],
    ) -> list[str]:
        total_plaintext_bytes = sum(
            len(capture.plaintext) for capture in captures if capture.plaintext is not None
        )
        evidence_by_transport_ref = {
            evidence.transport_attachment_ref: evidence
            for evidence in self.session.scalars(
                select(AttachmentEvidence).where(
                    AttachmentEvidence.operator_utterance_ref == utterance.ref_id
                )
            )
        }
        if set(evidence_by_transport_ref) != {
            capture.transport_attachment_ref for capture in captures
        }:
            raise IdempotencyConflict(utterance.request_key)
        for capture in captures:
            self._validate_capture(capture)
            evidence = evidence_by_transport_ref[capture.transport_attachment_ref]
            if (
                evidence.source_message_ref != utterance.source_message_ref
                or evidence.filename != capture.filename
                or evidence.media_type != capture.media_type
                or evidence.byte_size != capture.byte_size
            ):
                raise IdempotencyConflict(utterance.request_key)
            ingest_state, retention, content_hash = self._outcome(
                capture,
                total_plaintext_bytes=total_plaintext_bytes,
            )
            if evidence.ingest_state == "available":
                if content_hash is not None and evidence.content_hash != content_hash:
                    raise IdempotencyConflict(utterance.request_key)
                continue
            if evidence.ingest_state in {"failed", "rejected"}:
                if evidence.ingest_state != ingest_state:
                    raise IdempotencyConflict(utterance.request_key)
                continue
            if ingest_state == "pending":
                continue
            evidence.ingest_state = ingest_state
            evidence.retention_disposition = retention
            evidence.content_hash = content_hash
            if ingest_state == "available":
                assert capture.plaintext is not None
                self.session.add(self._blob(evidence.ref_id, capture.plaintext))
        return list(utterance.attachment_source_refs)

    def summaries(self, utterance: OperatorUtterance) -> list[dict[str, object]]:
        if not utterance.attachment_source_refs:
            return []
        evidence_by_ref = {
            evidence.ref_id: evidence
            for evidence in self.session.scalars(
                select(AttachmentEvidence).where(
                    AttachmentEvidence.ref_id.in_(utterance.attachment_source_refs)
                )
            )
        }
        return [
            {
                "ref": ref,
                "ingest_state": evidence_by_ref[ref].ingest_state,
                "retention_disposition": evidence_by_ref[ref].retention_disposition,
                "content_hash": evidence_by_ref[ref].content_hash,
                "source_revision": 1,
                "untrusted_content": True,
            }
            for ref in utterance.attachment_source_refs
            if ref in evidence_by_ref
        ]

    def plaintext(self, source_ref: str) -> bytes:
        evidence = self.session.scalar(
            select(AttachmentEvidence).where(AttachmentEvidence.ref_id == source_ref)
        )
        blob = self.session.scalar(
            select(EncryptedAttachmentBlob).where(
                EncryptedAttachmentBlob.attachment_source_ref == source_ref
            )
        )
        if evidence is None or evidence.ingest_state != "available" or blob is None:
            raise DocketError(
                code="attachment_evidence_unavailable",
                message="Attachment bytes are not durably available.",
                details={"source_ref": source_ref},
            )
        if blob.encryption_key_ref != self.encryption_key_ref:
            raise DocketError(
                code="attachment_encryption_key_unavailable",
                message="The retained attachment requires a different encryption key.",
                details={"source_ref": source_ref},
            )
        if hashlib.sha256(blob.ciphertext).hexdigest() != blob.ciphertext_hash:
            raise DocketError(
                code="attachment_ciphertext_integrity_failed",
                message="Retained attachment ciphertext failed its integrity check.",
                details={"source_ref": source_ref},
            )
        plaintext = AESGCM(self.encryption_key).decrypt(
            blob.nonce,
            blob.ciphertext,
            source_ref.encode("ascii"),
        )
        if evidence.content_hash != hashlib.sha256(plaintext).hexdigest():
            raise DocketError(
                code="attachment_plaintext_integrity_failed",
                message="Retained attachment plaintext failed its integrity check.",
                details={"source_ref": source_ref},
            )
        return plaintext
