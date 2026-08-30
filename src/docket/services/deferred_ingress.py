from __future__ import annotations

import base64
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.models import (
    AttachmentEvidence,
    DeferredIngress,
    DiscordDailyThread,
    DrainBarrier,
    OperatorUtterance,
)
from docket.providers.discord import DiscordProjectionAdapter
from docket.services.attachment_evidence import AttachmentEvidenceService


def _source_parts(source_ref: str, *, prefix: str) -> tuple[str, str, str] | None:
    parts = source_ref.split(":")
    if len(parts) != 4 or parts[0] != prefix:
        return None
    return parts[1], parts[2], parts[3]


class DeferredIngressRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: DiscordProjectionAdapter,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter

    def run_once(self) -> bool:
        with self.session_factory.begin() as session:
            if session.scalar(
                select(DrainBarrier.id)
                .where(DrainBarrier.status.in_(("requested", "draining")))
                .limit(1)
            ) is not None:
                return False
            ingress = session.scalar(
                select(DeferredIngress)
                .where(DeferredIngress.status == "pending")
                .order_by(DeferredIngress.created_at, DeferredIngress.ref_id)
                .limit(1)
            )
            if ingress is None:
                return False
            utterance = session.scalar(
                select(OperatorUtterance).where(
                    OperatorUtterance.ref_id == ingress.utterance_ref
                )
            )
            if utterance is None:
                ingress.status = "rejected"
                ingress.last_error_code = "operator_utterance_not_found"
                return True
            attachment_evidence = list(
                session.scalars(
                    select(AttachmentEvidence).where(
                        AttachmentEvidence.ref_id.in_(utterance.attachment_source_refs)
                    )
                )
            )
            if len(attachment_evidence) != len(utterance.attachment_source_refs):
                ingress.status = "rejected"
                ingress.last_error_code = "attachment_evidence_missing"
                return True
            if any(evidence.ingest_state == "pending" for evidence in attachment_evidence):
                return False
            settings = get_settings()
            attachment_service = AttachmentEvidenceService(
                session,
                encryption_key=settings.attachment_encryption_key(),
                encryption_key_ref=settings.attachment_encryption_key_ref,
                max_attachment_bytes=settings.attachment_max_bytes,
                max_total_bytes=settings.attachment_total_max_bytes,
            )
            payload = self._payload(
                session,
                ingress,
                utterance,
                attachment_evidence=attachment_evidence,
                attachment_service=attachment_service,
            )
        self.adapter.post_deferred_ingress(payload)
        return True

    @staticmethod
    def _payload(
        session: Session,
        ingress: DeferredIngress,
        utterance: OperatorUtterance,
        *,
        attachment_evidence: list[AttachmentEvidence],
        attachment_service: AttachmentEvidenceService,
    ) -> dict[str, Any]:
        source_prefix = (
            "discord_message" if ingress.ingress_kind == "typed_message" else "discord_interaction"
        )
        source = _source_parts(utterance.source_message_ref, prefix=source_prefix)
        if source is None:
            raise RuntimeError("deferred ingress has an invalid Discord source binding")
        guild_id, channel_id, source_id = source
        parent_channel_id = session.scalar(
            select(DiscordDailyThread.channel_id).where(
                DiscordDailyThread.guild_id == guild_id,
                DiscordDailyThread.thread_id == channel_id,
            )
        )
        reply_to_message_id: str | None = None
        if utterance.reply_to_source_ref is not None:
            reply = _source_parts(utterance.reply_to_source_ref, prefix="discord_message")
            if reply is not None:
                reply_to_message_id = reply[2]
        attachments: list[dict[str, Any]] = []
        evidence_by_ref = {evidence.ref_id: evidence for evidence in attachment_evidence}
        for source_ref in utterance.attachment_source_refs:
            evidence = evidence_by_ref.get(source_ref)
            if evidence is None:
                continue
            attachment: dict[str, Any] = {
                "ref": source_ref,
                "transport_attachment_ref": evidence.transport_attachment_ref,
                "filename": evidence.filename,
                "media_type": evidence.media_type,
                "byte_size": evidence.byte_size,
                "content_hash": evidence.content_hash,
                "ingest_state": evidence.ingest_state,
                "retention_disposition": evidence.retention_disposition,
                "source_revision": 1,
                "untrusted_content": True,
            }
            if evidence.ingest_state == "available":
                attachment["plaintext_base64"] = base64.b64encode(
                    attachment_service.plaintext(source_ref)
                ).decode("ascii")
            attachments.append(attachment)
        return {
            "request_id": str(uuid.uuid4()),
            "deferred_ingress_ref": ingress.ref_id,
            "ingress_kind": ingress.ingress_kind,
            "utterance_ref": utterance.ref_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "parent_channel_id": parent_channel_id,
            "source_id": source_id,
            "reply_to_message_id": reply_to_message_id,
            "verbatim_text": utterance.verbatim_text,
            "selected_option_binding": ingress.selected_option_binding_json,
            "attachment_evidence": attachments,
        }
