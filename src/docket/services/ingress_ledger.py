from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import new_public_ref
from docket.models import (
    DeferredIngress,
    DiscordDailyThread,
    DrainBarrier,
    OperatorProjection,
    OperatorUtterance,
    PersistedSemanticOption,
    ProjectionDelivery,
)
from docket.security import decode_semantic_option_token, verify_semantic_option_token
from docket.services.attachment_evidence import AttachmentCapture, AttachmentEvidenceService


@dataclass(frozen=True)
class IngressIdentity:
    operator_id: str
    guild_id: str
    chat_channel_id: str
    queue_channel_id: str


def _message_ref(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"discord_message:{guild_id}:{channel_id}:{message_id}"


class IngressLedgerService:
    """Restricted append-only Discord evidence writer; never compiles mutations."""

    def __init__(
        self,
        session: Session,
        *,
        identity: IngressIdentity,
        signing_key: bytes,
        attachment_encryption_key: bytes,
        attachment_encryption_key_ref: str,
        attachment_max_bytes: int,
        attachment_total_max_bytes: int,
    ) -> None:
        self.session = session
        self.identity = identity
        self.signing_key = signing_key
        self.attachments = AttachmentEvidenceService(
            session,
            encryption_key=attachment_encryption_key,
            encryption_key_ref=attachment_encryption_key_ref,
            max_attachment_bytes=attachment_max_bytes,
            max_total_bytes=attachment_total_max_bytes,
        )

    def _validate_surface(
        self,
        *,
        actor_id: str,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
    ) -> None:
        if actor_id != self.identity.operator_id or guild_id != self.identity.guild_id:
            raise DocketError(
                code="unauthorized_ingress",
                message="Discord ingress does not identify the configured Operator.",
            )
        if channel_id == self.identity.chat_channel_id and parent_channel_id is None:
            return
        if (
            parent_channel_id == self.identity.queue_channel_id
            and channel_id != self.identity.queue_channel_id
            and self.session.scalar(
                select(DiscordDailyThread.id)
                .where(
                    DiscordDailyThread.guild_id == guild_id,
                    DiscordDailyThread.channel_id == self.identity.queue_channel_id,
                    DiscordDailyThread.thread_id == channel_id,
                    DiscordDailyThread.status.in_(("active", "archived")),
                )
                .limit(1)
            )
            is not None
        ):
            return
        raise DocketError(
            code="unauthorized_ingress",
            message="Discord ingress is not a configured Docket conversational surface.",
        )

    def capture_message(
        self,
        *,
        actor_id: str,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        message_id: str,
        reply_to_message_id: str | None,
        verbatim_text: str,
        said_at: datetime,
        attachments: list[AttachmentCapture] | None = None,
    ) -> dict[str, Any]:
        self._validate_surface(
            actor_id=actor_id,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_channel_id,
        )
        source_key = f"discord:{guild_id}:{channel_id}:{message_id}:0"
        attachment_captures = attachments or []
        existing = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.request_key == source_key)
        )
        if existing is not None:
            if (
                existing.verbatim_text != verbatim_text
                or existing.actor_ref != f"discord_user:{actor_id}"
            ):
                raise IdempotencyConflict(source_key)
            self.attachments.reconcile_existing(existing, attachment_captures)
            ingress = self._existing_or_append_ingress(
                source_key=source_key,
                ingress_kind="typed_message",
                utterance=existing,
                selected_option_binding=None,
            )
            return self._result(
                existing,
                ingress,
                replay=True,
                attachments=self.attachments.summaries(existing),
            )

        utterance = OperatorUtterance(
            ref_id=new_public_ref("utt"),
            actor_ref=f"discord_user:{actor_id}",
            transport="discord",
            source_message_ref=_message_ref(guild_id, channel_id, message_id),
            conversation_ref=f"discord_conversation:{guild_id}:{channel_id}",
            reply_to_source_ref=(
                _message_ref(guild_id, channel_id, reply_to_message_id)
                if reply_to_message_id is not None
                else None
            ),
            said_at=said_at.astimezone(UTC),
            verbatim_text=verbatim_text,
            content_hash=hashlib.sha256(verbatim_text.encode("utf-8")).hexdigest(),
            request_key=source_key,
            utterance_kind="typed_message",
        )
        try:
            with self.session.begin_nested():
                self.attachments.plan_new(utterance, attachment_captures)
                self.session.add(utterance)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(
                select(OperatorUtterance).where(OperatorUtterance.request_key == source_key)
            )
            if existing is None or existing.verbatim_text != verbatim_text:
                raise IdempotencyConflict(source_key) from None
            utterance = existing
            self.attachments.reconcile_existing(utterance, attachment_captures)
        ingress = self._existing_or_append_ingress(
            source_key=source_key,
            ingress_kind="typed_message",
            utterance=utterance,
            selected_option_binding=None,
        )
        return self._result(
            utterance,
            ingress,
            replay=False,
            attachments=self.attachments.summaries(utterance),
        )

    def capture_semantic_selection(
        self,
        *,
        actor_id: str,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        interaction_id: str,
        message_id: str,
        option_token: str,
        responded_at: datetime,
    ) -> dict[str, Any]:
        self._validate_surface(
            actor_id=actor_id,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_channel_id,
        )
        decoded = decode_semantic_option_token(option_token)
        if decoded is None or decoded.actor_id != str(int(actor_id)):
            raise DocketError(code="invalid_semantic_option_token", message="Option is invalid.")
        if responded_at.astimezone(UTC) > decoded.expires_at:
            raise DocketError(code="semantic_option_expired", message="Option has expired.")
        if not verify_semantic_option_token(
            option_token,
            reference=decoded,
            signing_key=self.signing_key,
        ):
            raise DocketError(code="invalid_semantic_option_token", message="Option is invalid.")
        option = self.session.get(PersistedSemanticOption, decoded.option_row_id)
        if option is None:
            raise DocketError(code="semantic_option_not_found", message="Option was not found.")
        projection = self.session.get(OperatorProjection, option.projection_id)
        delivery = self.session.scalar(
            select(ProjectionDelivery).where(
                ProjectionDelivery.projection_ref == option.projection_ref,
                ProjectionDelivery.transport == "discord",
                ProjectionDelivery.status == "delivered",
                ProjectionDelivery.external_message_ref
                == _message_ref(guild_id, channel_id, message_id),
            )
        )
        if (
            projection is None
            or projection.ref_id != option.projection_ref
            or delivery is None
            or sha256_json(option.authority_scope_json) != option.authority_scope_hash
            or sha256_json(option.execution_preconditions_json) != option.precondition_hash
        ):
            raise DocketError(
                code="semantic_option_binding_mismatch",
                message="Option does not match its durable Discord projection.",
            )
        source_key = f"discord:{guild_id}:{channel_id}:{interaction_id}:0"
        existing = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.discord_interaction_ref == interaction_id
            )
        )
        if existing is not None:
            if (
                existing.selected_option_ref != option.ref_id
                or existing.authority_scope_hash != option.authority_scope_hash
                or existing.selected_precondition_hash != option.precondition_hash
            ):
                raise IdempotencyConflict(source_key)
            ingress = self._existing_or_append_ingress(
                source_key=source_key,
                ingress_kind="button_selection",
                utterance=existing,
                selected_option_binding=self._selection_binding(
                    option=option,
                    option_token=option_token,
                    actor_id=actor_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    parent_channel_id=parent_channel_id,
                    interaction_id=interaction_id,
                    message_id=message_id,
                    responded_at=responded_at,
                ),
            )
            return self._result(existing, ingress, replay=True)

        utterance = OperatorUtterance(
            actor_ref=f"discord_user:{actor_id}",
            transport="discord",
            source_message_ref=f"discord_interaction:{guild_id}:{channel_id}:{interaction_id}",
            conversation_ref=f"discord_conversation:{guild_id}:{channel_id}",
            reply_to_source_ref=_message_ref(guild_id, channel_id, message_id),
            said_at=responded_at.astimezone(UTC),
            verbatim_text=option.visible_text,
            content_hash=hashlib.sha256(option.visible_text.encode("utf-8")).hexdigest(),
            request_key=source_key,
            utterance_kind="button_selection",
            selected_option_ref=option.ref_id,
            visible_choice_text=option.visible_text,
            authority_scope_hash=option.authority_scope_hash,
            selected_precondition_hash=option.precondition_hash,
            projection_ref=option.projection_ref,
            case_ref=option.case_ref,
            case_revision_ref=option.case_revision_ref,
            intent_session_ref=option.intent_session_ref,
            discord_interaction_ref=interaction_id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(utterance)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(
                select(OperatorUtterance).where(
                    OperatorUtterance.discord_interaction_ref == interaction_id
                )
            )
            if existing is None or existing.authority_scope_hash != option.authority_scope_hash:
                raise IdempotencyConflict(source_key) from None
            utterance = existing
        ingress = self._existing_or_append_ingress(
            source_key=source_key,
            ingress_kind="button_selection",
            utterance=utterance,
            selected_option_binding=self._selection_binding(
                option=option,
                option_token=option_token,
                actor_id=actor_id,
                guild_id=guild_id,
                channel_id=channel_id,
                parent_channel_id=parent_channel_id,
                interaction_id=interaction_id,
                message_id=message_id,
                responded_at=responded_at,
            ),
        )
        return self._result(utterance, ingress, replay=False)

    def _existing_or_append_ingress(
        self,
        *,
        source_key: str,
        ingress_kind: str,
        utterance: OperatorUtterance,
        selected_option_binding: dict[str, Any] | None,
    ) -> DeferredIngress:
        existing = self.session.scalar(
            select(DeferredIngress).where(DeferredIngress.source_key == source_key)
        )
        if existing is not None:
            if existing.utterance_ref != utterance.ref_id or existing.ingress_kind != ingress_kind:
                raise IdempotencyConflict(source_key)
            return existing
        barrier = self.session.scalar(
            select(DrainBarrier)
            .where(DrainBarrier.status.in_(("requested", "draining")))
            .order_by(DrainBarrier.requested_at.desc())
            .limit(1)
        )
        ingress = DeferredIngress(
            source_key=source_key,
            ingress_kind=ingress_kind,
            utterance_ref=utterance.ref_id,
            selected_option_binding_json=selected_option_binding,
            drain_ref=barrier.ref_id if barrier is not None else None,
            status="pending",
        )
        try:
            with self.session.begin_nested():
                self.session.add(ingress)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(
                select(DeferredIngress).where(DeferredIngress.source_key == source_key)
            )
            if existing is None or existing.utterance_ref != utterance.ref_id:
                raise IdempotencyConflict(source_key) from None
            ingress = existing
        return ingress

    @staticmethod
    def _selection_binding(
        *,
        option: PersistedSemanticOption,
        option_token: str,
        actor_id: str,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        interaction_id: str,
        message_id: str,
        responded_at: datetime,
    ) -> dict[str, Any]:
        return {
            "option_token": option_token,
            "discord_user_id": actor_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "parent_channel_id": parent_channel_id,
            "discord_interaction_id": interaction_id,
            "message_id": message_id,
            "responded_at": responded_at.astimezone(UTC).isoformat(),
            "selected_option_ref": option.ref_id,
            "projection_ref": option.projection_ref,
            "option_id": option.option_id,
            "authority_scope_hash": option.authority_scope_hash,
            "precondition_hash": option.precondition_hash,
        }

    @staticmethod
    def _result(
        utterance: OperatorUtterance,
        ingress: DeferredIngress,
        *,
        replay: bool,
        attachments: list[dict[str, object]] | None = None,
    ) -> dict[str, Any]:
        result = {
            "ok": True,
            "utterance_ref": utterance.ref_id,
            "deferred_ingress_ref": ingress.ref_id,
            "state": ingress.status,
            "disposition": "replayed_request" if replay else "stored",
        }
        if attachments is not None:
            result["attachments"] = attachments
        return result
