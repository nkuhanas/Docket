from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.production_reset import (
    TRACKED_CONTEXT_DOCUMENT_REF,
    TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH,
    ProductionResetAuthorityBinding,
    production_reset_authorization_text,
)
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import (
    AgentResponseCapture,
    AgentResponseDeliveryUpdate,
    AgentTurnNoResponse,
    OperatorUtteranceCapture,
    ProductionResetAuthorizationCapture,
    SpecificationSignoffCapture,
)
from docket.models import (
    AgentResponse,
    AuditEvent,
    Decision,
    DeferredIngress,
    DiscordDailyThread,
    IntentSession,
    IntentTurn,
    OperatorProjection,
    OperatorUtterance,
    ProjectionDelivery,
    SemanticRequest,
    ToolInvocation,
)
from docket.models.base import utc_now
from docket.schemas.authority import IntentTurnFinalize
from docket.services.attachment_evidence import AttachmentCapture, AttachmentEvidenceService
from docket.services.continuity import ContinuityService
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.intent_sessions import IntentSessionService
from docket.services.reply_bindings import ReplyBindingService
from docket.specification_artifacts import specification_artifact

FROZEN_DOCUMENT_REF = "ONT-DELTA-2026-08-27"
FROZEN_ARTIFACT_HASH = "3d744f4d021f8a605086152eb76743a7ec5a7ed2c8754694e38c1a891a14b5e1"
FINAL_ARCHITECTURE_SIGNOFF_TEXT = (
    "I explicitly sign off on Docket architecture "
    f"{FROZEN_DOCUMENT_REF} at SHA-256 `{FROZEN_ARTIFACT_HASH}`."
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(serialized)


def _discord_said_at(message_id: str) -> datetime:
    timestamp_ms = (int(message_id) >> 22) + 1_420_070_400_000
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


def _conversation_ref(guild_id: str, channel_id: str) -> str:
    return f"discord_conversation:{guild_id}:{channel_id}"


def _message_ref(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"discord_message:{guild_id}:{channel_id}:{message_id}"


def _interaction_ref(guild_id: str, channel_id: str, interaction_id: str) -> str:
    return f"discord_interaction:{guild_id}:{channel_id}:{interaction_id}"


def _response_source_ref(
    utterance: OperatorUtterance,
    *,
    guild_id: str,
    channel_id: str,
    source_message_id: str,
) -> str:
    if utterance.utterance_kind in {"button_selection", "select_selection"}:
        return _interaction_ref(guild_id, channel_id, source_message_id)
    return _message_ref(guild_id, channel_id, source_message_id)


def _actor_ref(actor_id: str) -> str:
    return f"discord_user:{actor_id}"


class ProvenanceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _validate_discord_surface(
        self,
        *,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        actor_id: str,
        allow_queue_root: bool = False,
    ) -> None:
        settings = get_settings()
        if guild_id != settings.discord_guild_id or actor_id != settings.operator_discord_user_id:
            raise DocketError(
                code="invalid_provenance_context",
                message="Discord provenance does not identify the configured Operator.",
            )
        if channel_id == settings.chat_channel_id and parent_channel_id is None:
            return
        if (
            allow_queue_root
            and channel_id == settings.queue_channel_id
            and parent_channel_id is None
        ):
            return
        if (
            parent_channel_id == settings.queue_channel_id
            and channel_id != settings.queue_channel_id
            and self.session.scalar(
                select(DiscordDailyThread.id)
                .where(
                    DiscordDailyThread.guild_id == settings.discord_guild_id,
                    DiscordDailyThread.channel_id == settings.queue_channel_id,
                    DiscordDailyThread.thread_id == channel_id,
                    DiscordDailyThread.status.in_(("active", "archived")),
                )
                .limit(1)
            )
            is not None
        ):
            return
        raise DocketError(
            code="invalid_provenance_context",
            message=(
                "Discord provenance is not bound to the configured Docket chat, queue, "
                "or a Docket-owned daily thread."
            ),
        )

    @staticmethod
    def _same_utterance(existing: OperatorUtterance, request: OperatorUtteranceCapture) -> bool:
        return (
            existing.actor_ref == _actor_ref(request.actor_id)
            and existing.source_message_ref
            == _message_ref(request.guild_id, request.channel_id, request.message_id)
            and existing.conversation_ref == _conversation_ref(request.guild_id, request.channel_id)
            and existing.reply_to_source_ref
            == (
                _message_ref(
                    request.guild_id,
                    request.channel_id,
                    request.reply_to_message_id,
                )
                if request.reply_to_message_id is not None
                else None
            )
            and existing.verbatim_text == request.verbatim_text
            and existing.content_hash == _sha256_text(request.verbatim_text)
        )

    @staticmethod
    def _attachment_captures(request: OperatorUtteranceCapture) -> list[AttachmentCapture]:
        captures: list[AttachmentCapture] = []
        for manifest in request.attachments:
            plaintext: bytes | None = None
            if manifest.plaintext_base64 is not None:
                try:
                    plaintext = base64.b64decode(manifest.plaintext_base64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise DocketError(
                        code="invalid_attachment_encoding",
                        message="Attachment plaintext is not valid canonical base64.",
                    ) from exc
                if manifest.byte_size is not None and manifest.byte_size != len(plaintext):
                    raise DocketError(
                        code="attachment_size_mismatch",
                        message="Attachment manifest size does not match its received bytes.",
                    )
            captures.append(
                AttachmentCapture(
                    transport_attachment_ref=manifest.transport_attachment_ref,
                    filename=manifest.filename,
                    media_type=manifest.media_type,
                    byte_size=manifest.byte_size,
                    received_at=manifest.received_at,
                    plaintext=plaintext,
                    ingest_error_code=manifest.ingest_error_code,
                )
            )
        return captures

    def _attachment_service(self) -> AttachmentEvidenceService:
        settings = get_settings()
        return AttachmentEvidenceService(
            self.session,
            encryption_key=settings.attachment_encryption_key(),
            encryption_key_ref=settings.attachment_encryption_key_ref,
            max_attachment_bytes=settings.attachment_max_bytes,
            max_total_bytes=settings.attachment_total_max_bytes,
        )

    def _ensure_utterance_audit(
        self,
        utterance: OperatorUtterance,
        request: OperatorUtteranceCapture,
    ) -> None:
        audit_ref = f"aud_{utterance.ref_id.removeprefix('utt_')}"
        if self.session.scalar(
            select(AuditEvent.id).where(
                (AuditEvent.ref_id == audit_ref)
                | (
                    (AuditEvent.event_type == "operator_utterance.recorded")
                    & (AuditEvent.primary_ref == utterance.ref_id)
                )
            )
        ) is not None:
            return
        self.session.add(
            AuditEvent(
                ref_id=audit_ref,
                event_type="operator_utterance.recorded",
                entity_type="operator_utterance",
                entity_id=utterance.id,
                actor_type="operator",
                actor_id=request.actor_id,
                request_id=request.request_id,
                primary_ref=utterance.ref_id,
                affected_refs=[utterance.ref_id],
                basis_refs=[utterance.ref_id],
                data={
                    "transport": "discord",
                    "conversation_ref": utterance.conversation_ref,
                    "content_hash": utterance.content_hash,
                    "attachment_source_refs": list(utterance.attachment_source_refs),
                },
            )
        )

    def capture_operator_utterance(
        self,
        request: OperatorUtteranceCapture,
    ) -> dict[str, Any]:
        self._validate_discord_surface(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            parent_channel_id=request.parent_channel_id,
            actor_id=request.actor_id,
            allow_queue_root=True,
        )
        if request.gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(request.gateway_instance_ref)
        existing = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.request_key == request.request_key)
        )
        captures = self._attachment_captures(request)
        attachment_service = self._attachment_service()
        if existing is not None:
            if not self._same_utterance(existing, request):
                raise IdempotencyConflict(request.request_key)
            attachment_service.reconcile_existing(existing, captures)
            self._ensure_utterance_audit(existing, request)
            attachment_summaries = attachment_service.summaries(existing)
            replay_result: dict[str, Any] = {
                "ok": True,
                "ref": existing.ref_id,
                "state": "recorded",
                "content_hash": existing.content_hash,
                "disposition": "replayed_request",
                "reply_binding": ReplyBindingService(self.session).resolve(existing),
                "attachments": attachment_summaries,
            }
            ingress = self._capture_or_claim_typed_ingress(
                existing,
                request,
                attachments_ready=all(
                    attachment["ingest_state"] != "pending"
                    for attachment in attachment_summaries
                ),
            )
            if ingress is not None:
                replay_result["deferred_ingress"] = ingress
            return replay_result

        message_ref = _message_ref(request.guild_id, request.channel_id, request.message_id)
        utterance = OperatorUtterance(
            ref_id=new_public_ref("utt"),
            actor_ref=_actor_ref(request.actor_id),
            transport="discord",
            source_message_ref=message_ref,
            conversation_ref=_conversation_ref(request.guild_id, request.channel_id),
            reply_to_source_ref=(
                _message_ref(
                    request.guild_id,
                    request.channel_id,
                    request.reply_to_message_id,
                )
                if request.reply_to_message_id is not None
                else None
            ),
            said_at=_discord_said_at(request.message_id),
            verbatim_text=request.verbatim_text,
            content_hash=_sha256_text(request.verbatim_text),
            request_key=request.request_key,
        )
        created = True
        try:
            with self.session.begin_nested():
                attachment_service.plan_new(utterance, captures)
                self.session.add(utterance)
                self.session.flush()
        except IntegrityError:
            created = False
            existing = self.session.scalar(
                select(OperatorUtterance).where(
                    OperatorUtterance.request_key == request.request_key
                )
            )
            if existing is None or not self._same_utterance(existing, request):
                raise IdempotencyConflict(request.request_key) from None
            utterance = existing
            attachment_service.reconcile_existing(utterance, captures)
        self._ensure_utterance_audit(utterance, request)
        attachment_summaries = attachment_service.summaries(utterance)
        result: dict[str, Any] = {
            "ok": True,
            "ref": utterance.ref_id,
            "state": "recorded",
            "content_hash": utterance.content_hash,
            "disposition": "created" if created else "replayed_request",
            "reply_binding": ReplyBindingService(self.session).resolve(utterance),
            "attachments": attachment_summaries,
        }
        ingress = self._capture_or_claim_typed_ingress(
            utterance,
            request,
            attachments_ready=all(
                attachment["ingest_state"] != "pending"
                for attachment in attachment_summaries
            ),
        )
        if ingress is not None:
            result["deferred_ingress"] = ingress
        return result

    def _capture_or_claim_typed_ingress(
        self,
        utterance: OperatorUtterance,
        request: OperatorUtteranceCapture,
        *,
        attachments_ready: bool,
    ) -> dict[str, Any] | None:
        if request.gateway_instance_ref is None:
            return None
        existing = self.session.scalar(
            select(DeferredIngress).where(DeferredIngress.source_key == request.request_key)
        )
        if existing is not None and existing.utterance_ref != utterance.ref_id:
            raise IdempotencyConflict(request.request_key)
        if existing is not None and existing.status in {"completed", "rejected", "claimed"}:
            return {
                "ref": existing.ref_id,
                "state": existing.status,
                "drain_ref": existing.drain_ref,
                "execution_completion_token": None,
                "claim_token": str(existing.claim_token) if existing.claim_token else None,
            }

        if not attachments_ready:
            ingress = existing or DeferredIngress(
                source_key=request.request_key,
                ingress_kind="typed_message",
                utterance_ref=utterance.ref_id,
            )
            ingress.status = "pending"
            ingress.claimed_by_gateway_ref = None
            ingress.claim_token = None
            ingress.claimed_at = None
            if existing is None:
                self.session.add(ingress)
            self.session.flush()
            return {
                "ref": ingress.ref_id,
                "state": "pending",
                "drain_ref": ingress.drain_ref,
                "execution_completion_token": None,
                "attachment_state": "pending",
            }

        continuity = ContinuityService(self.session)
        claim_token = uuid.uuid4()
        completion_token: str | None = None
        drain_ref: str | None = None
        state = "claimed"
        try:
            lease = continuity.acquire_execution_lease(
                lease_key=f"interactive:{utterance.ref_id}:{claim_token}",
                lease_kind="interactive_turn",
                subject_ref=utterance.ref_id,
                gateway_instance_ref=request.gateway_instance_ref,
            )
            completion_token = lease.completion_token
        except DocketError as exc:
            if exc.code != "deployment_drain_active":
                raise
            state = "pending"
            drain_ref = str((exc.details or {}).get("drain_ref") or "") or None

        ingress = existing or DeferredIngress(
            source_key=request.request_key,
            ingress_kind="typed_message",
            utterance_ref=utterance.ref_id,
        )
        ingress.status = state
        ingress.drain_ref = drain_ref
        ingress.claimed_by_gateway_ref = (
            request.gateway_instance_ref if state == "claimed" else None
        )
        ingress.claim_token = claim_token if state == "claimed" else None
        ingress.claimed_at = utc_now() if state == "claimed" else None
        if existing is None:
            self.session.add(ingress)
        self.session.flush()
        return {
            "ref": ingress.ref_id,
            "state": ingress.status,
            "drain_ref": ingress.drain_ref,
            "execution_completion_token": completion_token,
            "claim_token": str(ingress.claim_token) if ingress.claim_token else None,
        }

    def capture_agent_response(self, request: AgentResponseCapture) -> dict[str, Any]:
        self._validate_discord_surface(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            parent_channel_id=request.parent_channel_id,
            actor_id=request.actor_id,
        )
        if request.gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(request.gateway_instance_ref)
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == request.utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="response_utterance_binding_invalid",
                message="Agent response does not bind to the authenticated source utterance.",
            )
        expected_message_ref = _response_source_ref(
            utterance,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            source_message_id=request.source_message_id,
        )
        if (
            utterance.source_message_ref != expected_message_ref
            or utterance.actor_ref != _actor_ref(request.actor_id)
        ):
            raise DocketError(
                code="response_utterance_binding_invalid",
                message="Agent response does not bind to the authenticated source utterance.",
            )
        response_key = (
            f"discord:{request.guild_id}:{request.channel_id}:"
            f"{request.source_message_id}:response:{request.turn_id}"
        )
        existing = self.session.scalar(
            select(AgentResponse).where(AgentResponse.response_key == response_key)
        )
        if existing is not None:
            if (
                existing.verbatim_text != request.verbatim_text
                or existing.model_identifier != request.model_identifier
                or existing.responds_to_utterance_refs != [utterance.ref_id]
            ):
                raise IdempotencyConflict(response_key)
            if request.finalize_intent_turn:
                self._finalize_intent_turn(
                    utterance=utterance,
                    trace_ref=request.trace_ref,
                    response=existing,
                    response_disposition="final_response",
                )
            return {
                "ok": True,
                "ref": existing.ref_id,
                "state": existing.delivery_state,
                "projection_ref": existing.projection_ref,
                "disposition": "replayed_request",
            }

        tool_call_refs = list(
            self.session.scalars(
                select(ToolInvocation.ref_id)
                .where(ToolInvocation.trace_ref == request.trace_ref)
                .order_by(ToolInvocation.trace_ordinal, ToolInvocation.started_at)
            )
        )
        intent_turn, intent_session = self._intent_turn_for_utterance(utterance)
        if (
            intent_turn is not None
            and request.gateway_instance_ref is not None
            and intent_turn.gateway_instance_ref != request.gateway_instance_ref
        ):
            raise DocketError(
                code="gateway_lifetime_binding_mismatch",
                message="Agent response does not belong to the turn's gateway lifetime.",
            )
        context_packet_refs = (
            [ref for ref in intent_turn.context_refs if ref.startswith("ctx_")]
            if intent_turn is not None
            else []
        )
        response_ref = new_public_ref("rsp")
        projection_ref = new_public_ref("proj")
        semantic_content = {
            "response_ref": response_ref,
            "verbatim_text": request.verbatim_text,
        }
        projection = OperatorProjection(
            ref_id=projection_ref,
            projection_kind="agent_response",
            operator_ref=utterance.actor_ref,
            primary_public_ref=response_ref,
            intent_session_ref=(intent_session.ref_id if intent_session is not None else None),
            case_ref=(
                intent_session.case_refs[0]
                if intent_session is not None and len(intent_session.case_refs) == 1
                else None
            ),
            case_revision_ref=(
                intent_session.case_revision_refs[0]
                if intent_session is not None and len(intent_session.case_revision_refs) == 1
                else None
            ),
            brief_ref=(intent_session.brief_ref if intent_session is not None else None),
            semantic_content=semantic_content,
            visible_text=request.verbatim_text,
            render_schema_version=1,
            render_sha256=_sha256_json(semantic_content),
            component_sha256=_sha256_json([]),
            basis_refs=[utterance.ref_id, *tool_call_refs],
        )
        self.session.add(projection)
        self.session.flush()
        response = AgentResponse(
            ref_id=response_ref,
            response_key=response_key,
            conversation_ref=utterance.conversation_ref,
            intent_session_ref=(intent_session.ref_id if intent_session is not None else None),
            responds_to_utterance_refs=[utterance.ref_id],
            basis_refs=[utterance.ref_id, *tool_call_refs],
            verbatim_text=request.verbatim_text,
            model_identifier=request.model_identifier,
            context_packet_refs=context_packet_refs,
            tool_call_refs=tool_call_refs,
            generation_state="complete",
            delivery_state="pending",
            generated_at=request.generated_at,
            projection_ref=projection_ref,
            gateway_instance_ref=request.gateway_instance_ref,
        )
        self.session.add(response)
        self.session.flush()
        self.session.add(
            ProjectionDelivery(
                projection_id=projection.id,
                projection_ref=projection_ref,
                transport="discord",
                destination_ref=utterance.conversation_ref,
                source_message_ref=expected_message_ref,
                status="pending",
            )
        )
        self.session.add(
            AuditEvent(
                event_type="agent_response.submitted",
                entity_type="agent_response",
                entity_id=response.id,
                actor_type="hermes",
                actor_id=None,
                request_id=request.request_id,
                primary_ref=response.ref_id,
                affected_refs=[response.ref_id],
                basis_refs=list(response.basis_refs),
                data={
                    "projection_ref": projection_ref,
                    "model_identifier": response.model_identifier,
                },
            )
        )
        if request.finalize_intent_turn:
            self._finalize_intent_turn(
                utterance=utterance,
                trace_ref=request.trace_ref,
                response=response,
                response_disposition="final_response",
            )
        return {
            "ok": True,
            "ref": response.ref_id,
            "state": "pending",
            "projection_ref": projection_ref,
            "disposition": "created",
        }

    def finalize_agent_turn_without_response(
        self,
        request: AgentTurnNoResponse,
    ) -> dict[str, Any]:
        self._validate_discord_surface(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            parent_channel_id=request.parent_channel_id,
            actor_id=request.actor_id,
        )
        if request.gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(request.gateway_instance_ref)
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == request.utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="response_utterance_binding_invalid",
                message="Agent turn does not bind to the authenticated source utterance.",
            )
        expected_message_ref = _response_source_ref(
            utterance,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            source_message_id=request.source_message_id,
        )
        if (
            utterance.source_message_ref != expected_message_ref
            or utterance.actor_ref != _actor_ref(request.actor_id)
        ):
            raise DocketError(
                code="response_utterance_binding_invalid",
                message="Agent turn does not bind to the authenticated source utterance.",
            )
        turn = self._finalize_intent_turn(
            utterance=utterance,
            trace_ref=request.trace_ref,
            response=None,
            response_disposition="no_response",
        )
        return {
            "ok": True,
            "ref": turn.ref_id if turn is not None else utterance.ref_id,
            "state": "no_response",
            "disposition": "updated" if turn is not None else "no_op",
        }

    def _intent_turn_for_utterance(
        self,
        utterance: OperatorUtterance,
    ) -> tuple[IntentTurn | None, IntentSession | None]:
        turns = list(
            self.session.scalars(
                select(IntentTurn)
                .where(IntentTurn.utterance_ref == utterance.ref_id)
                .order_by(IntentTurn.created_at, IntentTurn.ref_id)
            )
        )
        if len(turns) > 1:
            raise DocketError(
                code="intent_turn_binding_ambiguous",
                message="OperatorUtterance is bound to more than one IntentTurn.",
            )
        if not turns:
            return None, None
        turn = turns[0]
        intent_session = self.session.scalar(
            select(IntentSession).where(IntentSession.id == turn.intent_session_id)
        )
        if intent_session is None:
            raise DocketError(
                code="intent_session_not_found",
                message="IntentTurn does not resolve to its durable IntentSession.",
            )
        return turn, intent_session

    def _finalize_intent_turn(
        self,
        *,
        utterance: OperatorUtterance,
        trace_ref: str,
        response: AgentResponse | None,
        response_disposition: str,
    ) -> IntentTurn | None:
        turn, intent_session = self._intent_turn_for_utterance(utterance)
        if turn is None or intent_session is None:
            return None
        invocations = list(
            self.session.scalars(
                select(ToolInvocation)
                .where(ToolInvocation.trace_ref == trace_ref)
                .order_by(ToolInvocation.trace_ordinal, ToolInvocation.started_at)
            )
        )
        tool_call_refs = [item.ref_id for item in invocations]
        resulting_semantic_refs: list[str] = []
        for invocation in invocations:
            if invocation.intent_session_ref is None:
                invocation.intent_session_ref = intent_session.ref_id
            if invocation.domain_state != "succeeded" or invocation.tool_name not in {
                "docket_commit_changeset",
                "docket_resolve_conflict",
            }:
                continue
            for ref in invocation.result_refs:
                if ref not in resulting_semantic_refs:
                    resulting_semantic_refs.append(ref)
        if turn.semantic_request_ref is not None:
            semantic_request = self.session.scalar(
                select(SemanticRequest).where(SemanticRequest.ref_id == turn.semantic_request_ref)
            )
            if semantic_request is not None:
                for semantic_ref in (
                    semantic_request.ref_id,
                    semantic_request.committed_changeset_ref,
                ):
                    if semantic_ref is not None and semantic_ref not in resulting_semantic_refs:
                        resulting_semantic_refs.append(semantic_ref)
        finalized = IntentSessionService(self.session).finalize_turn(
            IntentTurnFinalize(
                turn_ref=turn.ref_id,
                tool_call_refs=tool_call_refs,
                agent_response_ref=response.ref_id if response is not None else None,
                resulting_semantic_refs=resulting_semantic_refs,
                response_disposition=response_disposition,
            )
        )
        return finalized

    def update_agent_response_delivery(
        self,
        request: AgentResponseDeliveryUpdate,
    ) -> dict[str, Any]:
        self._validate_discord_surface(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            parent_channel_id=request.parent_channel_id,
            actor_id=request.actor_id,
        )
        if request.gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(request.gateway_instance_ref)
        response = self.session.scalar(
            select(AgentResponse)
            .where(AgentResponse.ref_id == request.response_ref)
            .with_for_update()
        )
        if response is None:
            raise DocketError(
                code="agent_response_not_found",
                message="Agent response reference does not exist.",
            )
        delivery = self.session.scalar(
            select(ProjectionDelivery)
            .where(ProjectionDelivery.projection_ref == response.projection_ref)
            .with_for_update()
        )
        utterance_ref = response.responds_to_utterance_refs[0]
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="response_utterance_binding_invalid",
                message="Agent response source utterance no longer exists.",
            )
        expected_message_ref = _response_source_ref(
            utterance,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            source_message_id=request.source_message_id,
        )
        if delivery is None or delivery.source_message_ref != expected_message_ref:
            raise DocketError(
                code="agent_response_projection_binding_invalid",
                message="Agent response delivery does not match its source projection.",
            )
        if response.delivery_state == "delivered":
            if request.outcome != "delivered":
                raise DocketError(
                    code="agent_response_delivery_terminal",
                    message="A delivered response cannot regress to failed.",
                )
            return {
                "ok": True,
                "ref": response.ref_id,
                "state": "delivered",
                "disposition": "replayed_request",
            }
        if (
            response.delivery_state == request.outcome
            and response.delivered_at == request.completed_at
            and response.delivery_error_code == request.error_code
        ):
            return {
                "ok": True,
                "ref": response.ref_id,
                "state": response.delivery_state,
                "disposition": "replayed_request",
            }

        response.delivery_state = request.outcome
        response.delivered_at = request.completed_at if request.outcome == "delivered" else None
        response.delivery_error_code = request.error_code
        delivery.status = request.outcome
        delivery.attempt_count += 1
        delivery.delivered_at = request.completed_at if request.outcome == "delivered" else None
        delivery.last_error_code = request.error_code
        self.session.add(
            AuditEvent(
                event_type=f"agent_response.{request.outcome}",
                entity_type="agent_response",
                entity_id=response.id,
                actor_type="discord",
                actor_id=None,
                request_id=request.request_id,
                primary_ref=response.ref_id,
                affected_refs=[response.ref_id],
                basis_refs=list(response.basis_refs),
                data={
                    "projection_ref": response.projection_ref,
                    "error_code": request.error_code,
                },
            )
        )
        return {
            "ok": True,
            "ref": response.ref_id,
            "state": response.delivery_state,
            "disposition": "updated",
        }

    def record_final_architecture_signoff(
        self,
        request: SpecificationSignoffCapture,
    ) -> dict[str, Any]:
        artifact = specification_artifact(
            request.document_ref,
            request.frozen_artifact_hash,
        )
        if artifact is None:
            raise DocketError(
                code="specification_signoff_artifact_mismatch",
                message="Specification sign-off does not identify an eligible frozen artifact.",
            )
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == request.utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="operator_utterance_not_found",
                message="Specification sign-off must reference a persisted OperatorUtterance.",
            )
        settings = get_settings()
        if (
            utterance.actor_ref != _actor_ref(settings.operator_discord_user_id)
            or utterance.transport != "discord"
            or utterance.verbatim_text != artifact.signoff_text
        ):
            raise DocketError(
                code="specification_signoff_not_explicit",
                message="OperatorUtterance is not the exact manifest-bound sign-off command.",
            )

        prerequisite_decisions: list[Decision] = []
        for binding in artifact.prerequisites:
            prerequisite_clauses = [
                Decision.decision_kind == binding.decision_kind,
                Decision.document_ref == binding.document_ref,
                Decision.frozen_artifact_hash == binding.frozen_artifact_hash,
                Decision.architecture_authority == binding.architecture_authority,
            ]
            if binding.decision_ref is not None:
                prerequisite_clauses.append(Decision.ref_id == binding.decision_ref)
            prerequisite = self.session.scalar(
                select(Decision).where(*prerequisite_clauses)
            )
            if prerequisite is None:
                details = {
                    "document_ref": binding.document_ref,
                    "frozen_artifact_hash": binding.frozen_artifact_hash,
                }
                if binding.decision_ref is not None:
                    details["decision_ref"] = binding.decision_ref
                raise DocketError(
                    code=(
                        "provenance_bootstrap_not_verified"
                        if artifact.document_ref == FROZEN_DOCUMENT_REF
                        else "specification_signoff_prerequisite_missing"
                    ),
                    message="Specification sign-off prerequisite provenance is unavailable.",
                    details=details,
                )
            prerequisite_decisions.append(prerequisite)
        prerequisite_decision_refs = [item.ref_id for item in prerequisite_decisions]

        bootstrap_utterance: OperatorUtterance | None = None
        if artifact.bootstrap_authority is not None:
            bootstrap_utterance = self.session.scalar(
                select(OperatorUtterance).where(
                    OperatorUtterance.ref_id == artifact.bootstrap_authority.utterance_ref
                )
            )
            if (
                bootstrap_utterance is None
                or bootstrap_utterance.actor_ref != _actor_ref(settings.operator_discord_user_id)
                or bootstrap_utterance.transport != "discord"
                or bootstrap_utterance.content_hash != artifact.bootstrap_authority.content_hash
                or bootstrap_utterance.content_hash
                != _sha256_text(bootstrap_utterance.verbatim_text)
            ):
                raise DocketError(
                    code="amendment_signoff_bootstrap_not_verified",
                    message=("The manifest-bound amendment bootstrap authority is unavailable."),
                )

        existing = self.session.scalar(
            select(Decision).where(
                Decision.decision_kind == "specification_signoff",
                Decision.document_ref == artifact.document_ref,
                Decision.frozen_artifact_hash == artifact.frozen_artifact_hash,
            )
        )
        if existing is not None:
            if (
                existing.basis_refs != [utterance.ref_id]
                or existing.authorized_scope != artifact.authorized_scope
                or existing.architecture_authority != artifact.architecture_authority
                or existing.implementation_authority != artifact.implementation_authority
            ):
                raise DocketError(
                    code="specification_signoff_conflict",
                    message="Frozen architecture already has conflicting sign-off authority.",
                )
            return {
                "ok": True,
                "ref": existing.ref_id,
                "state": "signed",
                "disposition": "replayed_request",
                "document_ref": artifact.document_ref,
                "frozen_artifact_hash": artifact.frozen_artifact_hash,
                "authorized_scope": artifact.authorized_scope,
                "implementation_authority": artifact.implementation_authority,
                "production_reset_authority": artifact.production_reset_authority,
                "prerequisite_decision_refs": prerequisite_decision_refs,
            }
        decision = Decision(
            decision_kind="specification_signoff",
            actor_ref=utterance.actor_ref,
            basis_refs=[utterance.ref_id],
            document_ref=artifact.document_ref,
            frozen_artifact_hash=artifact.frozen_artifact_hash,
            authorized_scope=artifact.authorized_scope,
            architecture_authority=artifact.architecture_authority,
            implementation_authority=artifact.implementation_authority,
            payload_json={
                "prerequisite_decision_refs": prerequisite_decision_refs,
                "bootstrap_utterance_ref": (
                    bootstrap_utterance.ref_id if bootstrap_utterance is not None else None
                ),
                "implementation_authority": artifact.implementation_authority,
                "production_reset_authority": artifact.production_reset_authority,
            },
        )
        self.session.add(decision)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="decision.specification_signoff_recorded",
                entity_type="decision",
                entity_id=decision.id,
                actor_type="operator",
                actor_id=settings.operator_discord_user_id,
                request_id=request.request_id,
                primary_ref=decision.ref_id,
                affected_refs=[
                    decision.ref_id,
                    utterance.ref_id,
                    *prerequisite_decision_refs,
                ],
                basis_refs=[utterance.ref_id],
                data={
                    "document_ref": artifact.document_ref,
                    "frozen_artifact_hash": artifact.frozen_artifact_hash,
                    "architecture_authority": artifact.architecture_authority,
                    "implementation_authority": artifact.implementation_authority,
                    "authorized_scope": artifact.authorized_scope,
                    "production_reset_authority": artifact.production_reset_authority,
                    "prerequisite_decision_refs": prerequisite_decision_refs,
                    "bootstrap_utterance_ref": (
                        bootstrap_utterance.ref_id if bootstrap_utterance is not None else None
                    ),
                },
            )
        )
        return {
            "ok": True,
            "ref": decision.ref_id,
            "state": "signed",
            "disposition": "created",
            "document_ref": artifact.document_ref,
            "frozen_artifact_hash": artifact.frozen_artifact_hash,
            "authorized_scope": artifact.authorized_scope,
            "implementation_authority": artifact.implementation_authority,
            "production_reset_authority": artifact.production_reset_authority,
            "prerequisite_decision_refs": prerequisite_decision_refs,
        }

    def record_production_reset_authorization(
        self,
        request: ProductionResetAuthorizationCapture,
    ) -> dict[str, Any]:
        if (
            request.document_ref != TRACKED_CONTEXT_DOCUMENT_REF
            or request.frozen_artifact_hash != TRACKED_CONTEXT_FROZEN_ARTIFACT_HASH
        ):
            raise DocketError(
                code="production_reset_artifact_mismatch",
                message="Production reset authority does not identify the frozen amendment.",
            )
        artifact = specification_artifact(
            request.document_ref,
            request.frozen_artifact_hash,
        )
        if artifact is None or artifact.production_reset_authority is not False:
            raise DocketError(
                code="production_reset_artifact_ineligible",
                message="The frozen artifact is not eligible for a later reset authorization.",
            )
        binding = ProductionResetAuthorityBinding(
            document_ref=request.document_ref,
            frozen_artifact_hash=request.frozen_artifact_hash,
            reset_manifest_sha256=request.reset_manifest_sha256,
            verified_backup_ref=request.verified_backup_ref,
            verified_backup_sha256=request.verified_backup_sha256,
            deployment_revision=request.deployment_revision,
        )
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == request.utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="operator_utterance_not_found",
                message="Production reset authority must reference a persisted OperatorUtterance.",
            )
        settings = get_settings()
        if (
            utterance.actor_ref != _actor_ref(settings.operator_discord_user_id)
            or utterance.transport != "discord"
            or utterance.verbatim_text != production_reset_authorization_text(binding)
            or utterance.content_hash != _sha256_text(utterance.verbatim_text)
        ):
            raise DocketError(
                code="production_reset_authorization_not_explicit",
                message=(
                    "OperatorUtterance is not the exact manifest, backup, and revision-bound "
                    "production reset command."
                ),
            )

        payload = {
            "reset_manifest_sha256": request.reset_manifest_sha256,
            "verified_backup_ref": request.verified_backup_ref,
            "verified_backup_sha256": request.verified_backup_sha256,
            "deployment_revision": request.deployment_revision,
        }
        candidates = self.session.scalars(
            select(Decision).where(
                Decision.decision_kind == "production_reset_authorization",
                Decision.document_ref == request.document_ref,
                Decision.frozen_artifact_hash == request.frozen_artifact_hash,
            )
        ).all()
        existing = next(
            (
                decision
                for decision in candidates
                if decision.basis_refs == [utterance.ref_id] and decision.payload_json == payload
            ),
            None,
        )
        if existing is not None:
            return {
                "ok": True,
                "ref": existing.ref_id,
                "state": "authorized",
                "disposition": "replayed_request",
                "document_ref": request.document_ref,
                "frozen_artifact_hash": request.frozen_artifact_hash,
                **payload,
                "production_reset_executed": False,
            }

        decision = Decision(
            decision_kind="production_reset_authorization",
            actor_ref=utterance.actor_ref,
            basis_refs=[utterance.ref_id],
            document_ref=request.document_ref,
            frozen_artifact_hash=request.frozen_artifact_hash,
            authorized_scope="production_reset_exact_manifest_revision",
            architecture_authority=False,
            implementation_authority=None,
            payload_json=payload,
        )
        self.session.add(decision)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="decision.production_reset_authorization_recorded",
                entity_type="decision",
                entity_id=decision.id,
                actor_type="operator",
                actor_id=settings.operator_discord_user_id,
                request_id=request.request_id,
                primary_ref=decision.ref_id,
                affected_refs=[decision.ref_id, utterance.ref_id],
                basis_refs=[utterance.ref_id],
                data={
                    "document_ref": request.document_ref,
                    "frozen_artifact_hash": request.frozen_artifact_hash,
                    **payload,
                    "production_reset_executed": False,
                },
            )
        )
        return {
            "ok": True,
            "ref": decision.ref_id,
            "state": "authorized",
            "disposition": "created",
            "document_ref": request.document_ref,
            "frozen_artifact_hash": request.frozen_artifact_hash,
            **payload,
            "production_reset_executed": False,
        }


def provenance_health_snapshot(session: Session) -> dict[str, Any]:
    bootstrap = session.scalar(
        select(Decision).where(
            Decision.decision_kind == "provenance_bootstrap_signoff",
            Decision.document_ref == FROZEN_DOCUMENT_REF,
            Decision.frozen_artifact_hash == FROZEN_ARTIFACT_HASH,
        )
    )
    return {
        "bootstrap_backfilled": bootstrap is not None,
        "bootstrap_decision_ref": bootstrap.ref_id if bootstrap is not None else None,
        "checked_at": utc_now().isoformat(),
    }
