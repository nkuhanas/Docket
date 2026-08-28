from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import ChangeSet, OperatorUtterance
from docket.schemas.authority import (
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    ChangeSetRevise,
    IntentSessionOpen,
    IntentTurnAppend,
    StatementInput,
    StatementRelationInput,
)
from docket.services.canonical_events import CanonicalEventAuthorityService
from docket.services.case_resolutions import AttentionCaseResolutionService
from docket.services.change_sets import (
    CanonicalChangeHandler,
    ChangeSetService,
    ProviderIntentHandler,
)
from docket.services.intent_sessions import IntentSessionService
from docket.services.policies import ContextPolicyService
from docket.services.provider_intents import ProviderIntentService
from docket.services.registry import RegistryService
from docket.services.registry_conflicts import RegistryConflictCompiler
from docket.services.reply_bindings import ReplyBindingService


class InteractiveAuthorityService:
    """Compile one authenticated conversational turn into durable intent state."""

    def __init__(
        self,
        session: Session,
        *,
        handlers: dict[str, CanonicalChangeHandler] | None = None,
        provider_handler: ProviderIntentHandler | None = None,
    ) -> None:
        self.session = session
        registry_handlers = RegistryService(session).handlers()
        policy_handlers = ContextPolicyService(session).handlers()
        event_handlers = CanonicalEventAuthorityService(session).handlers()
        case_resolution_handlers = AttentionCaseResolutionService(session).handlers()
        provider_service = ProviderIntentService(session)
        self.changesets = ChangeSetService(
            session,
            handlers={
                **registry_handlers,
                **policy_handlers,
                **event_handlers,
                **case_resolution_handlers,
                **(handlers or {}),
            },
            provider_handler=provider_handler or provider_service.materialize,
        )

    def _authority_utterance(
        self,
        *,
        utterance_ref: str,
        request_key: str,
        actor_id: str,
    ) -> OperatorUtterance:
        components = request_key.split(":")
        if len(components) != 5 or components[0] != "discord":
            raise DocketError(
                code="invalid_request_key",
                message="Interactive authority requires a trusted Discord request key.",
            )
        authority_key = ":".join([*components[:4], "0"])
        utterance = self.session.scalar(
            select(OperatorUtterance).where(
                OperatorUtterance.ref_id == utterance_ref,
                OperatorUtterance.request_key == authority_key,
            )
        )
        if (
            utterance is None
            or utterance.actor_ref != f"discord_user:{actor_id}"
            or actor_id != get_settings().operator_discord_user_id
        ):
            raise DocketError(
                code="operator_utterance_authority_required",
                message="Turn authority does not resolve to the authenticated OperatorUtterance.",
            )
        return utterance

    def process_turn(
        self,
        *,
        utterance_ref: str,
        request_key: str,
        actor_id: str,
        intent_session_ref: str | None,
        expected_session_version: int | None,
        statements: list[StatementInput],
        relations: list[StatementRelationInput],
        resolved_intent_json: dict[str, Any],
        blocking_clarifications: list[dict[str, Any]],
        content: ChangeSetContent | None,
        changeset_ref: str | None,
        expected_changeset_version: int | None,
    ) -> dict[str, Any]:
        utterance = self._authority_utterance(
            utterance_ref=utterance_ref,
            request_key=request_key,
            actor_id=actor_id,
        )
        replay = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.idempotency_key == f"{request_key}:changeset"
            )
        )
        if replay is not None and (
            changeset_ref is None or replay.ref_id == changeset_ref
        ):
            return {
                "ok": True,
                "ref": replay.ref_id,
                "state": replay.state,
                "summary": "Replayed the existing durable ChangeSet result.",
                "affected_refs": [],
                "basis_refs": replay.basis_refs,
                "next": (
                    None
                    if replay.state == "committed"
                    else {"clarifications": replay.validation_errors}
                ),
                "warnings": [],
                "disposition": "replayed_request",
                "intent_session_ref": replay.intent_session_ref,
            }
        intent_service = IntentSessionService(self.session)
        if intent_session_ref is None:
            reply_binding = ReplyBindingService(self.session).resolve(utterance) or {}
            intent_session, _created = intent_service.open(
                IntentSessionOpen(
                    source_utterance_ref=utterance.ref_id,
                    case_refs=reply_binding.get("case_refs", []),
                    case_revision_refs=reply_binding.get("case_revision_refs", []),
                    brief_ref=reply_binding.get("brief_ref"),
                    trusted_context_refs=reply_binding.get(
                        "trusted_context_refs", []
                    ),
                )
            )
        else:
            intent_session = intent_service.get(intent_session_ref)
            if (
                expected_session_version is not None
                and intent_session.version != expected_session_version
            ):
                raise DocketError(
                    code="version_conflict",
                    message="IntentSession changed after it was read.",
                    details={
                        "expected_version": expected_session_version,
                        "current_version": intent_session.version,
                    },
                )
        intent_session, turn = intent_service.append_turn(
            IntentTurnAppend(
                intent_session_ref=intent_session.ref_id,
                utterance_ref=utterance.ref_id,
                statements=statements,
                relations=relations,
                resolved_intent_json=resolved_intent_json,
                blocking_clarifications=blocking_clarifications,
                response_disposition="pending",
            )
        )
        conflict_refs = RegistryConflictCompiler(self.session).compile(
            list(turn.statement_refs)
        )
        if conflict_refs:
            compiled_clarifications = [
                {
                    "blocking": True,
                    "code": "ambiguous_contradiction",
                    "conflict_ref": conflict_ref,
                    "question": (
                        "Does the new statement replace the prior value, apply to a "
                        "different scope, or retract it?"
                    ),
                }
                for conflict_ref in conflict_refs
            ]
            existing_conflicts = {
                str(item.get("conflict_ref"))
                for item in intent_session.blocking_clarifications
            }
            intent_session.blocking_clarifications = [
                *intent_session.blocking_clarifications,
                *[
                    item
                    for item in compiled_clarifications
                    if str(item["conflict_ref"]) not in existing_conflicts
                ],
            ]
            intent_session.state = "needs_clarification"
            intent_session.version += 1
        if content is None:
            if not intent_session.blocking_clarifications:
                raise DocketError(
                    code="changeset_content_required",
                    message=(
                        "A turn without a ChangeSet must identify at least one blocking "
                        "clarification."
                    ),
                )
            return {
                "ok": True,
                "ref": intent_session.ref_id,
                "state": "needs_clarification",
                "summary": "Intent was preserved and requires consolidated clarification.",
                "affected_refs": [intent_session.ref_id, turn.ref_id],
                "basis_refs": [utterance.ref_id],
                "next": {"clarifications": intent_session.blocking_clarifications},
                "warnings": [],
                "intent_session": intent_service.projection(intent_session),
            }

        if changeset_ref is None:
            changeset, _created = self.changesets.prepare(
                ChangeSetPrepare(
                    intent_session_ref=intent_session.ref_id,
                    expected_session_version=intent_session.version,
                    idempotency_key=f"{request_key}:changeset",
                    content=content,
                )
            )
        else:
            if expected_changeset_version is None:
                raise DocketError(
                    code="expected_version_required",
                    message="Revising a ChangeSet requires its expected version.",
                )
            changeset = self.changesets.revise(
                ChangeSetRevise(
                    changeset_ref=changeset_ref,
                    expected_version=expected_changeset_version,
                    content=content,
                )
            )
        if changeset.state != "validated":
            return {
                "ok": True,
                "ref": changeset.ref_id,
                "state": "needs_clarification",
                "summary": "ChangeSet is durable but does not yet satisfy Resolved Intent.",
                "affected_refs": [intent_session.ref_id, turn.ref_id, changeset.ref_id],
                "basis_refs": changeset.basis_refs,
                "next": {
                    "clarifications": intent_session.blocking_clarifications,
                    "allowed_conflict_actions": [
                        "resolve_conflict",
                        "remove_blocked_mutation",
                        "cancel_changeset",
                    ],
                },
                "warnings": [],
                "intent_session": intent_service.projection(intent_session),
                "changeset": self.changesets.projection(changeset),
            }
        committed, affected_refs = self.changesets.commit(
            ChangeSetCommit(
                changeset_ref=changeset.ref_id,
                expected_version=changeset.version,
                idempotency_key=changeset.idempotency_key,
                authority_utterance_ref=utterance.ref_id,
            )
        )
        return {
            "ok": True,
            "ref": committed.ref_id,
            "state": "committed",
            "summary": "Resolved Operator intent committed atomically.",
            "affected_refs": affected_refs,
            "basis_refs": committed.basis_refs,
            "next": None,
            "warnings": [],
            "intent_session_ref": intent_session.ref_id,
            "intent_turn_ref": turn.ref_id,
        }
