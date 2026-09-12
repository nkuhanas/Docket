from __future__ import annotations

import base64
import json
import secrets
from collections import Counter
from typing import Any, ClassVar, Literal, cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import is_public_ref
from docket.models import (
    AssemblyExecution,
    AssemblyOperation,
    AuditEvent,
    CalendarLane,
    ChangeSet,
    ChangeSetRevision,
    IntentSession,
    IntentTurn,
    InterpretedStatement,
    OperatorUtterance,
    SemanticRequest,
    SemanticRequestAttempt,
    ToolInvocation,
)
from docket.models.base import utc_now
from docket.schemas.assembly import (
    AssemblyAuthorityScopeInput,
    NormalizedEntryInput,
    ReviewChangesInput,
    ScheduledOccurrenceEntry,
    StageActionRemove,
    StageActionUpsert,
    StageChangesInput,
    StageNormalizedEntryRemove,
    StageNormalizedEntryUpsert,
)
from docket.schemas.authority import (
    ChangeSetCommit,
    ChangeSetContent,
    ImportEffect,
    ImportScope,
    IntentSessionOpen,
    StatementInput,
)
from docket.services.changeset_compiler import (
    COMPILER_IDENTIFIER,
    COMPILER_VERSION,
    compile_normalized_entry,
    entry_action_ids,
    entry_facets,
    entry_mutation_types,
    normalized_entry_record,
)
from docket.services.changeset_diff import bounded_details, bounded_sample, draft_diff
from docket.services.changeset_pins import effect_hash, migration_required, pin_snapshot
from docket.services.intent_sessions import IntentSessionService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.reply_bindings import ReplyBindingService
from docket.services.statements import StatementService

MAX_DRAFT_ENTRIES = 250
MAX_DRAFT_REVISIONS = 1_000
MAX_CANONICAL_ACTIONS = 1_000
MAX_PROVIDER_OPERATIONS = 500

_GROUP_FOR_OBJECT_TYPE = {
    "entity": "registry_changes",
    "identity_handle": "registry_changes",
    "identity_binding": "registry_changes",
    "affiliation": "registry_changes",
    "relationship": "registry_changes",
    "fact": "registry_changes",
    "interaction": "registry_changes",
    "preference": "preference_changes",
    "calendar_lane": "lane_changes",
    "lane_routing_decision": "lane_changes",
    "canonical_event": "event_changes",
    "item": "tracked_context_changes",
    "temporal_binding": "tracked_context_changes",
    "task": "tracked_context_changes",
    "temporal_calendar_projection": "tracked_context_changes",
    "reminder_plan": "tracked_context_changes",
    "attention_case_resolution": "resolution_changes",
}

_SNAPSHOT_GROUPS = (
    "registry_changes",
    "preference_changes",
    "lane_changes",
    "event_changes",
    "tracked_context_changes",
    "resolution_changes",
    "provider_intents",
)


def _cursor_encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DocketError(
            code="invalid_review_cursor",
            message="Review cursor is malformed.",
        ) from exc
    if not isinstance(payload, dict):
        raise DocketError(code="invalid_review_cursor", message="Review cursor is malformed.")
    return payload


def _diagnostic_projection(
    *, changeset_ref: str, revision: int, errors: list[dict[str, Any]]
) -> dict[str, Any]:
    """A bounded receipt sample plus a read of this exact immutable revision.

    A diagnostic may itself exceed the sample budget. Keep it in the revision
    and return a cursor, never fail the saved operation or imply zero errors.
    """
    sample = bounded_sample(errors)
    result: dict[str, Any] = {
        "diagnostic_count": len(errors),
        "diagnostic_sample": sample,
        "omitted_diagnostic_count": len(errors) - len(sample),
    }
    if errors:
        result["diagnostic_review"] = {
            "tool": "docket_review_changeset",
            "arguments": {
                "view": "diagnostics",
                "cursor": _cursor_encode({
                    "format_version": 1,
                    "changeset_ref": changeset_ref,
                    "revision": revision,
                    "view": "diagnostics",
                    "mutation_types": [],
                    "entry_types": [],
                    "position": 0,
                }),
            },
        }
    return result


class ChangeSetAssemblyAdmissionService:
    """Persist infrastructure-owned ordering and retry identity before MCP delivery."""

    _KINDS: ClassVar[dict[str, str]] = {
        "docket_stage_changes": "stage",
        "docket_review_changeset": "review",
        "docket_commit_changeset": "commit",
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    def admit(
        self,
        *,
        utterance_ref: str,
        trace_ref: str,
        upstream_tool_call_id: str,
        trace_ordinal: int,
        tool_name: str,
        argument_hash: str,
        guild_id: str,
        channel_id: str,
        source_message_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        operation_kind = self._KINDS.get(tool_name)
        if operation_kind is None:
            raise DocketError(
                code="assembly_tool_invalid",
                message="This tool does not participate in ChangeSet assembly.",
            )
        utterance = self.session.scalar(
            select(OperatorUtterance).where(OperatorUtterance.ref_id == utterance_ref)
        )
        expected_request_key = f"discord:{guild_id}:{channel_id}:{source_message_id}:0"
        if (
            utterance is None
            or utterance.request_key != expected_request_key
            or utterance.actor_ref != f"discord_user:{actor_id}"
            or actor_id != get_settings().operator_discord_user_id
        ):
            raise DocketError(
                code="assembly_authority_mismatch",
                message="Assembly admission does not match the authenticated utterance.",
            )
        existing = self.session.scalar(
            select(AssemblyOperation).where(
                AssemblyOperation.source_utterance_ref == utterance.ref_id,
                AssemblyOperation.upstream_tool_call_id == upstream_tool_call_id,
            )
        )
        if existing is not None:
            if existing.tool_name != tool_name or existing.argument_hash != argument_hash:
                raise DocketError(
                    code="stage_idempotency_mismatch",
                    message="An upstream tool-call identity was reused with different content.",
                )
            return {
                "ok": True,
                "assembly_operation_token": existing.operation_key,
                "canonical_model_argument_hash": existing.argument_hash,
                "attempt_sequence": existing.attempt_sequence,
                "replayed": True,
            }
        execution = self.session.scalar(
            select(AssemblyExecution)
            .where(
                AssemblyExecution.source_utterance_ref == utterance.ref_id,
                AssemblyExecution.trace_ref == trace_ref,
            )
            .with_for_update()
        )
        if execution is None:
            execution = AssemblyExecution(
                source_utterance_ref=utterance.ref_id,
                trace_ref=trace_ref,
                next_sequence=1,
            )
            self.session.add(execution)
            self.session.flush()
            resumable = [
                request
                for request in self.session.scalars(select(SemanticRequest))
                if utterance.ref_id in request.origin_utterance_refs
                and (request.selected_option_binding or {}).get("kind") == "freeform_assembly"
                and request.authority_availability in {"available", "consumed_committed"}
            ]
            if len(resumable) > 1:
                raise DocketError(
                    code="assembly_resume_ambiguous",
                    message="More than one assembly request is bound to this utterance.",
                )
            if resumable:
                semantic_request = resumable[0]
                attempt = self.session.scalar(
                    select(SemanticRequestAttempt).where(
                        SemanticRequestAttempt.semantic_request_id == semantic_request.id,
                        SemanticRequestAttempt.execution_trace_ref == trace_ref,
                    )
                )
                if attempt is None:
                    next_attempt = (
                        int(
                            self.session.scalar(
                                select(func.max(SemanticRequestAttempt.attempt_number)).where(
                                    SemanticRequestAttempt.semantic_request_id
                                    == semantic_request.id
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    attempt = SemanticRequestAttempt(
                        semantic_request_id=semantic_request.id,
                        semantic_request_ref=semantic_request.ref_id,
                        attempt_number=next_attempt,
                        authority_scope_hash=semantic_request.authority_scope_hash,
                        precondition_hash=semantic_request.current_precondition_hash,
                        case_revision_ref=semantic_request.current_case_revision_ref,
                        execution_trace_ref=trace_ref,
                        state="pending",
                    )
                    self.session.add(attempt)
                    self.session.flush()
                execution.semantic_request_ref = semantic_request.ref_id
                execution.semantic_request_attempt_ref = attempt.ref_id
        attempt = (
            self.session.scalar(
                select(SemanticRequestAttempt).where(
                    SemanticRequestAttempt.ref_id == execution.semantic_request_attempt_ref
                )
            )
            if execution.semantic_request_attempt_ref is not None
            else None
        )
        token = secrets.token_hex(32)
        operation = AssemblyOperation(
            assembly_execution_id=execution.id,
            source_utterance_ref=utterance.ref_id,
            trace_ref=trace_ref,
            upstream_tool_call_id=upstream_tool_call_id,
            operation_key=token,
            operation_kind=operation_kind,
            tool_name=tool_name,
            argument_hash=argument_hash,
            patch_hash=argument_hash if operation_kind == "stage" else None,
            attempt_sequence=execution.next_sequence,
            causal_observed_revision=(
                attempt.observed_draft_revision if attempt is not None else None
            ),
            semantic_request_ref=execution.semantic_request_ref,
            semantic_request_attempt_ref=execution.semantic_request_attempt_ref,
            state="admitted",
        )
        execution.next_sequence += 1
        self.session.add(operation)
        self.session.flush()
        return {
            "ok": True,
            "assembly_operation_token": token,
            "canonical_model_argument_hash": argument_hash,
            "attempt_sequence": operation.attempt_sequence,
            "trace_ordinal": trace_ordinal,
            "replayed": False,
        }


class ChangeSetAssemblyService:
    """Incrementally assemble one authorized ChangeSet without model-held bookkeeping."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.authority = InteractiveAuthorityService(session)
        self.changesets = self.authority.changesets

    def _reconcile_terminal_predecessors(
        self,
        *,
        execution: AssemblyExecution,
        before_sequence: int,
    ) -> None:
        """Recover admitted predecessors whose authenticated call is terminal."""

        predecessors = list(
            self.session.scalars(
                select(AssemblyOperation)
                .where(
                    AssemblyOperation.assembly_execution_id == execution.id,
                    AssemblyOperation.attempt_sequence < before_sequence,
                    AssemblyOperation.state.in_(("admitted", "running", "unknown")),
                )
                .order_by(AssemblyOperation.attempt_sequence)
                .with_for_update()
            )
        )
        for predecessor in predecessors:
            invocation = self.session.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.trace_ref == predecessor.trace_ref,
                    ToolInvocation.trace_call_id == predecessor.upstream_tool_call_id,
                )
            )
            if invocation is None or invocation.transport_state == "running":
                continue
            if invocation.domain_state == "rejected":
                disposition = invocation.result_disposition or "rejected_validation"
                self._terminal(
                    predecessor,
                    {
                        "ok": False,
                        "disposition": disposition,
                        "error": {
                            "code": invocation.error_code or "rejected_validation",
                            "message": (
                                "The earlier admitted operation was rejected before "
                                "draft execution."
                            ),
                            "details": {"authority_preserved": True},
                        },
                        "reconciled": True,
                    },
                    state="rejected",
                )
                continue
            predecessor.state = "unknown"
            self._replay(predecessor)

    def _operation(
        self,
        *,
        token: str,
        argument_hash: str,
        operation_kind: Literal["stage", "review", "commit"],
        utterance_ref: str,
    ) -> tuple[AssemblyOperation, AssemblyExecution]:
        operation = self.session.scalar(
            select(AssemblyOperation)
            .where(AssemblyOperation.operation_key == token)
            .with_for_update()
        )
        if (
            operation is None
            or operation.operation_kind != operation_kind
            or operation.source_utterance_ref != utterance_ref
        ):
            raise DocketError(
                code="assembly_operation_invalid",
                message="The infrastructure assembly binding is missing or mismatched.",
            )
        if argument_hash != operation.argument_hash:
            raise DocketError(
                code="stage_idempotency_mismatch",
                message="The admitted assembly operation does not match this request.",
            )
        execution = self.session.scalar(
            select(AssemblyExecution)
            .where(AssemblyExecution.id == operation.assembly_execution_id)
            .with_for_update()
        )
        if execution is None:
            raise DocketError(
                code="assembly_execution_missing",
                message="Assembly operation lost its durable execution binding.",
            )
        self._reconcile_terminal_predecessors(
            execution=execution,
            before_sequence=operation.attempt_sequence,
        )
        earlier_pending = self.session.scalar(
            select(func.count(AssemblyOperation.id)).where(
                AssemblyOperation.assembly_execution_id == execution.id,
                AssemblyOperation.attempt_sequence < operation.attempt_sequence,
                AssemblyOperation.state.in_(("admitted", "running", "unknown")),
            )
        )
        if int(earlier_pending or 0) > 0:
            raise DocketError(
                code="assembly_operation_out_of_order",
                message="An earlier assembly operation has not reached a durable outcome.",
            )
        return operation, execution

    @staticmethod
    def _terminal(
        operation: AssemblyOperation,
        result: dict[str, Any],
        *,
        state: Literal["completed", "rejected"] = "completed",
    ) -> dict[str, Any]:
        operation.state = state
        operation.result_disposition = str(result.get("disposition") or "unknown")[:64]
        operation.result_json = result
        operation.completed_at = utc_now()
        return result

    def _replay(self, operation: AssemblyOperation) -> dict[str, Any] | None:
        if operation.state not in {"completed", "rejected"}:
            if operation.state != "unknown":
                return None
            changeset = (
                self.session.scalar(
                    select(ChangeSet).where(ChangeSet.ref_id == operation.change_set_ref)
                )
                if operation.change_set_ref is not None
                else None
            )
            if changeset is not None and changeset.state == "committed":
                return self._terminal(
                    operation,
                    {**changeset.commit_receipt_json, "reconciled": True},
                )
            revision = self.session.scalar(
                select(ChangeSetRevision).where(
                    ChangeSetRevision.assembly_operation_id == operation.id
                )
            )
            if revision is not None:
                return self._terminal(
                    operation,
                    {
                        "ok": True,
                        "disposition": "saved_with_errors"
                        if revision.validation_errors_json
                        else "ready_to_commit",
                        "draft_ref": changeset.ref_id if changeset is not None else None,
                        "current_revision": revision.revision,
                        "assembly_ready": not revision.validation_errors_json,
                        "diagnostic_count": len(revision.validation_errors_json),
                        "reconciled": True,
                        "next": {"action": "review_changeset"},
                    },
                )
            return self._terminal(
                operation,
                {
                    "ok": False,
                    "disposition": "unknown",
                    "error": {
                        "code": "assembly_operation_interrupted",
                        "message": (
                            "The interrupted operation has no durable draft or commit outcome."
                        ),
                        "details": {"authority_preserved": True},
                    },
                    "reconciled": True,
                    "next": {"action": "review_changeset"},
                },
                state="rejected",
            )
        return {
            **operation.result_json,
            "replayed": True,
        }

    def reject_admitted_operation(
        self,
        *,
        token: str,
        argument_hash: str,
        operation_kind: Literal["stage", "review", "commit"],
        utterance_ref: str,
        error: DocketError,
    ) -> dict[str, Any] | None:
        """Persist an admitted domain rejection in the operation's transaction."""

        operation = self.session.scalar(
            select(AssemblyOperation)
            .where(AssemblyOperation.operation_key == token)
            .with_for_update()
        )
        if (
            operation is None
            or operation.argument_hash != argument_hash
            or operation.operation_kind != operation_kind
            or operation.source_utterance_ref != utterance_ref
            or operation.state in {"completed", "rejected"}
        ):
            return None
        result = {
            **error.as_dict(),
            "disposition": "rejected_validation",
            "authority_preserved": True,
        }
        return self._terminal(operation, result, state="rejected")

    def _authority_utterance(self, request: StageChangesInput) -> OperatorUtterance:
        return self.authority._authority_utterance(
            utterance_ref=request.utterance_ref,
            request_key=request.request_key,
            actor_id=str(get_settings().operator_discord_user_id),
        )

    def _open_session(self, utterance: OperatorUtterance) -> IntentSession:
        reply_binding = ReplyBindingService(self.session).resolve(utterance) or {}
        intent_session, _created = IntentSessionService(self.session).open(
            IntentSessionOpen(
                source_utterance_ref=utterance.ref_id,
                case_refs=reply_binding.get("case_refs", []),
                case_revision_refs=reply_binding.get("case_revision_refs", []),
                brief_ref=reply_binding.get("brief_ref"),
                trusted_context_refs=reply_binding.get("trusted_context_refs", []),
            )
        )
        return intent_session

    def _bind_request_and_attempt(
        self,
        *,
        execution: AssemblyExecution,
        operation: AssemblyOperation,
        utterance: OperatorUtterance,
        scope: AssemblyAuthorityScopeInput | None,
        expected_versions: dict[str, int],
    ) -> tuple[IntentSession, SemanticRequest, SemanticRequestAttempt]:
        if execution.semantic_request_ref is None:
            if scope is None:
                raise DocketError(
                    code="assembly_scope_required",
                    message="The first stage operation requires the exact assembly scope.",
                )
            intent_session = self._open_session(utterance)
            scope_payload = scope.model_dump(mode="json", exclude_none=True)
            authority_scope_hash = sha256_json(scope_payload)
            precondition_payload = {
                "expected_versions": expected_versions,
                "case_refs": intent_session.case_refs,
                "case_revision_refs": intent_session.case_revision_refs,
            }
            precondition_hash = sha256_json(precondition_payload)
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
                        "kind": "freeform_assembly",
                        "scope": scope_payload,
                    },
                    authority_availability="available",
                    commit_state="pending",
                    current_case_revision_ref=(
                        intent_session.case_revision_refs[0]
                        if len(intent_session.case_revision_refs) == 1
                        else None
                    ),
                    symbolic_substitutions_json={},
                )
                self.session.add(semantic_request)
                self.session.flush()
            elif utterance.ref_id not in semantic_request.origin_utterance_refs:
                raise DocketError(
                    code="semantic_request_binding_mismatch",
                    message="This utterance cannot adopt another request's assembly draft.",
                )
            existing_attempt = self.session.scalar(
                select(SemanticRequestAttempt).where(
                    SemanticRequestAttempt.semantic_request_id == semantic_request.id,
                    SemanticRequestAttempt.execution_trace_ref == execution.trace_ref,
                )
            )
            if existing_attempt is None:
                next_attempt = (
                    int(
                        self.session.scalar(
                            select(func.max(SemanticRequestAttempt.attempt_number)).where(
                                SemanticRequestAttempt.semantic_request_id == semantic_request.id
                            )
                        )
                        or 0
                    )
                    + 1
                )
                existing_attempt = SemanticRequestAttempt(
                    semantic_request_id=semantic_request.id,
                    semantic_request_ref=semantic_request.ref_id,
                    attempt_number=next_attempt,
                    authority_scope_hash=semantic_request.authority_scope_hash,
                    precondition_hash=semantic_request.current_precondition_hash,
                    case_revision_ref=semantic_request.current_case_revision_ref,
                    execution_trace_ref=execution.trace_ref,
                    state="pending",
                )
                self.session.add(existing_attempt)
                self.session.flush()
            execution.semantic_request_ref = semantic_request.ref_id
            execution.semantic_request_attempt_ref = existing_attempt.ref_id
            operation.semantic_request_ref = semantic_request.ref_id
            operation.semantic_request_attempt_ref = existing_attempt.ref_id
            intent_session.semantic_state = "ready"
            intent_session.commit_state = "pending"
            intent_session.semantic_request_ref = semantic_request.ref_id
            intent_session.resolved_intent_json = dict(scope.resolved_intent)
            intent_session.blocking_clarifications = []
            intent_session.version += 1
            self.session.add(
                AuditEvent(
                    event_type="changeset.assembly_started",
                    entity_type="semantic_request",
                    entity_id=semantic_request.id,
                    actor_type="operator",
                    actor_id=get_settings().operator_discord_user_id,
                    request_id=None,
                    primary_ref=semantic_request.ref_id,
                    affected_refs=[semantic_request.ref_id, intent_session.ref_id],
                    basis_refs=[utterance.ref_id],
                    data={"authority_scope_hash": authority_scope_hash},
                )
            )
            return intent_session, semantic_request, existing_attempt

        semantic_request = self.session.scalar(
            select(SemanticRequest)
            .where(SemanticRequest.ref_id == execution.semantic_request_ref)
            .with_for_update()
        )
        attempt = self.session.scalar(
            select(SemanticRequestAttempt)
            .where(SemanticRequestAttempt.ref_id == execution.semantic_request_attempt_ref)
            .with_for_update()
        )
        if semantic_request is None or attempt is None:
            raise DocketError(
                code="assembly_binding_missing",
                message="The assembly execution lost its semantic request binding.",
            )
        bound_session = self.session.get(IntentSession, semantic_request.intent_session_id)
        if bound_session is None:
            raise DocketError(
                code="intent_session_not_found",
                message="The assembly request lost its IntentSession.",
            )
        persisted_scope = (semantic_request.selected_option_binding or {}).get("scope")
        if (
            scope is not None
            and scope.model_dump(mode="json", exclude_none=True) != persisted_scope
        ):
            raise DocketError(
                code="assembly_scope_mismatch",
                message="A later stage operation cannot change the authorized semantic scope.",
            )
        if semantic_request.authority_availability != "available":
            return bound_session, semantic_request, attempt
        operation.semantic_request_ref = semantic_request.ref_id
        operation.semantic_request_attempt_ref = attempt.ref_id
        return bound_session, semantic_request, attempt

    @staticmethod
    def _scope(semantic_request: SemanticRequest) -> AssemblyAuthorityScopeInput:
        payload = (semantic_request.selected_option_binding or {}).get("scope")
        if not isinstance(payload, dict):
            raise DocketError(
                code="assembly_scope_missing",
                message="Semantic request has no persisted assembly scope.",
            )
        return AssemblyAuthorityScopeInput.model_validate(payload)

    @staticmethod
    def _draft_actions(changeset: ChangeSet) -> dict[str, dict[str, Any]]:
        if changeset.staged_actions_json is not None:
            return {str(item["change_id"]): dict(item) for item in changeset.staged_actions_json}
        raise DocketError(
            code="draft_input_adoption_required",
            message="This draft predates independent staged inputs; explicit adoption is required.",
            details={"authority_preserved": True, "next_action": "adopt_preserved_request"},
        )

    @staticmethod
    def _compilation_diagnostics(
        error: DocketError | ValidationError,
        *,
        entry_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(error, DocketError):
            details = error.details or {}
            return [
                {
                    "code": error.code,
                    "category": details.get("category", "domain_validation"),
                    "entry_id": entry_id or details.get("entry_id"),
                    **({"change_id": details["change_id"]} if "change_id" in details else {}),
                    "field_path": details.get("field_path", []),
                    "constraint": details.get("constraint", error.code),
                    "next_action": details.get(
                        "next_action",
                        "repair_staged_entry" if entry_id else "repair_staged_actions",
                    ),
                }
            ]
        # Only structured paths/codes cross the boundary, never validation
        # inputs, arbitrary exception text or copied source contents.
        return [
            {
                "code": "compiled_input_invalid",
                "category": "implementation_validation",
                "entry_id": entry_id,
                "field_path": list(item["loc"]),
                "constraint": item["type"],
                "next_action": "repair_staged_entry" if entry_id else "repair_staged_actions",
            }
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        ]

    @staticmethod
    def _remove_owned_actions(
        actions: dict[str, dict[str, Any]],
        ownership: list[dict[str, Any]],
        entry_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        retained: list[dict[str, Any]] = []
        removed = 0
        for owner in ownership:
            if owner.get("owner_import_entry_id") != entry_id:
                retained.append(owner)
                continue
            for change_id in owner.get("change_ids", []):
                if actions.pop(str(change_id), None) is not None:
                    removed += 1
        return retained, removed

    @staticmethod
    def _entry_statement(entry: NormalizedEntryInput) -> StatementInput:
        item, _temporal = entry_facets(entry)
        subject_refs = list(item.context_entity_refs) or [entry.evidence.source_ref]
        return StatementInput(
            statement_kind="normalized_source_entry",
            subject_refs=subject_refs,
            predicate="normalized_temporal_entry",
            value_json=entry.model_dump(mode="json", exclude={"evidence"}, exclude_none=True),
            affected_fields=["item", "temporal_binding", "calendar_representation"],
            interpretation_json={
                "compiler_identifier": COMPILER_IDENTIFIER,
                "compiler_version": COMPILER_VERSION,
            },
            interpreter_version=f"docket.normalized-entry.v{COMPILER_VERSION}",
            import_entry_id=entry.import_entry_id,
            source_ref=entry.evidence.source_ref,
            source_fragment_locator=entry.evidence.source_fragment_locator,
            source_fragment_hash=entry.evidence.source_fragment_hash,
            extractor_identifier=entry.evidence.extractor_identifier,
            extractor_version=entry.evidence.extractor_version,
        )

    def _entry_lane(
        self,
        entry: NormalizedEntryInput,
        actions: dict[str, dict[str, Any]],
    ) -> str | None:
        if not isinstance(entry, ScheduledOccurrenceEntry):
            return None
        if entry.lane_ref is not None:
            lane = self.session.scalar(
                select(CalendarLane).where(CalendarLane.ref_id == entry.lane_ref)
            )
            if lane is not None:
                return lane.lane
        else:
            action = actions.get(entry.lane_change_id or "", {})
            if action.get("mutation_type") == "calendar_lane_create":
                return str(action["create_spec"]["lane"])
        raise DocketError(
            code="normalized_entry_lane_unresolved",
            message="The selected occurrence lane is unavailable; repair its binding.",
            details={
                "entry_id": entry.import_entry_id,
                "field_path": ["lane_ref"],
                "next_action": "resolve_lane",
            },
        )

    @staticmethod
    def _semantic_target_refs(value: Any, *, field_name: str = "") -> set[str]:
        if field_name in {
            "basis_refs",
            "source_refs",
            "authority_statement_refs",
            "selection_authority_ref",
            "utterance_ref",
        }:
            return set()
        if isinstance(value, str):
            return {value} if is_public_ref(value) else set()
        if isinstance(value, dict):
            return {
                ref
                for key, nested in value.items()
                for ref in ChangeSetAssemblyService._semantic_target_refs(
                    nested, field_name=str(key)
                )
            }
        if isinstance(value, list | tuple):
            return {
                ref
                for nested in value
                for ref in ChangeSetAssemblyService._semantic_target_refs(
                    nested, field_name=field_name
                )
            }
        return set()

    @classmethod
    def _require_targets_within_scope(
        cls,
        actions: list[dict[str, Any]],
        *,
        target_refs: set[str],
    ) -> None:
        observed = {ref for action in actions for ref in cls._semantic_target_refs(action)}
        unexpected = sorted(observed - target_refs)
        if unexpected:
            raise DocketError(
                code="assembly_scope_violation",
                message="Staged actions reference existing targets outside the authorized scope.",
                details={"unexpected_target_refs": unexpected[:25]},
            )

    def _content(
        self,
        *,
        changeset: ChangeSet,
        utterance: OperatorUtterance,
        actions: dict[str, dict[str, Any]],
        entries: list[dict[str, Any]],
        ownership: list[dict[str, Any]],
        expected_versions: dict[str, int],
    ) -> ChangeSetContent | None:
        if not actions:
            return None
        grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in _SNAPSHOT_GROUPS}
        for action in sorted(actions.values(), key=lambda item: str(item["change_id"])):
            group = _GROUP_FOR_OBJECT_TYPE.get(str(action.get("object_type")))
            if group is None:
                raise DocketError(
                    code="assembly_action_type_unknown",
                    message="A staged action has no canonical ChangeSet group.",
                    details={"object_type": action.get("object_type")},
                )
            grouped[group].append(action)

        import_scope: ImportScope | None = None
        statement_refs = [
            str(entry["statement_ref"])
            for entry in entries
            if isinstance(entry.get("statement_ref"), str)
        ]
        basis_refs = list(dict.fromkeys([utterance.ref_id, *statement_refs]))
        if entries:
            source_refs = sorted({str(entry["evidence"]["source_ref"]) for entry in entries})
            effect_types = sorted(
                {
                    str(action["object_type"])
                    for action in actions.values()
                    if str(action["object_type"])
                    in {
                        "entity",
                        "identity_handle",
                        "identity_binding",
                        "affiliation",
                        "relationship",
                        "fact",
                        "interaction",
                        "preference",
                        "calendar_lane",
                        "lane_routing_decision",
                        "canonical_event",
                        "item",
                        "temporal_binding",
                        "task",
                        "temporal_calendar_projection",
                        "reminder_plan",
                        "attention_case_resolution",
                    }
                }
            )
            authority_statement = StatementService(self.session).derive(
                utterance.ref_id,
                [
                    StatementInput(
                        statement_kind="operator_intent",
                        subject_refs=source_refs,
                        predicate="import_effect_authority",
                        value_json={"authorized_effects": effect_types},
                        affected_fields=["import_scope"],
                        interpretation_json={"compiler": "changeset_assembly"},
                        interpreter_version="docket.changeset-assembly.v1",
                    )
                ],
            )[0]
            basis_refs.append(authority_statement.ref_id)
            coverage_by_entry = {
                str(owner["owner_import_entry_id"]): owner.get("coverage") for owner in ownership
            }
            import_scope = ImportScope(
                mode="operator_explicit",
                source_refs=source_refs,
                authorized_effects=cast(list[ImportEffect], effect_types),
                authority_statement_refs=[authority_statement.ref_id],
                entry_coverage=[
                    coverage_by_entry[str(entry["import_entry_id"])]
                    for entry in sorted(entries, key=lambda item: str(item["import_entry_id"]))
                ],
                partition_key="assembled",
            )
        raw = ChangeSetContent(
            basis_refs=basis_refs,
            import_scope=import_scope,
            expected_versions=expected_versions,
            registry_changes=grouped["registry_changes"],
            preference_changes=grouped["preference_changes"],
            lane_changes=grouped["lane_changes"],
            event_changes=grouped["event_changes"],
            tracked_context_changes=grouped["tracked_context_changes"],
            resolution_changes=grouped["resolution_changes"],
            provider_intents=[],
        )
        return raw

    @staticmethod
    def _sync_empty(changeset: ChangeSet) -> None:
        changeset.import_scope_json = None
        for group_name in _SNAPSHOT_GROUPS:
            setattr(changeset, group_name, [])

    def _write_revision(
        self,
        *,
        changeset: ChangeSet,
        content: ChangeSetContent | None,
        operation: AssemblyOperation,
    ) -> ChangeSetRevision:
        if changeset.current_revision >= MAX_DRAFT_REVISIONS:
            raise DocketError(
                code="draft_revision_limit",
                message="Draft reached its explicit immutable revision limit.",
                details={"measured": changeset.current_revision, "limit": MAX_DRAFT_REVISIONS},
            )
        changeset.current_revision += 1
        changeset.version += 1
        if content is None:
            self._sync_empty(changeset)
            pin_snapshot(changeset, None)
            revision = ChangeSetRevision(
                change_set_id=changeset.id,
                revision=changeset.current_revision,
                semantic_request_ref=changeset.semantic_request_ref,
                authority_scope_hash=changeset.authority_scope_hash,
                precondition_hash=changeset.precondition_hash,
                execution_binding_json=changeset.execution_binding_json,
                basis_refs=changeset.basis_refs,
                import_scope_json=None,
                expected_versions=changeset.expected_versions,
                registry_changes=[],
                preference_changes=[],
                lane_changes=[],
                event_changes=[],
                tracked_context_changes=[],
                resolution_changes=[],
                provider_intents=[],
                normalized_entries_json=changeset.normalized_entries_json,
                staged_actions_json=changeset.staged_actions_json,
                compiled_action_ownership_json=changeset.compiled_action_ownership_json,
                compiler_manifest_json=changeset.compiler_manifest_json,
                validation_errors_json=changeset.validation_errors,
                assembly_operation_id=operation.id,
                parameter_hash=sha256_json(
                    {
                        "actions": changeset.staged_actions_json,
                        "entries": changeset.normalized_entries_json,
                    }
                ),
                preview_hash=sha256_json(
                    {
                        "actions": changeset.staged_actions_json,
                        "entries": changeset.normalized_entries_json,
                    }
                ),
            )
            self.session.add(revision)
        else:
            self.changesets._sync_snapshot(changeset, content)
            revision = self.changesets._revision(
                changeset,
                content,
                changeset.current_revision,
            )
            revision.normalized_entries_json = changeset.normalized_entries_json
            revision.staged_actions_json = changeset.staged_actions_json
            revision.compiled_action_ownership_json = changeset.compiled_action_ownership_json
            revision.compiler_manifest_json = changeset.compiler_manifest_json
            revision.validation_errors_json = changeset.validation_errors
            revision.assembly_operation_id = operation.id
        return revision

    @staticmethod
    def _counts(changeset: ChangeSet) -> dict[str, int]:
        if changeset.staged_actions_json is not None:
            return dict(
                sorted(
                    Counter(
                        str(item["object_type"]) for item in changeset.staged_actions_json
                    ).items()
                )
            )
        action_counts = Counter(
            str(item.get("object_type"))
            for group_name in _SNAPSHOT_GROUPS[:-1]
            for item in cast(list[dict[str, Any]], getattr(changeset, group_name))
        )
        return dict(sorted(action_counts.items()))

    @staticmethod
    def _snapshot_counts(snapshot: dict[str, Any]) -> dict[str, int]:
        if snapshot.get("staged_actions") is not None:
            return dict(
                sorted(
                    Counter(str(item["object_type"]) for item in snapshot["staged_actions"]).items()
                )
            )
        action_counts = Counter(
            str(item.get("object_type"))
            for group_name in _SNAPSHOT_GROUPS[:-1]
            for item in cast(list[dict[str, Any]], snapshot[group_name])
        )
        return dict(sorted(action_counts.items()))

    @staticmethod
    def _entry_preview(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "import_entry_id": entry["import_entry_id"],
                "entry_type": entry["entry_type"],
                "title": entry.get("title") or (entry.get("item") or {}).get("title"),
                "timing": entry.get("timing")
                or (entry.get("temporal") or {}).get("temporal_value"),
                "location": entry.get("location"),
                "lane_ref": entry.get("lane_ref"),
                "lane_change_id": entry.get("lane_change_id"),
                "scope": "one_time"
                if entry["entry_type"] == "scheduled_occurrence_entry"
                else "tracked_context",
            }.items()
            if value is not None
        }

    @classmethod
    def _entry_previews(cls, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        for entry in entries[:3]:
            candidate = [*previews, cls._entry_preview(entry)]
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > 7_000:
                break
            previews = candidate
        return previews

    def stage(
        self,
        request: StageChangesInput,
        *,
        assembly_operation_token: str,
        assembly_argument_hash: str,
    ) -> dict[str, Any]:
        operation, execution = self._operation(
            token=assembly_operation_token,
            argument_hash=assembly_argument_hash,
            operation_kind="stage",
            utterance_ref=request.utterance_ref,
        )
        replay = self._replay(operation)
        if replay is not None:
            return replay
        operation.state = "running"
        utterance = self._authority_utterance(request)
        intent_session, semantic_request, attempt = self._bind_request_and_attempt(
            execution=execution,
            operation=operation,
            utterance=utterance,
            scope=request.assembly_scope,
            expected_versions=request.expected_versions,
        )
        changeset = self.session.scalar(
            select(ChangeSet)
            .where(ChangeSet.semantic_request_ref == semantic_request.ref_id)
            .with_for_update()
        )
        if changeset is not None and changeset.state == "committed":
            return self._terminal(
                operation,
                {**changeset.commit_receipt_json, "disposition": "already_committed"},
            )
        created = changeset is None
        if changeset is None:
            changeset = ChangeSet(
                intent_session_id=intent_session.id,
                intent_session_ref=intent_session.ref_id,
                semantic_request_ref=semantic_request.ref_id,
                authority_scope_hash=semantic_request.authority_scope_hash,
                precondition_hash=semantic_request.current_precondition_hash,
                execution_binding_json={"kind": "incremental_assembly"},
                idempotency_key=f"semantic-request:{semantic_request.ref_id}:changeset",
                state="draft",
                version=0,
                current_revision=0,
                basis_refs=[],
                staged_actions_json=[],
            )
            self.session.add(changeset)
            self.session.flush()
        else:
            if attempt.observed_changeset_ref != changeset.ref_id or (
                attempt.observed_draft_revision != changeset.current_revision
            ):
                return self._terminal(
                    operation,
                    {
                        "ok": False,
                        "disposition": "draft_revision_conflict",
                        "error": {
                            "code": "draft_revision_conflict",
                            "message": "Review the current draft before applying this patch.",
                            "details": {
                                "observed_revision": attempt.observed_draft_revision,
                                "current_revision": changeset.current_revision,
                            },
                        },
                        "next": {"action": "review_changeset"},
                    },
                    state="rejected",
                )
            if operation.causal_observed_revision != changeset.current_revision:
                return self._terminal(
                    operation,
                    {
                        "ok": False,
                        "disposition": "draft_revision_conflict",
                        "error": {
                            "code": "draft_revision_conflict",
                            "message": "This operation was admitted against an older revision.",
                            "details": {
                                "observed_revision": operation.causal_observed_revision,
                                "current_revision": changeset.current_revision,
                            },
                        },
                        "next": {"action": "review_changeset"},
                    },
                    state="rejected",
                )

        scope = self._scope(semantic_request)
        prior_content = None if created else self.changesets.verify_execution_revision(changeset)
        allowed_mutations = set(scope.allowed_mutation_types)
        allowed_sources = set(scope.source_refs)
        allowed_targets = set(scope.target_refs)
        actions = self._draft_actions(changeset)
        entries = [dict(item) for item in changeset.normalized_entries_json]
        before_hash = sha256_json(
            {
                "actions": actions,
                "entries": entries,
                "expected_versions": changeset.expected_versions,
            }
        )
        entries_by_id = {str(item["import_entry_id"]): item for item in entries}
        ownership = [dict(item) for item in changeset.compiled_action_ownership_json]
        owned_ids = {
            str(change_id) for owner in ownership for change_id in owner.get("change_ids", [])
        }
        staged = removed = replaced = unchanged = 0
        statement_service = StatementService(self.session)
        # Same-patch creates are resolvable regardless of patch order. Their
        # authority and whole-graph validation still run before any commit.
        proposed_actions = {
            **actions,
            **{
                op.action.change_id: op.action.model_dump(mode="json", exclude_none=True)
                for op in request.patch.operations
                if isinstance(op, StageActionUpsert)
            },
        }
        for op in request.patch.operations:
            if isinstance(op, StageActionRemove):
                proposed_actions.pop(op.change_id, None)
        for patch_operation in request.patch.operations:
            if isinstance(patch_operation, StageActionUpsert):
                action = patch_operation.action.model_dump(mode="json", exclude_none=True)
                change_id = patch_operation.action.change_id
                if change_id in owned_ids:
                    raise DocketError(
                        code="compiler_owned_action",
                        message="Edit the owning normalized entry instead of its compiled action.",
                        details={"change_id": change_id},
                    )
                if patch_operation.action.mutation_type not in allowed_mutations:
                    raise DocketError(
                        code="assembly_scope_violation",
                        message="Staged action is outside the authorized mutation scope.",
                        details={"mutation_type": patch_operation.action.mutation_type},
                    )
                self._require_targets_within_scope([action], target_refs=allowed_targets)
                prior = actions.get(change_id)
                if prior == action:
                    unchanged += 1
                else:
                    actions[change_id] = action
                    replaced += int(prior is not None)
                    staged += int(prior is None)
            elif isinstance(patch_operation, StageActionRemove):
                if patch_operation.change_id in owned_ids:
                    raise DocketError(
                        code="compiler_owned_action",
                        message="Remove the owning normalized entry instead.",
                        details={"change_id": patch_operation.change_id},
                    )
                existed = actions.pop(patch_operation.change_id, None) is not None
                removed += int(existed)
                unchanged += int(not existed)
            elif isinstance(patch_operation, StageNormalizedEntryUpsert):
                entry = patch_operation.entry
                if entry.evidence.source_ref not in allowed_sources:
                    raise DocketError(
                        code="assembly_scope_violation",
                        message="Normalized entry uses a source outside the authorized scope.",
                    )
                statement = statement_service.derive(
                    utterance.ref_id,
                    [self._entry_statement(entry)],
                )[0]
                compiled_types = entry_mutation_types(entry)
                if entry.entry_type not in scope.normalized_entry_types and not (
                    compiled_types <= allowed_mutations
                ):
                    raise DocketError(
                        code="assembly_scope_violation",
                        message="Compiled entry would exceed the authorized mutation scope.",
                        details={"mutation_types": sorted(compiled_types - allowed_mutations)},
                    )
                self._require_targets_within_scope(
                    [entry.model_dump(mode="json", exclude={"evidence"}, exclude_none=True)],
                    target_refs=allowed_targets,
                )
                record = normalized_entry_record(entry, statement_ref=statement.ref_id)
                reserved_ids = entry_action_ids(entry)
                compiled = None
                try:
                    compiled = compile_normalized_entry(
                        entry,
                        utterance_ref=utterance.ref_id,
                        statement_ref=statement.ref_id,
                        calendar_lane=self._entry_lane(entry, proposed_actions),
                    )
                except (DocketError, ValidationError) as exc:
                    record["compilation_errors"] = self._compilation_diagnostics(
                        exc,
                        entry_id=entry.import_entry_id,
                    )
                prior = entries_by_id.get(entry.import_entry_id)
                if prior == record:
                    unchanged += 1
                    continue
                ownership, removed_count = self._remove_owned_actions(
                    actions, ownership, entry.import_entry_id
                )
                removed += removed_count
                for change_id in reserved_ids:
                    if change_id in actions:
                        raise DocketError(
                            code="compiled_action_collision",
                            message="Normalized entry action ID collides with a direct action.",
                            details={"change_id": change_id},
                        )
                if compiled is not None:
                    for action in compiled.actions:
                        actions[str(action["change_id"])] = dict(action)
                    owner = {
                        **compiled.ownership,
                        "coverage": compiled.coverage.model_dump(mode="json", exclude_none=True),
                    }
                else:
                    owner = {
                        "owner_kind": "normalized_entry",
                        "owner_import_entry_id": entry.import_entry_id,
                        "compiler_identifier": COMPILER_IDENTIFIER,
                        "compiler_version": COMPILER_VERSION,
                        "change_ids": reserved_ids,
                        "compilation_state": "blocked_validation",
                    }
                ownership.append(owner)
                owned_ids.update(reserved_ids)
                entries_by_id[entry.import_entry_id] = record
                replaced += int(prior is not None)
                staged += int(prior is None)
            elif isinstance(patch_operation, StageNormalizedEntryRemove):
                prior = entries_by_id.pop(patch_operation.import_entry_id, None)
                ownership, removed_count = self._remove_owned_actions(
                    actions, ownership, patch_operation.import_entry_id
                )
                removed += removed_count + int(prior is not None)
                unchanged += int(prior is None)
                owned_ids = {
                    str(change_id) for owner in ownership for change_id in owner["change_ids"]
                }

        entries = sorted(entries_by_id.values(), key=lambda item: str(item["import_entry_id"]))
        ownership = sorted(ownership, key=lambda item: str(item["owner_import_entry_id"]))
        if len(entries) > MAX_DRAFT_ENTRIES or len(actions) > MAX_CANONICAL_ACTIONS:
            measured = max(len(entries), len(actions))
            limit = MAX_DRAFT_ENTRIES if len(entries) > MAX_DRAFT_ENTRIES else MAX_CANONICAL_ACTIONS
            raise DocketError(
                code="changeset_workload_limit",
                message="The assembled draft exceeds an explicit workload limit.",
                details={
                    "measured": measured,
                    "limit": limit,
                    "authority_preserved": True,
                    "partition_requires_review": True,
                },
            )
        expected_versions = dict(changeset.expected_versions)
        for ref_id, version in request.expected_versions.items():
            prior_version = expected_versions.get(ref_id)
            if prior_version is not None and prior_version != version:
                raise DocketError(
                    code="assembly_precondition_conflict",
                    message="One draft cannot carry two expected versions for the same object.",
                    details={"ref": ref_id},
                )
            expected_versions[ref_id] = version
        errors = [error for entry in entries for error in entry.get("compilation_errors", [])]
        content = None
        try:
            if not errors:
                content = self._content(
                    changeset=changeset,
                    utterance=utterance,
                    actions=actions,
                    entries=entries,
                    ownership=ownership,
                    expected_versions=expected_versions,
                )
                if content is not None:
                    content = self.changesets._compile_required_provider_intents(
                        content,
                        changeset_idempotency_key=changeset.idempotency_key,
                    )
                    errors.extend(
                        self.changesets._validate(intent_session, content, require_handlers=False)
                    )
                else:
                    errors.append(
                        {
                            "code": "empty_changeset",
                            "category": "domain_validation",
                            "field_path": ["patch"],
                            "constraint": "nonempty_changeset",
                            "next_action": "stage_changes",
                        }
                    )
        except (DocketError, ValidationError) as exc:
            errors.extend(self._compilation_diagnostics(exc))
        after_hash = sha256_json(
            {
                "actions": actions,
                "entries": entries,
                "expected_versions": expected_versions,
            }
        )
        if not created and before_hash == after_hash and errors == changeset.validation_errors:
            if (
                prior_content is not None
                and content is not None
                and effect_hash(prior_content.model_dump(mode="json", exclude_none=True))
                != effect_hash(content.model_dump(mode="json", exclude_none=True))
            ):
                # An unchanged stage patch is not permission for a deployment
                # to silently replace provider/occurrence compiler products.
                raise migration_required()
            result = {
                "ok": True,
                "disposition": "no_op",
                "draft_ref": changeset.ref_id,
                "current_revision": changeset.current_revision,
                "staged_count": 0,
                "removed_count": 0,
                "replaced_count": 0,
                "unchanged_count": unchanged,
                "totals": self._counts(changeset),
                "assembly_ready": changeset.state == "validated",
                "readiness": "saved_with_errors" if errors else "ready_to_commit",
                **_diagnostic_projection(
                    changeset_ref=changeset.ref_id,
                    revision=changeset.current_revision,
                    errors=changeset.validation_errors,
                ),
                "next": {"action": "repair_staged_actions" if errors else "commit_changeset"},
            }
            return self._terminal(operation, result)
        if content is not None and len(content.provider_intents) > MAX_PROVIDER_OPERATIONS:
            raise DocketError(
                code="changeset_provider_workload_limit",
                message="The draft exceeds the provider Operation workload limit.",
                details={
                    "measured": len(content.provider_intents),
                    "limit": MAX_PROVIDER_OPERATIONS,
                },
            )
        new_precondition_hash = sha256_json(
            {
                "expected_versions": expected_versions,
                "case_refs": intent_session.case_refs,
                "case_revision_refs": intent_session.case_revision_refs,
            }
        )
        semantic_request.current_precondition_hash = new_precondition_hash
        attempt.precondition_hash = new_precondition_hash
        changeset.normalized_entries_json = entries
        changeset.staged_actions_json = sorted(
            actions.values(), key=lambda action: str(action["change_id"])
        )
        changeset.compiled_action_ownership_json = ownership
        changeset.compiler_manifest_json = (
            {
                "identifier": COMPILER_IDENTIFIER,
                "version": COMPILER_VERSION,
                "entry_count": len(entries),
            }
            if entries
            else {}
        )
        changeset.precondition_hash = new_precondition_hash
        changeset.expected_versions = expected_versions
        changeset.basis_refs = list(
            dict.fromkeys(
                [
                    utterance.ref_id,
                    *(str(entry["statement_ref"]) for entry in entries),
                ]
            )
        )
        changeset.validation_errors = errors
        changeset.state = "validated" if not errors else "draft"
        if not errors:
            semantic_request.commit_state = "pending"
            intent_session.commit_state = "pending"
            attempt.state = "pending"
        self._write_revision(changeset=changeset, content=content, operation=operation)
        attempt.observed_changeset_ref = changeset.ref_id
        attempt.observed_draft_revision = changeset.current_revision
        attempt.change_set_ref = changeset.ref_id
        operation.change_set_ref = changeset.ref_id
        entry_previews = self._entry_previews(entries)
        result = {
            "ok": True,
            "disposition": "saved_with_errors" if errors else "ready_to_commit",
            "draft_ref": changeset.ref_id,
            "current_revision": changeset.current_revision,
            "staged_count": staged,
            "removed_count": removed,
            "replaced_count": replaced,
            "unchanged_count": unchanged,
            "totals": self._counts(changeset),
            "normalized_entry_count": len(entries),
            "entry_preview": entry_previews,
            "omitted_entry_count": len(entries) - len(entry_previews),
            "predicted_provider_operation_count": len(changeset.provider_intents),
            "assembly_ready": not errors,
            **_diagnostic_projection(
                changeset_ref=changeset.ref_id,
                revision=changeset.current_revision,
                errors=errors,
            ),
            "next": {"action": "commit_changeset" if not errors else "repair_staged_actions"},
        }
        self.session.add(
            AuditEvent(
                event_type="changeset.staged",
                entity_type="changeset",
                entity_id=changeset.id,
                actor_type="docket_compiler",
                actor_id=None,
                request_id=None,
                primary_ref=changeset.ref_id,
                affected_refs=[changeset.ref_id, semantic_request.ref_id],
                basis_refs=[utterance.ref_id],
                data={
                    "revision": changeset.current_revision,
                    "staged_count": staged,
                    "removed_count": removed,
                    "replaced_count": replaced,
                },
            )
        )
        return self._terminal(operation, result)

    @staticmethod
    def _revision_snapshot(revision: ChangeSetRevision) -> dict[str, Any]:
        return {
            "basis_refs": revision.basis_refs,
            "import_scope": revision.import_scope_json,
            "expected_versions": revision.expected_versions,
            "registry_changes": revision.registry_changes,
            "preference_changes": revision.preference_changes,
            "lane_changes": revision.lane_changes,
            "event_changes": revision.event_changes,
            "tracked_context_changes": revision.tracked_context_changes,
            "resolution_changes": revision.resolution_changes,
            "provider_intents": revision.provider_intents,
            "normalized_entries": revision.normalized_entries_json,
            "staged_actions": revision.staged_actions_json,
            "ownership": revision.compiled_action_ownership_json,
            "validation_errors": revision.validation_errors_json,
        }

    def review(
        self,
        request: ReviewChangesInput,
        *,
        assembly_operation_token: str,
        assembly_argument_hash: str,
    ) -> dict[str, Any]:
        operation, execution = self._operation(
            token=assembly_operation_token,
            argument_hash=assembly_argument_hash,
            operation_kind="review",
            utterance_ref=request.utterance_ref,
        )
        replay = self._replay(operation)
        if replay is not None:
            return replay
        operation.state = "running"
        if execution.semantic_request_ref is None:
            raise DocketError(
                code="assembly_not_started",
                message="There is no current staged request to review.",
            )
        semantic_request = self.session.scalar(
            select(SemanticRequest).where(SemanticRequest.ref_id == execution.semantic_request_ref)
        )
        attempt = self.session.scalar(
            select(SemanticRequestAttempt).where(
                SemanticRequestAttempt.ref_id == execution.semantic_request_attempt_ref
            )
        )
        if semantic_request is None or attempt is None:
            raise DocketError(
                code="assembly_binding_missing", message="Assembly binding is missing."
            )
        changeset = self.session.scalar(
            select(ChangeSet).where(ChangeSet.semantic_request_ref == semantic_request.ref_id)
        )
        if changeset is None:
            raise DocketError(code="assembly_not_started", message="No draft exists to review.")
        cursor_payload: dict[str, Any] | None = None
        if request.cursor is not None:
            cursor_payload = _cursor_decode(request.cursor)
            expected = {
                "format_version": 1,
                "changeset_ref": changeset.ref_id,
                "view": request.view,
                "mutation_types": sorted(request.mutation_types),
                "entry_types": sorted(request.normalized_entry_types),
            }
            if any(cursor_payload.get(key) != value for key, value in expected.items()):
                raise DocketError(
                    code="review_cursor_mismatch",
                    message="Review cursor does not match this bounded review request.",
                )
            revision_value = cursor_payload.get("revision")
            position_value = cursor_payload.get("position")
            if (
                type(revision_value) is not int or revision_value < 1
                or type(position_value) is not int or position_value < 0
            ):
                raise DocketError(
                    code="invalid_review_cursor",
                    message="Restart review; the cursor revision or position is invalid.",
                )
            revision_number = revision_value
            position = position_value
        else:
            revision_number = changeset.current_revision
            position = 0
        revision = self.session.scalar(
            select(ChangeSetRevision).where(
                ChangeSetRevision.change_set_id == changeset.id,
                ChangeSetRevision.revision == revision_number,
            )
        )
        if revision is None:
            raise DocketError(
                code="review_revision_unavailable",
                message="The immutable review revision is unavailable; restart review.",
            )
        snapshot = self._revision_snapshot(revision)
        all_actions = snapshot["staged_actions"]
        if all_actions is None:
            all_actions = [
                item for group_name in _SNAPSHOT_GROUPS[:-1] for item in snapshot[group_name]
            ]
        actions = [
            item
            for item in all_actions
            if not request.mutation_types
            or str(item.get("mutation_type")) in request.mutation_types
        ]
        entries = [
            item
            for item in cast(list[dict[str, Any]], snapshot["normalized_entries"])
            if not request.normalized_entry_types
            or str(item.get("entry_type")) in request.normalized_entry_types
        ]
        ownership = {
            str(item.get("owner_import_entry_id")): item
            for item in cast(list[dict[str, Any]], snapshot["ownership"])
        }
        details: list[dict[str, Any]]
        if request.view == "entries":
            details = [
                {
                    **self._entry_preview(entry),
                    "compiled_change_ids": ownership.get(str(entry.get("import_entry_id")), {}).get(
                        "change_ids", []
                    ),
                    "predicted_provider_operation_types": ownership.get(
                        str(entry.get("import_entry_id")), {}
                    ).get("predicted_provider_operation_types", []),
                }
                for entry in entries
            ]
        elif request.view == "diagnostics":
            details = list(snapshot["validation_errors"])
        elif request.view == "diff":
            previous_revision = self.session.scalar(
                select(ChangeSetRevision)
                .where(
                    ChangeSetRevision.change_set_id == changeset.id,
                    ChangeSetRevision.revision < revision_number,
                )
                .order_by(ChangeSetRevision.revision.desc())
                .limit(1)
            )
            details, diff_counts = draft_diff(
                previous_revision,
                revision,
                mutation_types=request.mutation_types,
                entry_types=request.normalized_entry_types,
            )
        elif request.view == "actions":
            details = [
                {
                    "change_id": action.get("change_id"),
                    "mutation_type": action.get("mutation_type"),
                    "action": action.get("action"),
                    "object_type": action.get("object_type"),
                }
                for action in actions
            ]
        else:
            details = []
        details.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        logical_detail_count = len(details)
        details = bounded_details(details)
        if position > len(details):
            raise DocketError(
                code="invalid_review_cursor",
                message="Restart review; cursor exceeds this revision.",
            )
        page: list[dict[str, Any]] = []
        for detail in details[position : position + request.limit]:
            candidate = [*page, detail]
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > 8_000:
                break
            page = candidate
        next_position = position + len(page)
        next_cursor = None
        if next_position < len(details):
            next_cursor = _cursor_encode(
                {
                    "format_version": 1,
                    "changeset_ref": changeset.ref_id,
                    "revision": revision_number,
                    "view": request.view,
                    "mutation_types": sorted(request.mutation_types),
                    "entry_types": sorted(request.normalized_entry_types),
                    "position": next_position,
                }
            )
        if request.cursor is None and revision_number == changeset.current_revision:
            attempt.observed_changeset_ref = changeset.ref_id
            attempt.observed_draft_revision = revision_number
        revision_state = (
            changeset.state
            if revision_number == changeset.current_revision
            else "validated"
            if not snapshot["validation_errors"]
            else "draft"
        )
        result = {
            "ok": True,
            "disposition": "reviewed",
            "draft_ref": changeset.ref_id,
            "revision": revision_number,
            "current_revision": changeset.current_revision,
            "is_current_revision": revision_number == changeset.current_revision,
            "state": revision_state,
            "totals": self._snapshot_counts(snapshot),
            "normalized_entry_counts": dict(
                sorted(Counter(str(item.get("entry_type")) for item in entries).items())
            ),
            "provider_operation_counts": dict(
                sorted(
                    Counter(
                        str(item.get("operation_type"))
                        for item in cast(list[dict[str, Any]], snapshot["provider_intents"])
                    ).items()
                )
            ),
            **_diagnostic_projection(
                changeset_ref=changeset.ref_id,
                revision=revision_number,
                errors=snapshot["validation_errors"],
            ),
            "items": page,
            "count": len(page),
            "total_if_known": len(details),
            "logical_detail_count": logical_detail_count,
            "omitted_detail_count": len(details) - next_position,
            **(
                {
                    "diff_basis": "previous_draft_revision",
                    "base_revision": previous_revision.revision if previous_revision else None,
                    "diff_subject_counts": diff_counts,
                }
                if request.view == "diff"
                else {}
            ),
            "truncated": next_cursor is not None,
            **({"cursor": next_cursor} if next_cursor is not None else {}),
            "next": {
                "action": "commit_changeset"
                if revision_state == "validated" and revision_number == changeset.current_revision
                else "stage_or_reconcile"
            },
        }
        return self._terminal(operation, result)

    def _ensure_turn(
        self,
        *,
        intent_session: IntentSession,
        semantic_request: SemanticRequest,
        utterance: OperatorUtterance,
    ) -> IntentTurn:
        existing = self.session.scalar(
            select(IntentTurn).where(
                IntentTurn.intent_session_id == intent_session.id,
                IntentTurn.utterance_ref == utterance.ref_id,
            )
        )
        if existing is not None:
            return existing
        statement_refs = list(
            self.session.scalars(
                select(InterpretedStatement.ref_id).where(
                    InterpretedStatement.utterance_id == utterance.id
                )
            )
        )
        turn = IntentTurn(
            intent_session_id=intent_session.id,
            intent_session_ref=intent_session.ref_id,
            utterance_ref=utterance.ref_id,
            statement_refs=statement_refs,
            context_refs=list(intent_session.trusted_context_refs),
            tool_call_refs=[],
            resulting_semantic_refs=[],
            response_disposition="pending",
            semantic_request_ref=semantic_request.ref_id,
            authority_substitutions_json={},
        )
        self.session.add(turn)
        self.session.flush()
        self.session.add(
            AuditEvent(
                event_type="intent.turn_appended",
                entity_type="intent_turn",
                entity_id=turn.id,
                actor_type="operator",
                actor_id=get_settings().operator_discord_user_id,
                request_id=None,
                primary_ref=turn.ref_id,
                affected_refs=[turn.ref_id, intent_session.ref_id, semantic_request.ref_id],
                basis_refs=[utterance.ref_id],
                data={"source": "changeset_assembly"},
            )
        )
        return turn

    def commit(
        self,
        *,
        utterance_ref: str,
        request_key: str,
        assembly_operation_token: str,
        assembly_argument_hash: str,
    ) -> dict[str, Any]:
        operation, execution = self._operation(
            token=assembly_operation_token,
            argument_hash=assembly_argument_hash,
            operation_kind="commit",
            utterance_ref=utterance_ref,
        )
        replay = self._replay(operation)
        if replay is not None:
            return replay
        operation.state = "running"
        utterance = self.authority._authority_utterance(
            utterance_ref=utterance_ref,
            request_key=request_key,
            actor_id=str(get_settings().operator_discord_user_id),
        )
        if execution.semantic_request_ref is None:
            raise DocketError(code="assembly_not_started", message="No staged request exists.")
        semantic_request = self.session.scalar(
            select(SemanticRequest)
            .where(SemanticRequest.ref_id == execution.semantic_request_ref)
            .with_for_update()
        )
        attempt = self.session.scalar(
            select(SemanticRequestAttempt)
            .where(SemanticRequestAttempt.ref_id == execution.semantic_request_attempt_ref)
            .with_for_update()
        )
        if semantic_request is None or attempt is None:
            raise DocketError(
                code="assembly_binding_missing", message="Assembly binding is missing."
            )
        changeset = self.session.scalar(
            select(ChangeSet)
            .where(ChangeSet.semantic_request_ref == semantic_request.ref_id)
            .with_for_update()
        )
        if changeset is None:
            raise DocketError(code="assembly_not_started", message="No staged request exists.")
        if changeset.state == "committed":
            return self._terminal(operation, dict(changeset.commit_receipt_json))
        if (
            attempt.observed_changeset_ref != changeset.ref_id
            or attempt.observed_draft_revision != changeset.current_revision
            or operation.causal_observed_revision != changeset.current_revision
        ):
            return self._terminal(
                operation,
                {
                    "ok": False,
                    "disposition": "draft_revision_conflict",
                    "error": {
                        "code": "draft_revision_conflict",
                        "message": "Review the current draft before committing it.",
                        "details": {
                            "observed_revision": attempt.observed_draft_revision,
                            "current_revision": changeset.current_revision,
                            "changed_action_counts": self._counts(changeset),
                        },
                    },
                    "next": {"action": "review_changeset"},
                },
                state="rejected",
            )
        if changeset.state != "validated":
            semantic_request.commit_state = "blocked_validation"
            attempt.state = "blocked_validation"
            attempt.error_code = "changeset_validation_failed"
            attempt.error_details_json = {"revision": changeset.current_revision}
            bound_session = self.session.get(IntentSession, semantic_request.intent_session_id)
            if bound_session is not None:
                bound_session.commit_state = "blocked_validation"
            return self._terminal(
                operation,
                {
                    "ok": False,
                    "disposition": "rejected_validation",
                    "error": {
                        "code": "changeset_validation_failed",
                        "message": "The assembled draft is not commit-ready.",
                        "details": {
                            **_diagnostic_projection(
                                changeset_ref=changeset.ref_id,
                                revision=changeset.current_revision,
                                errors=changeset.validation_errors,
                            ),
                            "authority_preserved": True,
                        },
                    },
                    "next": {"action": "review_changeset"},
                },
                state="rejected",
            )
        intent_session = self.session.get(IntentSession, semantic_request.intent_session_id)
        if intent_session is None:
            raise DocketError(code="intent_session_not_found", message="IntentSession missing.")
        self._ensure_turn(
            intent_session=intent_session,
            semantic_request=semantic_request,
            utterance=utterance,
        )
        committed, _receipt = self.changesets.commit(
            ChangeSetCommit(
                changeset_ref=changeset.ref_id,
                expected_version=changeset.version,
                idempotency_key=changeset.idempotency_key,
                authority_utterance_ref=utterance.ref_id,
            )
        )
        semantic_request.authority_availability = "consumed_committed"
        semantic_request.commit_state = "committed"
        semantic_request.committed_changeset_ref = committed.ref_id
        attempt.state = "committed"
        attempt.change_set_ref = committed.ref_id
        attempt.completed_at = committed.committed_at
        operation.change_set_ref = committed.ref_id
        return self._terminal(operation, dict(committed.commit_receipt_json))
