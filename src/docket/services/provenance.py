from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import (
    AgentResponseCapture,
    AgentResponseDeliveryUpdate,
    AgentTurnNoResponse,
    OperatorUtteranceCapture,
    SpecificationSignoffCapture,
)
from docket.models import (
    AgentResponse,
    AgentResponseProjection,
    AuditEvent,
    Decision,
    DiscordDailyThread,
    IntentSession,
    IntentTurn,
    OperatorUtterance,
    Record,
    RecordSource,
    SemanticRequest,
    ToolInvocation,
)
from docket.models.base import utc_now
from docket.schemas.authority import IntentTurnFinalize
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.intent_sessions import IntentSessionService
from docket.services.mcp_traces import trace_id_for_source
from docket.services.reply_bindings import ReplyBindingService
from docket.specification_artifacts import specification_artifact

FROZEN_DOCUMENT_REF = "ONT-DELTA-2026-08-27"
FROZEN_ARTIFACT_HASH = "3d744f4d021f8a605086152eb76743a7ec5a7ed2c8754694e38c1a891a14b5e1"
BOOTSTRAP_CANONICAL_KEY = f"generic:provenance-bootstrap-authorization-{FROZEN_ARTIFACT_HASH}"
BOOTSTRAP_AUTHORIZATION_TEXT = (
    "I authorize the provenance-bootstrap phase defined by the frozen Docket ontology "
    f"architecture at SHA-256 `{FROZEN_ARTIFACT_HASH}`.\n\n"
    "This authorization is limited to the minimum phase-1 implementation required to "
    "establish the specified provenance and authority foundation, including immutable "
    "operator utterance capture, agent-response provenance, public references, decisions, "
    "tool-invocation logging, audit/provenance plumbing, and the migrations and runtime "
    "wiring required for those capabilities.\n\n"
    "This does not authorize the remaining ontology rollout, registry redesign, "
    "AttentionCase migration, new interactive authority behavior, approval removal, "
    "calendar-lane changes, triage capability changes, or other later-phase behavior.\n\n"
    "After provenance bootstrap is implemented and operational through the trusted "
    "Docket/Discord path, I will issue a separate ledger-backed sign-off before the "
    "remaining architecture may be implemented."
)
FINAL_ARCHITECTURE_SIGNOFF_TEXT = (
    "I explicitly sign off on Docket architecture "
    f"{FROZEN_DOCUMENT_REF} at SHA-256 `{FROZEN_ARTIFACT_HASH}`."
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        if (
            guild_id != settings.discord_guild_id
            or actor_id != settings.operator_discord_user_id
        ):
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
            and existing.conversation_ref
            == _conversation_ref(request.guild_id, request.channel_id)
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
        existing = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.request_key == request.request_key
            )
        )
        if existing is not None:
            if not self._same_utterance(existing, request):
                raise IdempotencyConflict(request.request_key)
            return {
                "ok": True,
                "ref": existing.ref_id,
                "state": "recorded",
                "content_hash": existing.content_hash,
                "disposition": "replayed_request",
                "reply_binding": ReplyBindingService(self.session).resolve(existing),
            }

        message_ref = _message_ref(request.guild_id, request.channel_id, request.message_id)
        utterance = OperatorUtterance(
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
        self.session.add(utterance)
        self.session.flush()
        self.session.add(
            AuditEvent(
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
                },
            )
        )
        return {
            "ok": True,
            "ref": utterance.ref_id,
            "state": "recorded",
            "content_hash": utterance.content_hash,
            "disposition": "created",
            "reply_binding": ReplyBindingService(self.session).resolve(utterance),
        }

    def capture_agent_response(self, request: AgentResponseCapture) -> dict[str, Any]:
        self._validate_discord_surface(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            parent_channel_id=request.parent_channel_id,
            actor_id=request.actor_id,
        )
        if request.gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(
                request.gateway_instance_ref
            )
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
            self._finalize_intent_turn(
                utterance=utterance,
                trace_id=request.trace_id,
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
                .where(ToolInvocation.trace_id == request.trace_id)
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
        projection_ref = f"discord_response:{response_ref}"
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
            AgentResponseProjection(
                response_id=response.id,
                projection_ref=projection_ref,
                operator_ref=utterance.actor_ref,
                primary_public_ref=response.ref_id,
                projection_version=1,
                case_revision_refs=(
                    list(intent_session.case_revision_refs)
                    if intent_session is not None
                    else []
                ),
                brief_ref=(intent_session.brief_ref if intent_session is not None else None),
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
        self._finalize_intent_turn(
            utterance=utterance,
            trace_id=request.trace_id,
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
            GatewayLifetimeService(self.session).require_live(
                request.gateway_instance_ref
            )
        utterance = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == request.utterance_ref
            )
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
            trace_id=request.trace_id,
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
        trace_id: uuid.UUID,
        response: AgentResponse | None,
        response_disposition: str,
    ) -> IntentTurn | None:
        turn, intent_session = self._intent_turn_for_utterance(utterance)
        if turn is None or intent_session is None:
            return None
        invocations = list(
            self.session.scalars(
                select(ToolInvocation)
                .where(ToolInvocation.trace_id == trace_id)
                .order_by(ToolInvocation.trace_ordinal, ToolInvocation.started_at)
            )
        )
        tool_call_refs = [item.ref_id for item in invocations]
        resulting_semantic_refs: list[str] = []
        for invocation in invocations:
            if invocation.intent_session_ref is None:
                invocation.intent_session_ref = intent_session.ref_id
            if invocation.status != "succeeded" or invocation.tool_name not in {
                "docket_commit_changeset",
                "docket_resolve_conflict",
            }:
                continue
            for ref in invocation.result_refs:
                if ref not in resulting_semantic_refs:
                    resulting_semantic_refs.append(ref)
        if turn.semantic_request_ref is not None:
            semantic_request = self.session.scalar(
                select(SemanticRequest).where(
                    SemanticRequest.ref_id == turn.semantic_request_ref
                )
            )
            if semantic_request is not None:
                for semantic_ref in (
                    semantic_request.ref_id,
                    semantic_request.committed_changeset_ref,
                ):
                    if (
                        semantic_ref is not None
                        and semantic_ref not in resulting_semantic_refs
                    ):
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
            GatewayLifetimeService(self.session).require_live(
                request.gateway_instance_ref
            )
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
        projection = self.session.scalar(
            select(AgentResponseProjection)
            .where(AgentResponseProjection.response_id == response.id)
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
        if projection is None or projection.source_message_ref != expected_message_ref:
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
        projection.status = request.outcome
        projection.attempt_count += 1
        projection.completed_at = request.completed_at
        projection.last_error_code = request.error_code
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

    def backfill_bootstrap_authorization(self) -> dict[str, str] | None:
        record = self.session.scalar(
            select(Record).where(
                Record.record_type == "generic",
                Record.canonical_key == BOOTSTRAP_CANONICAL_KEY,
            )
        )
        if record is None:
            return None
        data = record.data
        if (
            data.get("utterance") != BOOTSTRAP_AUTHORIZATION_TEXT
            or data.get("architecture_sha256") != FROZEN_ARTIFACT_HASH
            or data.get("authorized_phase") != "provenance-bootstrap phase 1"
            or data.get("authorization_status") != "authorized"
        ):
            raise DocketError(
                code="bootstrap_authorization_evidence_invalid",
                message="Stored bootstrap authorization does not match the frozen artifact.",
            )
        sources = list(
            self.session.scalars(
                select(RecordSource).where(RecordSource.record_id == record.id)
            )
        )
        if len(sources) != 1:
            raise DocketError(
                code="bootstrap_authorization_evidence_invalid",
                message="Bootstrap authorization must have exactly one trusted Discord source.",
            )
        source = sources[0]
        metadata = source.source_metadata
        message_id = str(source.source_object_id or "")
        guild_id = str(metadata.get("guild_id") or "")
        channel_id = str(metadata.get("channel_id") or "")
        actor_id = str(metadata.get("user_id") or "")
        parent_channel_id = metadata.get("parent_channel_id")
        self._validate_discord_surface(
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=str(parent_channel_id) if parent_channel_id else None,
            actor_id=actor_id,
        )
        if (
            source.source_type != "discord_message"
            or not message_id.isascii()
            or not message_id.isdecimal()
            or metadata.get("message_id") != message_id
        ):
            raise DocketError(
                code="bootstrap_authorization_evidence_invalid",
                message="Bootstrap authorization source binding is malformed.",
            )

        request_key = source.source_request_key
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.request_key == request_key)
        )
        if utterance is None:
            utterance = OperatorUtterance(
                actor_ref=_actor_ref(actor_id),
                transport="discord",
                source_message_ref=_message_ref(guild_id, channel_id, message_id),
                conversation_ref=_conversation_ref(guild_id, channel_id),
                said_at=_discord_said_at(message_id),
                recorded_at=record.created_at,
                verbatim_text=BOOTSTRAP_AUTHORIZATION_TEXT,
                content_hash=_sha256_text(BOOTSTRAP_AUTHORIZATION_TEXT),
                request_key=request_key,
                source_record_id=record.id,
            )
            self.session.add(utterance)
            self.session.flush()
        elif (
            utterance.verbatim_text != BOOTSTRAP_AUTHORIZATION_TEXT
            or utterance.source_record_id not in {None, record.id}
        ):
            raise DocketError(
                code="bootstrap_authorization_evidence_conflict",
                message="Bootstrap authorization collides with a different utterance.",
            )

        decision = self.session.scalar(
            select(Decision).where(
                Decision.decision_kind == "provenance_bootstrap_signoff",
                Decision.document_ref == FROZEN_DOCUMENT_REF,
                Decision.frozen_artifact_hash == FROZEN_ARTIFACT_HASH,
            )
        )
        if decision is None:
            decision = Decision(
                decision_kind="provenance_bootstrap_signoff",
                actor_ref=utterance.actor_ref,
                basis_refs=[utterance.ref_id],
                document_ref=FROZEN_DOCUMENT_REF,
                frozen_artifact_hash=FROZEN_ARTIFACT_HASH,
                authorized_scope="provenance_bootstrap_only",
                architecture_authority=False,
                implementation_authority="provenance_bootstrap_only",
                payload_json={
                    "source_record_id": str(record.id),
                    "source_request_key": source.source_request_key,
                    "authorization_content_hash": utterance.content_hash,
                },
            )
            self.session.add(decision)
            self.session.flush()
            self.session.add(
                AuditEvent(
                    event_type="decision.provenance_bootstrap_signoff_recorded",
                    entity_type="decision",
                    entity_id=decision.id,
                    actor_type="operator",
                    actor_id=actor_id,
                    request_id=None,
                    primary_ref=decision.ref_id,
                    affected_refs=[decision.ref_id, utterance.ref_id],
                    basis_refs=[utterance.ref_id],
                    data={
                        "document_ref": FROZEN_DOCUMENT_REF,
                        "frozen_artifact_hash": FROZEN_ARTIFACT_HASH,
                        "authorized_scope": "provenance_bootstrap_only",
                        "architecture_authority": False,
                    },
                )
            )
        elif decision.basis_refs != [utterance.ref_id]:
            raise DocketError(
                code="bootstrap_authorization_decision_conflict",
                message="Bootstrap Decision is not based on the exact authorization utterance.",
            )
        return {"utterance_ref": utterance.ref_id, "decision_ref": decision.ref_id}

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
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == request.utterance_ref
            )
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

        prerequisite = self.session.scalar(
            select(Decision).where(
                Decision.decision_kind == artifact.prerequisite.decision_kind,
                Decision.document_ref == artifact.prerequisite.document_ref,
                Decision.frozen_artifact_hash
                == artifact.prerequisite.frozen_artifact_hash,
            )
        )
        if prerequisite is None:
            raise DocketError(
                code=(
                    "provenance_bootstrap_not_verified"
                    if artifact.document_ref == FROZEN_DOCUMENT_REF
                    else "specification_signoff_prerequisite_missing"
                ),
                message="Specification sign-off prerequisite provenance is unavailable.",
            )

        bootstrap_utterance: OperatorUtterance | None = None
        if artifact.bootstrap_authority is not None:
            bootstrap_utterance = self.session.scalar(
                select(OperatorUtterance).where(
                    OperatorUtterance.ref_id == artifact.bootstrap_authority.utterance_ref
                )
            )
            if (
                bootstrap_utterance is None
                or bootstrap_utterance.actor_ref
                != _actor_ref(settings.operator_discord_user_id)
                or bootstrap_utterance.transport != "discord"
                or bootstrap_utterance.content_hash
                != artifact.bootstrap_authority.content_hash
                or bootstrap_utterance.content_hash
                != _sha256_text(bootstrap_utterance.verbatim_text)
            ):
                raise DocketError(
                    code="amendment_signoff_bootstrap_not_verified",
                    message=(
                        "The manifest-bound amendment bootstrap authority is unavailable."
                    ),
                )

        existing = self.session.scalar(
            select(Decision).where(
                Decision.decision_kind == "specification_signoff",
                Decision.document_ref == artifact.document_ref,
                Decision.frozen_artifact_hash == artifact.frozen_artifact_hash,
            )
        )
        if existing is not None:
            if existing.basis_refs != [utterance.ref_id]:
                raise DocketError(
                    code="specification_signoff_conflict",
                    message="Frozen architecture already has a different final sign-off basis.",
                )
            return {
                "ok": True,
                "ref": existing.ref_id,
                "state": "signed",
                "disposition": "replayed_request",
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
                "prerequisite_decision_ref": prerequisite.ref_id,
                "bootstrap_utterance_ref": (
                    bootstrap_utterance.ref_id if bootstrap_utterance is not None else None
                ),
                "implementation_authority": artifact.implementation_authority,
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
                affected_refs=[decision.ref_id, utterance.ref_id],
                basis_refs=[utterance.ref_id],
                data={
                    "document_ref": artifact.document_ref,
                    "frozen_artifact_hash": artifact.frozen_artifact_hash,
                    "architecture_authority": artifact.architecture_authority,
                    "implementation_authority": artifact.implementation_authority,
                    "authorized_scope": artifact.authorized_scope,
                    "prerequisite_decision_ref": prerequisite.ref_id,
                    "bootstrap_utterance_ref": (
                        bootstrap_utterance.ref_id
                        if bootstrap_utterance is not None
                        else None
                    ),
                },
            )
        )
        return {
            "ok": True,
            "ref": decision.ref_id,
            "state": "signed",
            "disposition": "created",
        }


def bootstrap_trace_id(guild_id: str, channel_id: str, message_id: str) -> uuid.UUID:
    return trace_id_for_source(guild_id, channel_id, message_id)


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
