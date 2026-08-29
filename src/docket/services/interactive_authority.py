from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import (
    AttentionCase,
    CaseItem,
    ChangeSet,
    OperatorUtterance,
    PersistedSemanticOption,
    SemanticRequest,
    SemanticRequestAttempt,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    ChangeSetRevise,
    ConflictResolve,
    IntentSessionOpen,
    IntentTurnAppend,
    SemanticOptionDraft,
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
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.intent_sessions import IntentSessionService
from docket.services.policies import ContextPolicyService
from docket.services.provider_intents import ProviderIntentService
from docket.services.registry import RegistryService
from docket.services.registry_conflicts import RegistryConflictCompiler
from docket.services.reply_bindings import ReplyBindingService
from docket.services.semantic_options import (
    CURRENT_SELECTION_UTTERANCE,
    SemanticOptionService,
    complete_selection_provenance,
)


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
        semantic_options: list[SemanticOptionDraft] | None = None,
        semantic_request_ref: str | None = None,
        authority_scope_hash: str | None = None,
        precondition_hash: str | None = None,
        gateway_instance_ref: str | None = None,
    ) -> dict[str, Any]:
        utterance = self._authority_utterance(
            utterance_ref=utterance_ref,
            request_key=request_key,
            actor_id=actor_id,
        )
        if gateway_instance_ref is not None:
            GatewayLifetimeService(self.session).require_live(gateway_instance_ref)
        else:
            active_gateway = GatewayLifetimeService(self.session).current_live(
                "hermes_discord_gateway"
            )
            gateway_instance_ref = (
                active_gateway.ref_id if active_gateway is not None else None
            )
        semantic_request: SemanticRequest | None = None
        semantic_attempt: SemanticRequestAttempt | None = None
        if semantic_request_ref is not None:
            if content is None or authority_scope_hash is None or precondition_hash is None:
                raise DocketError(
                    code="semantic_request_binding_incomplete",
                    message="Semantic request execution requires content and both hashes.",
                )
            semantic_request = self.session.scalar(
                select(SemanticRequest).where(SemanticRequest.ref_id == semantic_request_ref)
            )
            if (
                semantic_request is None
                or semantic_request.intent_session_ref != intent_session_ref
                or semantic_request.authority_scope_hash != authority_scope_hash
                or semantic_request.current_precondition_hash != precondition_hash
                or utterance.ref_id not in semantic_request.origin_utterance_refs
                or semantic_request.authority_availability != "available"
            ):
                raise DocketError(
                    code="semantic_request_binding_mismatch",
                    message="Execution does not match the available selected authority scope.",
                )
            binding = semantic_request.selected_option_binding or {}
            option = self.session.scalar(
                select(PersistedSemanticOption).where(
                    PersistedSemanticOption.prompt_projection_ref
                    == binding.get("prompt_projection_ref"),
                    PersistedSemanticOption.prompt_projection_version
                    == binding.get("prompt_projection_version"),
                    PersistedSemanticOption.option_id == binding.get("option_id"),
                    PersistedSemanticOption.authority_scope_hash == authority_scope_hash,
                    PersistedSemanticOption.precondition_hash == precondition_hash,
                )
            )
            if option is None or complete_selection_provenance(
                option.compilation_template_json, utterance.ref_id
            ) != content.model_dump(mode="json"):
                raise DocketError(
                    code="semantic_request_scope_mismatch",
                    message="Submitted ChangeSet differs from the exact persisted option scope.",
                )
            next_attempt = int(
                self.session.scalar(
                    select(func.max(SemanticRequestAttempt.attempt_number)).where(
                        SemanticRequestAttempt.semantic_request_id == semantic_request.id
                    )
                )
                or 0
            ) + 1
            semantic_attempt = SemanticRequestAttempt(
                semantic_request_id=semantic_request.id,
                semantic_request_ref=semantic_request.ref_id,
                attempt_number=next_attempt,
                authority_scope_hash=authority_scope_hash,
                precondition_hash=precondition_hash,
                case_revision_ref=semantic_request.current_case_revision_ref,
                gateway_instance_ref=gateway_instance_ref,
                state="pending",
            )
            self.session.add(semantic_attempt)
            semantic_request.commit_state = "pending"
        replay = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.idempotency_key == f"{request_key}:changeset"
            )
        )
        if (
            replay is not None
            and (changeset_ref is None or replay.ref_id == changeset_ref)
            and (replay.state == "committed" or semantic_request is None)
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
                semantic_request_ref=(
                    semantic_request.ref_id if semantic_request is not None else None
                ),
                authority_substitutions=(
                    {CURRENT_SELECTION_UTTERANCE: utterance.ref_id}
                    if semantic_request is not None
                    else {}
                ),
                gateway_instance_ref=gateway_instance_ref,
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
            option_projection = None
            if semantic_options:
                question = next(
                    (
                        str(item.get("question", "")).strip()
                        for item in intent_session.blocking_clarifications
                        if str(item.get("question", "")).strip()
                    ),
                    "Choose how Docket should resolve this intent.",
                )
                option_projection = SemanticOptionService(self.session).persist_prompt(
                    utterance=utterance,
                    intent_session=intent_session,
                    question=question,
                    drafts=semantic_options,
                )
            return {
                "ok": True,
                "ref": intent_session.ref_id,
                "state": "needs_clarification",
                "summary": "Intent was preserved and requires consolidated clarification.",
                "affected_refs": [intent_session.ref_id, turn.ref_id],
                "basis_refs": [utterance.ref_id],
                "next": {
                    "clarifications": intent_session.blocking_clarifications,
                    **(
                        {
                            "semantic_prompt_ref": option_projection.ref_id,
                            "projection_version": option_projection.projection_version,
                            "delivery": "queued",
                        }
                        if option_projection is not None
                        else {}
                    ),
                },
                "warnings": [],
                "disposition": "needs_clarification",
                "intent_session": intent_service.projection(intent_session),
            }

        if changeset_ref is None:
            changeset, _created = self.changesets.prepare(
                ChangeSetPrepare(
                    intent_session_ref=intent_session.ref_id,
                    expected_session_version=intent_session.version,
                    idempotency_key=f"{request_key}:changeset",
                    content=content,
                    semantic_request_ref=(
                        semantic_request.ref_id if semantic_request is not None else None
                    ),
                    authority_scope_hash=authority_scope_hash,
                    precondition_hash=precondition_hash,
                    execution_binding=(
                        {
                            "selection_utterance_ref": utterance.ref_id,
                            "case_revision_ref": semantic_request.current_case_revision_ref,
                        }
                        if semantic_request is not None
                        else {}
                    ),
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
            if semantic_request is not None and semantic_attempt is not None:
                semantic_request.commit_state = intent_session.commit_state
                semantic_attempt.state = intent_session.commit_state
                semantic_attempt.change_set_ref = changeset.ref_id
                semantic_attempt.error_code = (
                    str(changeset.validation_errors[0].get("code"))[:128]
                    if changeset.validation_errors
                    else "changeset_validation_failed"
                )
                semantic_attempt.error_details_json = {
                    "errors": changeset.validation_errors
                }
                semantic_attempt.completed_at = utc_now()
            exact_case_error = next(
                (
                    error
                    for error in changeset.validation_errors
                    if error.get("code")
                    in {
                        "attention_case_revision_stale",
                        "attention_case_reply_binding_mismatch",
                        "attention_case_item_revision_mismatch",
                        "attention_case_item_not_open",
                    }
                ),
                None,
            )
            if exact_case_error is not None:
                return {
                    "ok": False,
                    "error": {
                        "code": exact_case_error["code"],
                        "message": (
                            "AttentionCase resolution does not match the current "
                            "visible revision."
                        ),
                        "details": exact_case_error.get("details", {}),
                    },
                    "disposition": "blocked_version",
                    "next": exact_case_error.get("details", {}).get("next"),
                }
            if intent_session.semantic_state == "ready":
                disposition = (
                    "blocked_version"
                    if intent_session.commit_state == "blocked_version"
                    else "rejected_conflict"
                    if intent_session.commit_state == "blocked_conflict"
                    else "rejected_validation"
                )
                return {
                    "ok": False,
                    "ref": changeset.ref_id,
                    "state": intent_session.commit_state,
                    "error": {
                        "code": disposition,
                        "message": (
                            "Resolved intent could not commit because Docket's "
                            "structural or implementation validation failed."
                        ),
                        "details": {"errors": changeset.validation_errors},
                    },
                    "affected_refs": [
                        intent_session.ref_id,
                        turn.ref_id,
                        changeset.ref_id,
                    ],
                    "basis_refs": changeset.basis_refs,
                    "next": None,
                    "warnings": [],
                    "disposition": disposition,
                    "intent_session": intent_service.projection(intent_session),
                    "changeset": self.changesets.projection(changeset),
                }
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
                "disposition": "needs_clarification",
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
        if semantic_request is not None and semantic_attempt is not None:
            semantic_request.authority_availability = "consumed_committed"
            semantic_request.commit_state = "committed"
            semantic_request.committed_changeset_ref = committed.ref_id
            semantic_attempt.state = "committed"
            semantic_attempt.change_set_ref = committed.ref_id
            semantic_attempt.completed_at = committed.committed_at
        partial_case_refs = [
            change.object_ref
            for change in content.resolution_changes
            if getattr(change, "object_type", None) == "attention_case_resolution"
            and getattr(change, "case_outcome", None) == "keep_open"
        ]
        remaining_required: dict[str, list[str]] = {}
        for case_ref in partial_case_refs:
            case = self.session.scalar(
                select(AttentionCase).where(AttentionCase.ref_id == case_ref)
            )
            if case is None:
                continue
            refs = list(
                self.session.scalars(
                    select(CaseItem.ref_id).where(
                        CaseItem.attention_case_id == case.id,
                        CaseItem.status == "open",
                        CaseItem.resolution_role.in_(
                            ("required", "legacy_unspecified")
                        ),
                    )
                )
            )
            if refs:
                remaining_required[case.ref_id] = refs
        next_action = (
            {
                "clarifications": [
                    {
                        "blocking": True,
                        "code": "attention_case_required_items_remaining",
                        "case_items_by_case": remaining_required,
                        "question": (
                            "What should Docket do with the remaining required "
                            "case items?"
                        ),
                    }
                ]
            }
            if remaining_required
            else None
        )
        return {
            "ok": True,
            "ref": committed.ref_id,
            "state": "committed",
            "summary": "Resolved Operator intent committed atomically.",
            "affected_refs": affected_refs,
            "basis_refs": committed.basis_refs,
            "next": next_action,
            "warnings": [],
            "disposition": "committed",
            "intent_session_ref": intent_session.ref_id,
            "intent_turn_ref": turn.ref_id,
        }

    def process_conflict_resolution(
        self,
        *,
        utterance_ref: str,
        request_key: str,
        actor_id: str,
        intent_session_ref: str | None,
        expected_session_version: int | None,
        statement: StatementInput,
        resolution: ConflictResolve,
    ) -> dict[str, Any]:
        utterance = self._authority_utterance(
            utterance_ref=utterance_ref,
            request_key=request_key,
            actor_id=actor_id,
        )
        idempotency_key = f"{request_key}:conflict-resolution"
        replay = self.session.scalar(
            select(ChangeSet).where(ChangeSet.idempotency_key == idempotency_key)
        )
        if replay is not None:
            return {
                "ok": True,
                "ref": replay.ref_id,
                "state": replay.state,
                "summary": "Replayed the existing durable Conflict resolution.",
                "affected_refs": [],
                "basis_refs": replay.basis_refs,
                "next": None,
                "warnings": [],
                "disposition": "replayed_request",
                "intent_session_ref": replay.intent_session_ref,
            }
        intent_service = IntentSessionService(self.session)
        if intent_session_ref is None:
            intent_session, _created = intent_service.open(
                IntentSessionOpen(source_utterance_ref=utterance.ref_id)
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
        active_gateway = GatewayLifetimeService(self.session).current_live(
            "hermes_discord_gateway"
        )
        intent_session, turn = intent_service.append_turn(
            IntentTurnAppend(
                intent_session_ref=intent_session.ref_id,
                utterance_ref=utterance.ref_id,
                statements=[statement],
                relations=[],
                resolved_intent_json={"conflict_ref": resolution.conflict_ref},
                blocking_clarifications=[],
                response_disposition="pending",
                gateway_instance_ref=(
                    active_gateway.ref_id if active_gateway is not None else None
                ),
            )
        )
        intent_session.semantic_state = "ready"
        changeset, affected_refs, created = self.changesets.resolve_conflict(
            intent_session=intent_session,
            request=resolution,
            idempotency_key=idempotency_key,
        )
        return {
            "ok": True,
            "ref": changeset.ref_id,
            "state": changeset.state,
            "summary": "Conflict resolution committed atomically.",
            "affected_refs": affected_refs,
            "basis_refs": changeset.basis_refs,
            "next": None,
            "warnings": [],
            "disposition": "committed" if created else "replayed_request",
            "intent_session_ref": intent_session.ref_id,
            "intent_turn_ref": turn.ref_id,
        }
