from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import (
    AgentResponse,
    AuditEvent,
    IntentSession,
    IntentTurn,
    OperatorUtterance,
    ToolInvocation,
)
from docket.schemas.authority import (
    IntentSessionOpen,
    IntentTurnAppend,
    IntentTurnFinalize,
)
from docket.services.provenance_refs import ProvenanceRefService
from docket.services.statements import StatementService


class IntentSessionService:
    """Durable multi-turn ascertainment rooted in authenticated utterances."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _expected_actor_ref() -> str:
        return f"discord_user:{get_settings().operator_discord_user_id}"

    def _utterance(self, utterance_ref: str) -> OperatorUtterance:
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref)
        )
        if utterance is None:
            raise DocketError(
                code="operator_utterance_not_found",
                message="Intent processing requires a persisted OperatorUtterance.",
                details={"utterance_ref": utterance_ref},
            )
        if utterance.actor_ref != self._expected_actor_ref():
            raise DocketError(
                code="operator_utterance_authority_required",
                message="Intent processing requires the authenticated Operator principal.",
            )
        return utterance

    def open(self, request: IntentSessionOpen) -> tuple[IntentSession, bool]:
        utterance = self._utterance(request.source_utterance_ref)
        existing = self.session.scalar(
            select(IntentSession).where(
                IntentSession.source_utterance_ref == utterance.ref_id
            )
        )
        if existing is not None:
            return existing, False
        provenance = ProvenanceRefService(self.session)
        provenance.require_all(
            [
                *request.case_refs,
                *request.case_revision_refs,
                *request.trusted_context_refs,
                *([request.brief_ref] if request.brief_ref is not None else []),
            ]
        )
        intent_session = IntentSession(
            conversation_ref=utterance.conversation_ref,
            source_utterance_ref=utterance.ref_id,
            case_refs=list(request.case_refs),
            case_revision_refs=list(request.case_revision_refs),
            brief_ref=request.brief_ref,
            trusted_context_refs=list(request.trusted_context_refs),
            state="open",
            semantic_state="open",
            commit_state="not_attempted",
        )
        self.session.add(intent_session)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="intent_session.opened",
                entity_type="intent_session",
                entity_id=intent_session.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                request_id=None,
                primary_ref=intent_session.ref_id,
                affected_refs=[intent_session.ref_id],
                basis_refs=[utterance.ref_id],
                data={
                    "case_refs": intent_session.case_refs,
                    "brief_ref": intent_session.brief_ref,
                },
            )
        )
        return intent_session, True

    def get(self, session_ref: str) -> IntentSession:
        intent_session = self.session.scalar(
            select(IntentSession).where(IntentSession.ref_id == session_ref)
        )
        if intent_session is None:
            raise DocketError(
                code="intent_session_not_found",
                message="IntentSession public reference was not found.",
                details={"intent_session_ref": session_ref},
            )
        return intent_session

    def append_turn(self, request: IntentTurnAppend) -> tuple[IntentSession, IntentTurn]:
        intent_session = self.get(request.intent_session_ref)
        if intent_session.state not in {"open", "needs_clarification"}:
            raise DocketError(
                code="intent_session_not_open",
                message="This IntentSession no longer accepts turns.",
                details={"state": intent_session.state},
            )
        utterance = self._utterance(request.utterance_ref)
        if utterance.conversation_ref != intent_session.conversation_ref:
            raise DocketError(
                code="intent_conversation_mismatch",
                message="OperatorUtterance belongs to a different conversation.",
            )
        existing = self.session.scalar(
            select(IntentTurn).where(
                IntentTurn.intent_session_id == intent_session.id,
                IntentTurn.utterance_ref == utterance.ref_id,
            )
        )
        if existing is not None:
            return intent_session, existing
        provenance = ProvenanceRefService(self.session)
        provenance.require_all(request.context_refs)
        statements = StatementService(self.session).derive(
            utterance.ref_id, list(request.statements)
        )
        for relation in request.relations:
            StatementService(self.session).relate(relation)
        call_refs: list[str] = []
        for call_ref in request.tool_call_refs:
            invocation = provenance.get(call_ref)
            if not isinstance(invocation, ToolInvocation):
                raise DocketError(
                    code="invalid_intent_tool_ref",
                    message="IntentTurn tool_call_refs must identify ToolInvocations.",
                    details={"ref": call_ref},
                )
            if utterance.ref_id not in invocation.utterance_refs:
                raise DocketError(
                    code="intent_tool_authority_mismatch",
                    message="ToolInvocation is not bound to this OperatorUtterance.",
                    details={"ref": call_ref},
                )
            call_refs.append(call_ref)
        if request.agent_response_ref is not None:
            response = provenance.get(request.agent_response_ref)
            if not isinstance(response, AgentResponse):
                raise DocketError(
                    code="invalid_intent_response_ref",
                    message="agent_response_ref must identify an AgentResponse.",
                )
            if utterance.ref_id not in response.responds_to_utterance_refs:
                raise DocketError(
                    code="intent_response_authority_mismatch",
                    message="AgentResponse does not respond to this turn's utterance.",
                )
        if request.response_disposition == "final_response" and request.agent_response_ref is None:
            raise DocketError(
                code="intent_response_required",
                message="final_response disposition requires an AgentResponse reference.",
            )
        if request.response_disposition == "no_response" and request.agent_response_ref is not None:
            raise DocketError(
                code="intent_response_forbidden",
                message="no_response disposition cannot name an AgentResponse.",
            )
        turn = IntentTurn(
            intent_session_id=intent_session.id,
            intent_session_ref=intent_session.ref_id,
            utterance_ref=utterance.ref_id,
            statement_refs=[item.ref_id for item in statements],
            context_refs=list(request.context_refs),
            tool_call_refs=call_refs,
            agent_response_ref=request.agent_response_ref,
            resulting_semantic_refs=[],
            response_disposition=request.response_disposition,
            semantic_request_ref=request.semantic_request_ref,
            authority_substitutions_json=dict(request.authority_substitutions),
            gateway_instance_ref=request.gateway_instance_ref,
        )
        self.session.add(turn)
        self.session.flush()
        intent_session.resolved_intent_json = dict(request.resolved_intent_json)
        intent_session.blocking_clarifications = list(request.blocking_clarifications)
        intent_session.state = (
            "needs_clarification" if request.blocking_clarifications else "open"
        )
        intent_session.semantic_state = intent_session.state
        intent_session.commit_state = "not_attempted"
        intent_session.version += 1
        self.session.add(
            AuditEvent(
                event_type="intent_turn.recorded",
                entity_type="intent_turn",
                entity_id=turn.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                request_id=None,
                primary_ref=turn.ref_id,
                affected_refs=[turn.ref_id, intent_session.ref_id, *turn.statement_refs],
                basis_refs=[utterance.ref_id],
                data={
                    "response_disposition": turn.response_disposition,
                    "blocking_clarification_count": len(
                        intent_session.blocking_clarifications
                    ),
                },
            )
        )
        return intent_session, turn

    def projection(self, intent_session: IntentSession) -> dict[str, Any]:
        turns = list(
            self.session.scalars(
                select(IntentTurn)
                .where(IntentTurn.intent_session_id == intent_session.id)
                .order_by(IntentTurn.created_at, IntentTurn.ref_id)
            )
        )
        return {
            "ref": intent_session.ref_id,
            "state": intent_session.state,
            "semantic_state": intent_session.semantic_state,
            "commit_state": intent_session.commit_state,
            "version": intent_session.version,
            "source_utterance_ref": intent_session.source_utterance_ref,
            "case_refs": intent_session.case_refs,
            "case_revision_refs": intent_session.case_revision_refs,
            "brief_ref": intent_session.brief_ref,
            "trusted_context_refs": intent_session.trusted_context_refs,
            "resolved_intent": intent_session.resolved_intent_json,
            "blocking_clarifications": intent_session.blocking_clarifications,
            "committed_changeset_ref": intent_session.committed_changeset_ref,
            "turns": [
                {
                    "ref": turn.ref_id,
                    "utterance_ref": turn.utterance_ref,
                    "statement_refs": turn.statement_refs,
                    "context_refs": turn.context_refs,
                    "tool_call_refs": turn.tool_call_refs,
                    "agent_response_ref": turn.agent_response_ref,
                    "resulting_semantic_refs": turn.resulting_semantic_refs,
                    "response_disposition": turn.response_disposition,
                }
                for turn in turns
            ],
        }

    def finalize_turn(self, request: IntentTurnFinalize) -> IntentTurn:
        turn = self.session.scalar(
            select(IntentTurn).where(IntentTurn.ref_id == request.turn_ref)
        )
        if turn is None:
            raise DocketError(
                code="intent_turn_not_found",
                message="IntentTurn public reference was not found.",
            )
        if turn.response_disposition != "pending":
            if (
                turn.response_disposition == request.response_disposition
                and turn.agent_response_ref == request.agent_response_ref
                and turn.tool_call_refs == request.tool_call_refs
                and turn.resulting_semantic_refs == request.resulting_semantic_refs
            ):
                return turn
            raise DocketError(
                code="intent_turn_immutable",
                message="Finalized IntentTurn cannot be changed.",
            )
        provenance = ProvenanceRefService(self.session)
        for call_ref in request.tool_call_refs:
            invocation = provenance.get(call_ref)
            if (
                not isinstance(invocation, ToolInvocation)
                or turn.utterance_ref not in invocation.utterance_refs
            ):
                raise DocketError(
                    code="intent_tool_authority_mismatch",
                    message="ToolInvocation is not bound to this IntentTurn utterance.",
                    details={"ref": call_ref},
                )
        provenance.require_all(request.resulting_semantic_refs)
        if request.agent_response_ref is not None:
            response = provenance.get(request.agent_response_ref)
            if (
                not isinstance(response, AgentResponse)
                or turn.utterance_ref not in response.responds_to_utterance_refs
            ):
                raise DocketError(
                    code="intent_response_authority_mismatch",
                    message="AgentResponse is not bound to this IntentTurn utterance.",
                )
        turn.tool_call_refs = list(request.tool_call_refs)
        turn.agent_response_ref = request.agent_response_ref
        turn.resulting_semantic_refs = list(request.resulting_semantic_refs)
        turn.response_disposition = request.response_disposition
        self.session.add(
            AuditEvent(
                event_type="intent_turn.finalized",
                entity_type="intent_turn",
                entity_id=turn.id,
                actor_type="docket_runtime",
                actor_id=None,
                request_id=None,
                primary_ref=turn.ref_id,
                affected_refs=[turn.ref_id, *turn.resulting_semantic_refs],
                basis_refs=[turn.utterance_ref, *turn.tool_call_refs],
                data={"response_disposition": turn.response_disposition},
            )
        )
        return turn
