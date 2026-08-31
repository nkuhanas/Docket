from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AttentionCase,
    AuditEvent,
    CaseItem,
    ChangeSet,
    IntentSession,
    InterpretedStatement,
    OperatorUtterance,
    PersistedSemanticOption,
    SemanticRequest,
    SemanticRequestAttempt,
)
from docket.models.base import utc_now
from docket.schemas.authority import (
    CURRENT_IMPORT_AUTHORITY_STATEMENT,
    ChangeSetCommit,
    ChangeSetContent,
    ChangeSetPrepare,
    ChangeSetRevise,
    ConflictResolve,
    IntentSessionOpen,
    IntentTurnAppend,
    OperatorChangeSetContent,
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
from docket.services.registry_conflicts import (
    IdentityBindingConflictCompiler,
    RegistryConflictCompiler,
    TemporalBindingConflictCompiler,
)
from docket.services.reply_bindings import ReplyBindingService
from docket.services.semantic_options import (
    CURRENT_SELECTION_UTTERANCE,
    SemanticOptionService,
    complete_selection_provenance,
    semantic_authority_scope,
)
from docket.services.tracked_context import TrackedContextService


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
        tracked_context_handlers = TrackedContextService(session).handlers()
        case_resolution_handlers = AttentionCaseResolutionService(session).handlers()
        provider_service = ProviderIntentService(session)
        self.changesets = ChangeSetService(
            session,
            handlers={
                **registry_handlers,
                **policy_handlers,
                **event_handlers,
                **tracked_context_handlers,
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

    @staticmethod
    def _freeform_scope(
        content: ChangeSetContent,
        resolved_intent_json: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "resolved_intent": resolved_intent_json,
            **semantic_authority_scope(
                content.model_dump(mode="json", exclude={"provider_intents"}), []
            ),
        }

    @staticmethod
    def _freeform_preconditions(
        content: ChangeSetContent,
        intent_session: IntentSession,
    ) -> dict[str, Any]:
        return {
            "expected_versions": content.expected_versions,
            "case_refs": list(intent_session.case_refs),
            "case_revision_refs": list(intent_session.case_revision_refs),
        }

    def _ensure_freeform_semantic_request(
        self,
        *,
        utterance: OperatorUtterance,
        request_key: str,
        intent_session: IntentSession,
        content: ChangeSetContent,
        resolved_intent_json: dict[str, Any],
    ) -> SemanticRequest:
        scope = self._freeform_scope(content, resolved_intent_json)
        authority_scope_hash = sha256_json(scope)
        preconditions = self._freeform_preconditions(content, intent_session)
        precondition_hash = sha256_json(preconditions)
        semantic_request = self.session.scalar(
            select(SemanticRequest).where(
                SemanticRequest.intent_session_ref == intent_session.ref_id,
                SemanticRequest.authority_scope_hash == authority_scope_hash,
            )
        )
        if semantic_request is None:
            semantic_request = SemanticRequest(
                intent_session_id=intent_session.id,
                intent_session_ref=intent_session.ref_id,
                authority_scope_hash=authority_scope_hash,
                current_precondition_hash=precondition_hash,
                origin_utterance_refs=[utterance.ref_id],
                selected_option_binding={
                    "kind": "freeform_turn",
                    "request_key": request_key,
                    "scope": scope,
                },
                authority_availability="available",
                commit_state="not_attempted",
                current_case_revision_ref=(
                    intent_session.case_revision_refs[0]
                    if len(intent_session.case_revision_refs) == 1
                    else None
                ),
                symbolic_substitutions_json={},
            )
            self.session.add(semantic_request)
            self.session.flush()
            self.session.add(
                AuditEvent(
                    event_type="semantic_request.created",
                    entity_type="semantic_request",
                    entity_id=semantic_request.id,
                    actor_type="operator",
                    actor_id=get_settings().operator_discord_user_id,
                    request_id=None,
                    primary_ref=semantic_request.ref_id,
                    affected_refs=[semantic_request.ref_id, intent_session.ref_id],
                    basis_refs=[utterance.ref_id],
                    data={
                        "kind": "freeform_turn",
                        "authority_scope_hash": authority_scope_hash,
                        "precondition_hash": precondition_hash,
                    },
                )
            )
        elif utterance.ref_id not in semantic_request.origin_utterance_refs:
            raise DocketError(
                code="semantic_request_binding_mismatch",
                message="Freeform retry does not originate from the authorized utterance.",
            )
        if semantic_request.current_precondition_hash != precondition_hash:
            old_precondition_hash = semantic_request.current_precondition_hash
            semantic_request.current_precondition_hash = precondition_hash
            semantic_request.current_case_revision_ref = (
                intent_session.case_revision_refs[0]
                if len(intent_session.case_revision_refs) == 1
                else None
            )
            self.session.add(
                AuditEvent(
                    event_type="semantic_request.safely_rebased",
                    entity_type="semantic_request",
                    entity_id=semantic_request.id,
                    actor_type="docket_compiler",
                    actor_id=None,
                    request_id=None,
                    primary_ref=semantic_request.ref_id,
                    affected_refs=[semantic_request.ref_id, intent_session.ref_id],
                    basis_refs=list(semantic_request.origin_utterance_refs),
                    data={
                        "original_authority_scope_hash": authority_scope_hash,
                        "old_precondition_hash": old_precondition_hash,
                        "new_precondition_hash": precondition_hash,
                        "semantic_scope_changed": False,
                    },
                )
            )
        intent_session.semantic_request_ref = semantic_request.ref_id
        return semantic_request

    def _start_semantic_attempt(
        self,
        *,
        semantic_request: SemanticRequest,
        gateway_instance_ref: str | None,
    ) -> SemanticRequestAttempt:
        next_attempt = int(
            self.session.scalar(
                select(func.max(SemanticRequestAttempt.attempt_number)).where(
                    SemanticRequestAttempt.semantic_request_id == semantic_request.id
                )
            )
            or 0
        ) + 1
        attempt = SemanticRequestAttempt(
            semantic_request_id=semantic_request.id,
            semantic_request_ref=semantic_request.ref_id,
            attempt_number=next_attempt,
            authority_scope_hash=semantic_request.authority_scope_hash,
            precondition_hash=semantic_request.current_precondition_hash,
            case_revision_ref=semantic_request.current_case_revision_ref,
            gateway_instance_ref=gateway_instance_ref,
            state="pending",
        )
        self.session.add(attempt)
        semantic_request.commit_state = "pending"
        return attempt

    def _complete_import_statement_basis(
        self,
        *,
        content: ChangeSetContent,
        turn_statement_refs: list[str],
    ) -> ChangeSetContent:
        scope = content.import_scope
        if scope is None:
            return content
        statements = list(
            self.session.scalars(
                select(InterpretedStatement).where(
                    InterpretedStatement.ref_id.in_(turn_statement_refs)
                )
            )
        )
        source_statement_refs = [
            statement.ref_id
            for statement in statements
            if statement.source_ref in set(scope.source_refs)
        ]
        authority_statement_refs = list(scope.authority_statement_refs)
        if CURRENT_IMPORT_AUTHORITY_STATEMENT in authority_statement_refs:
            expected_effects = sorted(scope.authorized_effects)
            candidates = [
                statement.ref_id
                for statement in statements
                if statement.source_ref is None
                and statement.statement_kind == "operator_intent"
                and statement.predicate == "import_effect_authority"
                and statement.value_json
                == {"authorized_effects": expected_effects}
            ]
            if len(candidates) == 1:
                authority_statement_refs = [
                    candidates[0]
                    if ref == CURRENT_IMPORT_AUTHORITY_STATEMENT
                    else ref
                    for ref in authority_statement_refs
                ]

        completed_statement_refs = list(
            dict.fromkeys([*source_statement_refs, *authority_statement_refs])
        )
        payload = content.model_dump(mode="json")
        payload["basis_refs"] = list(
            dict.fromkeys([*content.basis_refs, *completed_statement_refs])
        )
        payload["import_scope"]["authority_statement_refs"] = authority_statement_refs
        for group_name in (
            "registry_changes",
            "preference_changes",
            "lane_changes",
            "event_changes",
            "tracked_context_changes",
            "resolution_changes",
        ):
            for change in payload[group_name]:
                change["basis_refs"] = list(
                    dict.fromkeys([*change["basis_refs"], *completed_statement_refs])
                )
        return ChangeSetContent.model_validate(payload)

    @staticmethod
    def _compile_import_authority_statement(
        *,
        content: ChangeSetContent | None,
        statements: list[StatementInput],
    ) -> list[StatementInput]:
        if (
            content is None
            or content.import_scope is None
            or content.import_scope.mode != "operator_explicit"
            or CURRENT_IMPORT_AUTHORITY_STATEMENT
            not in content.import_scope.authority_statement_refs
        ):
            return statements
        expected_effects = sorted(content.import_scope.authorized_effects)
        if any(
            statement.source_ref is None
            and statement.statement_kind == "operator_intent"
            and statement.predicate == "import_effect_authority"
            and statement.value_json == {"authorized_effects": expected_effects}
            for statement in statements
        ):
            return statements
        return [
            *statements,
            StatementInput(
                statement_kind="operator_intent",
                subject_refs=list(content.import_scope.source_refs),
                predicate="import_effect_authority",
                value_json={"authorized_effects": expected_effects},
                affected_fields=["import_scope"],
                interpretation_json={"compiler": "operator_import_scope"},
                interpreter_version="docket.operator-import-scope.v1",
            ),
        ]

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
                or utterance.ref_id not in semantic_request.origin_utterance_refs
                or semantic_request.authority_availability != "available"
            ):
                raise DocketError(
                    code="semantic_request_binding_mismatch",
                    message="Execution does not match the available selected authority scope.",
                )
            binding = semantic_request.selected_option_binding or {}
            if binding.get("kind") == "freeform_turn":
                bound_session = self.session.get(
                    IntentSession, semantic_request.intent_session_id
                )
                if bound_session is None:
                    raise DocketError(
                        code="intent_session_not_found",
                        message="SemanticRequest lost its IntentSession binding.",
                    )
                calculated_scope_hash = sha256_json(
                    self._freeform_scope(content, resolved_intent_json)
                )
                if calculated_scope_hash != semantic_request.authority_scope_hash:
                    raise DocketError(
                        code="semantic_request_scope_mismatch",
                        message=(
                            "Submitted ChangeSet changes the authorized freeform "
                            "semantic scope."
                        ),
                    )
                if precondition_hash != semantic_request.current_precondition_hash:
                    raise DocketError(
                        code="semantic_request_binding_mismatch",
                        message="Freeform retry did not name the current preconditions.",
                    )
                semantic_request = self._ensure_freeform_semantic_request(
                    utterance=utterance,
                    request_key=request_key,
                    intent_session=bound_session,
                    content=content,
                    resolved_intent_json=resolved_intent_json,
                )
                authority_scope_hash = semantic_request.authority_scope_hash
                precondition_hash = semantic_request.current_precondition_hash
            else:
                if semantic_request.current_precondition_hash != precondition_hash:
                    raise DocketError(
                        code="semantic_request_binding_mismatch",
                        message="Selection execution preconditions no longer match.",
                    )
                option = self.session.scalar(
                    select(PersistedSemanticOption).where(
                        PersistedSemanticOption.ref_id
                        == binding.get(
                            "execution_option_ref",
                            binding.get("selected_option_ref"),
                        ),
                        PersistedSemanticOption.option_id == binding.get("option_id"),
                        PersistedSemanticOption.authority_scope_hash
                        == authority_scope_hash,
                        PersistedSemanticOption.precondition_hash == precondition_hash,
                    )
                )
                if option is None or complete_selection_provenance(
                    option.compilation_template_json, utterance.ref_id
                ) != content.model_dump(
                    mode="json", exclude={"provider_intents"}
                ):
                    raise DocketError(
                        code="semantic_request_scope_mismatch",
                        message=(
                            "Submitted ChangeSet differs from the exact persisted "
                            "option scope."
                        ),
                    )
            semantic_attempt = self._start_semantic_attempt(
                semantic_request=semantic_request,
                gateway_instance_ref=gateway_instance_ref,
            )
        replay = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.idempotency_key == f"{request_key}:changeset"
            )
        )
        if replay is not None and replay.state == "committed":
            replay_request = (
                self.session.scalar(
                    select(SemanticRequest).where(
                        SemanticRequest.ref_id == replay.semantic_request_ref
                    )
                )
                if replay.semantic_request_ref is not None
                else None
            )
            return {
                "ok": True,
                "ref": replay.ref_id,
                "state": replay.state,
                "summary": "Replayed the existing durable ChangeSet result.",
                "affected_refs": [],
                "basis_refs": replay.basis_refs,
                "next": None,
                "warnings": [],
                "disposition": "replayed_request",
                "intent_session_ref": replay.intent_session_ref,
                "semantic_request_ref": replay.semantic_request_ref,
                "authority_scope_hash": (
                    replay_request.authority_scope_hash
                    if replay_request is not None
                    else None
                ),
                "precondition_hash": (
                    replay_request.current_precondition_hash
                    if replay_request is not None
                    else None
                ),
            }
        if (
            replay is not None
            and changeset_ref is None
            and semantic_request is None
        ):
            replay_session = self.session.get(IntentSession, replay.intent_session_id)
            commit_state = (
                replay_session.commit_state
                if replay_session is not None
                else "unknown"
            )
            disposition = (
                "blocked_version"
                if commit_state == "blocked_version"
                else "rejected_conflict"
                if commit_state == "blocked_conflict"
                else "rejected_validation"
                if replay.validation_errors
                else "unknown"
            )
            replay_request = (
                self.session.scalar(
                    select(SemanticRequest).where(
                        SemanticRequest.ref_id == replay.semantic_request_ref
                    )
                )
                if replay.semantic_request_ref is not None
                else None
            )
            return {
                "ok": False,
                "ref": replay.ref_id,
                "state": commit_state,
                "error": {
                    "code": disposition,
                    "message": "The existing durable ChangeSet did not commit.",
                    "details": {"errors": replay.validation_errors},
                },
                "affected_refs": [replay.ref_id, replay.intent_session_ref],
                "basis_refs": replay.basis_refs,
                "next": {
                    "action": "revise_failed_changeset",
                    "changeset_ref": replay.ref_id,
                    "expected_changeset_version": replay.version,
                },
                "warnings": ["duplicate_delivery_of_failed_request"],
                "disposition": disposition,
                "intent_session_ref": replay.intent_session_ref,
                "semantic_request_ref": replay.semantic_request_ref,
                "authority_scope_hash": (
                    replay_request.authority_scope_hash
                    if replay_request is not None
                    else None
                ),
                "precondition_hash": (
                    replay_request.current_precondition_hash
                    if replay_request is not None
                    else None
                ),
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
        if (
            semantic_request is None
            and content is not None
            and not blocking_clarifications
        ):
            if replay is not None and replay.semantic_request_ref is not None:
                semantic_request = self.session.scalar(
                    select(SemanticRequest).where(
                        SemanticRequest.ref_id == replay.semantic_request_ref
                    )
                )
                if semantic_request is None:
                    raise DocketError(
                        code="semantic_request_not_found",
                        message="Failed ChangeSet lost its SemanticRequest lineage.",
                    )
            semantic_request = self._ensure_freeform_semantic_request(
                utterance=utterance,
                request_key=request_key,
                intent_session=intent_session,
                content=content,
                resolved_intent_json=resolved_intent_json,
            )
            authority_scope_hash = semantic_request.authority_scope_hash
            precondition_hash = semantic_request.current_precondition_hash
            semantic_attempt = self._start_semantic_attempt(
                semantic_request=semantic_request,
                gateway_instance_ref=gateway_instance_ref,
            )
        if semantic_request is not None and content is not None:
            selected_binding_statements = IdentityBindingConflictCompiler(
                self.session
            ).selected_statements(content)
            selected_temporal_statements = TemporalBindingConflictCompiler(
                self.session
            ).selected_statements(content)
            known_binding_subjects = {
                subject_ref
                for statement in statements
                if statement.predicate == "identity_binding"
                for subject_ref in statement.subject_refs
            }
            statements = [
                *statements,
                *[
                    statement
                    for statement in selected_binding_statements
                    if not known_binding_subjects.intersection(statement.subject_refs)
                ],
                *selected_temporal_statements,
            ]
        statements = self._compile_import_authority_statement(
            content=content,
            statements=statements,
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
                    and (semantic_request.selected_option_binding or {}).get("kind")
                    != "freeform_turn"
                    else {}
                ),
                gateway_instance_ref=gateway_instance_ref,
            )
        )
        if content is not None:
            content = self._complete_import_statement_basis(
                content=content,
                turn_statement_refs=list(turn.statement_refs),
            )
        if semantic_options:
            completed_options: list[SemanticOptionDraft] = []
            for draft in semantic_options:
                completed_content = self._complete_import_statement_basis(
                    content=draft.content.to_internal(),
                    turn_statement_refs=list(turn.statement_refs),
                )
                completed_options.append(
                    draft.model_copy(
                        update={
                            "content": OperatorChangeSetContent.from_internal(
                                completed_content
                            )
                        }
                    )
                )
            semantic_options = completed_options
        conflict_refs = list(
            dict.fromkeys(
                [
                    *RegistryConflictCompiler(self.session).compile(
                        list(turn.statement_refs)
                    ),
                    *IdentityBindingConflictCompiler(self.session).compile(
                        list(turn.statement_refs)
                    ),
                    *TemporalBindingConflictCompiler(self.session).compile(
                        list(turn.statement_refs)
                    ),
                ]
            )
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
            intent_session.semantic_state = "needs_clarification"
            intent_session.commit_state = "blocked_conflict"
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
                            "kind": (
                                semantic_request.selected_option_binding or {}
                            ).get("kind", "selected_option"),
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
                    semantic_request_ref=(
                        semantic_request.ref_id
                        if semantic_request is not None
                        else None
                    ),
                    authority_scope_hash=authority_scope_hash,
                    precondition_hash=precondition_hash,
                    execution_binding=(
                        {
                            "selection_utterance_ref": utterance.ref_id,
                            "case_revision_ref": (
                                semantic_request.current_case_revision_ref
                            ),
                            "kind": (
                                semantic_request.selected_option_binding or {}
                            ).get("kind", "selected_option"),
                        }
                        if semantic_request is not None
                        else {}
                    ),
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
            selected_conflict_error = next(
                (
                    error
                    for error in changeset.validation_errors
                    if error.get("code") == "open_conflict"
                ),
                None,
            )
            if (
                semantic_request is not None
                and (semantic_request.selected_option_binding or {}).get("kind")
                != "freeform_turn"
                and selected_conflict_error is not None
            ):
                conflict_ref = str(
                    selected_conflict_error.get("details", {}).get("conflict_ref", "")
                )
                return {
                    "ok": False,
                    "ref": changeset.ref_id,
                    "state": "blocked_conflict",
                    "error": {
                        "code": "identity_binding_conflict",
                        "message": (
                            "Canonical state changed after the option was projected; "
                            "the selected identity binding now conflicts with the "
                            "current binding."
                        ),
                        "details": {
                            "conflict_ref": conflict_ref,
                            "selected_authority_preserved": True,
                        },
                    },
                    "affected_refs": [
                        intent_session.ref_id,
                        turn.ref_id,
                        changeset.ref_id,
                        *([conflict_ref] if conflict_ref else []),
                    ],
                    "basis_refs": changeset.basis_refs,
                    "next": {
                        "clarifications": intent_session.blocking_clarifications,
                        "action": "resolve_changed_identity_binding",
                    },
                    "warnings": [],
                    "disposition": "rejected_conflict",
                    "semantic_request_ref": (
                        semantic_request.ref_id
                        if semantic_request is not None
                        else None
                    ),
                    "authority_scope_hash": authority_scope_hash,
                    "precondition_hash": precondition_hash,
                    "intent_session": intent_service.projection(intent_session),
                    "changeset": self.changesets.projection(changeset),
                }
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
                    "semantic_request_ref": (
                        semantic_request.ref_id
                        if semantic_request is not None
                        else None
                    ),
                    "authority_scope_hash": authority_scope_hash,
                    "precondition_hash": precondition_hash,
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
                    "semantic_request_ref": (
                        semantic_request.ref_id
                        if semantic_request is not None
                        else None
                    ),
                    "authority_scope_hash": authority_scope_hash,
                    "precondition_hash": precondition_hash,
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
                "semantic_request_ref": (
                    semantic_request.ref_id if semantic_request is not None else None
                ),
                "authority_scope_hash": authority_scope_hash,
                "precondition_hash": precondition_hash,
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
            "semantic_request_ref": (
                semantic_request.ref_id if semantic_request is not None else None
            ),
            "authority_scope_hash": authority_scope_hash,
            "precondition_hash": precondition_hash,
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
