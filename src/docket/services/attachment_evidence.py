from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pypdf import PdfReader
from pypdf import __version__ as pypdf_version
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import new_public_ref
from docket.models import AttachmentEvidence, EncryptedAttachmentBlob, OperatorUtterance, Source

PDF_TEXT_EXTRACTOR = "docket.pypdf.text"
PDF_TEXT_OUTPUT_BUDGET = 16 * 1024
PDF_TEXT_MAX_PAGES = 200


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
                # The immutable manifest already reached a terminal retention outcome.
                # A concurrent capture path may have observed different byte availability,
                # but it cannot redefine or enrich terminal evidence. Preserve the first
                # durable outcome and treat the matching manifest as an idempotent replay.
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


def _pdf_cursor(cursor: str | None) -> tuple[int, int]:
    if cursor is None:
        return 0, 0
    parts = cursor.split(":", maxsplit=1)
    if len(parts) != 2:
        raise DocketError(code="invalid_cursor", message="Invalid attachment-text cursor.")
    try:
        page_index, character_offset = (int(part) for part in parts)
    except ValueError as exc:
        raise DocketError(
            code="invalid_cursor", message="Invalid attachment-text cursor."
        ) from exc
    if page_index < 0 or character_offset < 0:
        raise DocketError(code="invalid_cursor", message="Invalid attachment-text cursor.")
    return page_index, character_offset


def _slice_utf8(value: str, start: int, byte_limit: int) -> tuple[str, int]:
    remaining = value[start:]
    if len(remaining.encode("utf-8")) <= byte_limit:
        return remaining, len(value)
    low = 0
    high = len(remaining)
    while low < high:
        midpoint = (low + high + 1) // 2
        if len(remaining[:midpoint].encode("utf-8")) <= byte_limit:
            low = midpoint
        else:
            high = midpoint - 1
    return remaining[:low], start + low


def _normalized_pdf_text(reader: PdfReader, page_index: int) -> str:
    text = reader.pages[page_index].extract_text(extraction_mode="layout") or ""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


class AttachmentTextService:
    """Return bounded, provenance-addressable text from retained Operator evidence."""

    def __init__(self, attachment_service: AttachmentEvidenceService) -> None:
        self.attachments = attachment_service

    @staticmethod
    def _serialized_bytes(value: dict[str, object]) -> int:
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    def read_pdf_text(
        self,
        *,
        source_ref: str,
        cursor: str | None,
        max_text_bytes: int,
        page_limit: int,
    ) -> dict[str, object]:
        evidence = self.attachments.session.scalar(
            select(AttachmentEvidence).where(AttachmentEvidence.ref_id == source_ref)
        )
        if evidence is None:
            raise DocketError(code="not_found", message="Attachment source was not found.")
        media_type = (evidence.media_type or "").casefold()
        filename = (evidence.filename or "").casefold()
        if media_type != "application/pdf" and not filename.endswith(".pdf"):
            raise DocketError(
                code="unsupported_attachment_media_type",
                message="Bounded text extraction currently supports PDF attachments only.",
                details={"source_ref": source_ref, "media_type": evidence.media_type},
            )
        plaintext = self.attachments.plaintext(source_ref)
        try:
            reader = PdfReader(BytesIO(plaintext), strict=False)
        except (PdfReadError, ValueError, OSError) as exc:
            raise DocketError(
                code="attachment_pdf_invalid",
                message="The retained attachment is not a readable PDF.",
                details={"source_ref": source_ref},
            ) from exc
        if reader.is_encrypted:
            raise DocketError(
                code="attachment_pdf_encrypted",
                message="The retained PDF requires a password and cannot be extracted.",
                details={"source_ref": source_ref},
            )
        page_count = len(reader.pages)
        if page_count > PDF_TEXT_MAX_PAGES:
            raise DocketError(
                code="attachment_pdf_page_limit_exceeded",
                message="The retained PDF exceeds the bounded extraction page limit.",
                details={"source_ref": source_ref, "page_count": page_count},
            )
        page_index, character_offset = _pdf_cursor(cursor)
        if page_index > page_count or (page_index == page_count and character_offset != 0):
            raise DocketError(code="invalid_cursor", message="Invalid attachment-text cursor.")

        items: list[dict[str, object]] = []
        remaining_bytes = max_text_bytes
        next_cursor: str | None = None
        while page_index < page_count and len(items) < page_limit and remaining_bytes > 0:
            next_cursor = None
            page_text = _normalized_pdf_text(reader, page_index)
            if character_offset > len(page_text):
                raise DocketError(code="invalid_cursor", message="Invalid attachment-text cursor.")
            if not page_text[character_offset:]:
                page_index += 1
                character_offset = 0
                continue
            fragment, end = _slice_utf8(page_text, character_offset, remaining_bytes)
            if not fragment:
                break
            items.append(
                {
                    "source_fragment_locator": {
                        "page": page_index + 1,
                        "text_character_start": character_offset,
                        "text_character_end": end,
                    },
                    "source_fragment_hash": hashlib.sha256(
                        fragment.encode("utf-8")
                    ).hexdigest(),
                    "text": fragment,
                }
            )
            remaining_bytes -= len(fragment.encode("utf-8"))
            if end < len(page_text):
                next_cursor = f"{page_index}:{end}"
                break
            page_index += 1
            character_offset = 0
            if page_index < page_count:
                next_cursor = f"{page_index}:0"

        # Distinguish a scanned/image-only document from an empty requested page.
        if (
            not items
            and cursor is None
            and all(
                not _normalized_pdf_text(reader, index).strip()
                for index in range(page_count)
            )
        ):
            raise DocketError(
                code="attachment_pdf_no_text_layer",
                message=(
                    "The retained PDF has no extractable text layer; OCR is not currently "
                    "available."
                ),
                details={"source_ref": source_ref, "page_count": page_count},
            )
        result: dict[str, object] = {
            "ok": True,
            "source_ref": source_ref,
            "source_revision": 1,
            "content_hash": evidence.content_hash,
            "media_type": evidence.media_type or "application/pdf",
            "untrusted_content": True,
            "extractor_identifier": PDF_TEXT_EXTRACTOR,
            "extractor_version": pypdf_version,
            "items": items,
            "count": len(items),
            "page_count": page_count,
            "truncated": next_cursor is not None,
            "cursor": next_cursor,
        }
        if next_cursor is not None:
            result["next"] = {"cursor": next_cursor}
        if self._serialized_bytes(result) > PDF_TEXT_OUTPUT_BUDGET:
            raise RuntimeError("bounded PDF extraction exceeded its output contract")
        return result
