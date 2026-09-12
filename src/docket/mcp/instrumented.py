from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock, TextContent, Tool
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.database import session_scope
from docket.domain.canonical import sha256_json
from docket.domain.enums import OutboxStatus
from docket.domain.errors import DocketError
from docket.domain.public_refs import is_public_ref
from docket.models import (
    AttachmentEvidence,
    ConversationalToolTrace,
    OperatorUtterance,
    OutboxEvent,
    SemanticRequestAttempt,
    ToolInvocation,
)
from docket.models.base import utc_now
from docket.services.changeset_assembly import ChangeSetAssemblyService
from docket.services.continuity import ContinuityService
from docket.services.invocation_binding import BINDING_ARGUMENT, bind_invocation
from docket.services.invocation_outcomes import (
    bound_assembly_operation,
    is_gateway_unknown,
    recover_assembly_outcome,
)
from docket.tool_contracts import CONTRACT_VERSION, contract_hash

INTERACTIVE_AUTHORITY_TOOLS = frozenset(
    {
        "docket_stage_changes",
        "docket_review_changeset",
        "docket_commit_changeset",
        "docket_resolve_conflict",
        "docket_request_clarification",
    }
)
INTERACTIVE_ATTACHMENT_TOOLS = frozenset(
    {
        "docket_stage_changes",
        "docket_commit_changeset",
        "docket_resolve_conflict",
        "docket_request_clarification",
    }
)
INTERACTIVE_CANONICAL_MUTATION_TOOLS = frozenset(
    {"docket_commit_changeset", "docket_resolve_conflict"}
)
INFRASTRUCTURE_ARGUMENT_NAMES = frozenset({
    "assembly_operation_token", "assembly_argument_hash", "utterance_ref",
    "request_key", "operator_utterance_ref",
    BINDING_ARGUMENT,
})

_LIST_RESULT_KEYS = (
    "accounts",
    "calendar_lanes",
    "events",
    "lanes",
    "results",
)


def _result_envelope(result: Sequence[ContentBlock] | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    for item in result:
        if isinstance(item, dict):
            return item
        if isinstance(item, list | tuple):
            nested = _result_envelope(item)
            if nested is not None:
                return nested
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _collect_result_refs(value: Any, *, limit: int = 100) -> list[str]:
    refs: list[str] = []

    def visit(item: Any) -> None:
        if len(refs) >= limit:
            return
        if is_public_ref(item):
            if item not in refs:
                refs.append(item)
            return
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list | tuple):
            for nested in item:
                visit(nested)

    visit(value)
    return refs


def _terminal_status(error_code: str | None) -> str:
    code = (error_code or "").casefold()
    if any(token in code for token in ("internal", "runtime", "service_exception")):
        return "failed"
    if any(token in code for token in ("authoriz", "invalid_source", "invalid_actor")):
        return "rejected_authority"
    if any(token in code for token in ("conflict", "stale", "version")):
        return "rejected_conflict"
    if any(token in code for token in ("validation", "invalid_argument", "unknown_tool")):
        return "rejected_validation"
    return "rejected_validation"


def _domain_state(status: str) -> str:
    if status == "succeeded":
        return "succeeded"
    if status in {
        "rejected_validation",
        "rejected_authority",
        "rejected_conflict",
    }:
        return "rejected"
    return "failed"


def _omit_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _omit_nulls(nested)
            for key, nested in value.items()
            if nested is not None and not _internal_uuid_field(key, nested)
        }
    if isinstance(value, list):
        return [_omit_nulls(nested) for nested in value]
    return value


def _internal_uuid_field(key: object, value: object) -> bool:
    if not isinstance(key, str) or not isinstance(value, str):
        return False
    if key in {
        "calendar_id",
        "external_event_id",
        "external_object_id",
        "external_parent_id",
        "provider_event_id",
        "recurring_event_id",
    }:
        return False
    if key != "id" and not key.endswith("_id"):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _serialized_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _validation_issues(exc: Exception) -> list[dict[str, Any]]:
    if not isinstance(exc, ValidationError):
        return []
    issues: list[dict[str, Any]] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:12]:
        location = [str(component)[:64] for component in error.get("loc", ())[:16]]
        issues.append(
            {
                "path": location,
                "type": str(error.get("type", "validation_error"))[:128],
                "message": str(error.get("msg", "Invalid argument."))[:240],
            }
        )
    return issues


def _terminalize_invalid_assembly_operation(
    session: Session,
    *,
    name: str,
    arguments: dict[str, Any],
    argument_hash: str,
    issues: list[dict[str, Any]],
) -> None:
    operation_kind: Literal["stage", "review", "commit"] | None = None
    if name == "docket_stage_changes":
        operation_kind = "stage"
    elif name == "docket_review_changeset":
        operation_kind = "review"
    elif name == "docket_commit_changeset":
        operation_kind = "commit"
    token = arguments.get("assembly_operation_token")
    admitted_hash = arguments.get("assembly_argument_hash")
    utterance_ref = arguments.get("utterance_ref")
    if (
        operation_kind is None
        or not isinstance(token, str)
        or not isinstance(admitted_hash, str)
        or admitted_hash != argument_hash
        or not isinstance(utterance_ref, str)
    ):
        return
    ChangeSetAssemblyService(session).reject_admitted_operation(
        token=token,
        argument_hash=argument_hash,
        operation_kind=operation_kind,
        utterance_ref=utterance_ref,
        error=DocketError(
            code="validation_error",
            message="Tool arguments do not satisfy the registered Pydantic schema.",
            details={"issues": issues},
        ),
    )


def _domain_error_result(
    *,
    code: str,
    message: str,
    disposition: str,
    details: dict[str, Any] | None = None,
) -> Sequence[ContentBlock] | dict[str, Any]:
    """Return a domain rejection as a successful MCP transport result."""
    payload = {
        "ok": False,
        "disposition": disposition,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }
    result, _envelope = _compact_result(
        payload,
        payload,
        audit=False,
        page_limit=25,
    )
    return result


def _compact_result(
    result: Sequence[ContentBlock] | dict[str, Any],
    envelope: dict[str, Any] | None,
    *,
    audit: bool,
    page_limit: int,
) -> tuple[Sequence[ContentBlock] | dict[str, Any], dict[str, Any] | None]:
    if envelope is None:
        return result, None
    payload = _omit_nulls(envelope)
    list_keys = [
        key for key in _LIST_RESULT_KEYS if key in payload and isinstance(payload[key], list)
    ]
    if "items" not in payload and len(list_keys) == 1:
        source_key = list_keys[0]
        items = payload.pop(source_key)
        payload["items"] = items
        payload.setdefault("count", len(items))
        payload.setdefault("total_if_known", payload.get("total", len(items)))
        payload.setdefault("truncated", False)
    budget = 65536 if audit else 16384
    items = payload.get("items")
    if isinstance(items, list):
        original_count = len(items)
        if original_count > page_limit:
            del items[page_limit:]
            payload["count"] = len(items)
            payload["total_if_known"] = payload.get("total_if_known", original_count)
            payload["truncated"] = True
        while items and _serialized_bytes(payload) > budget:
            items.pop()
        if len(items) != original_count:
            payload["count"] = len(items)
            payload["total_if_known"] = payload.get("total_if_known", original_count)
            payload["truncated"] = True
            payload.setdefault("next", {"cursor": payload.get("cursor")})
    if _serialized_bytes(payload) > budget:
        refs = _collect_result_refs(payload)
        payload = {
            "ok": False,
            "error": {
                "code": "output_budget_exceeded",
                "message": "Result requires a narrower query or paginated audit lookup.",
                "details": {"budget_bytes": budget},
            },
            "affected_refs": refs,
            "next": {"action": "narrow_query_or_paginate"},
        }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # FastMCP tools with an output schema return a two-tuple containing both
    # unstructured content and structured content. Preserve that protocol shape
    # after compaction; returning only TextContent makes the low-level MCP server
    # reject an otherwise successful call as missing structured output.
    compacted = ([TextContent(type="text", text=serialized)], payload)
    return cast(Sequence[ContentBlock], compacted), payload


def _operator_utterance_authority(
    session: Session,
    normalized_arguments: dict[str, Any],
) -> OperatorUtterance | None:
    request_key = normalized_arguments.get("request_key")
    if not isinstance(request_key, str):
        return None
    components = request_key.split(":")
    if (
        len(components) != 5
        or components[0] != "discord"
        or not all(component.isascii() and component.isdecimal() for component in components[1:])
    ):
        return None
    guild_id, channel_id, message_id = components[1:4]
    authority_request_key = f"discord:{guild_id}:{channel_id}:{message_id}:0"
    utterance = session.scalar(
        select(OperatorUtterance).where(OperatorUtterance.request_key == authority_request_key)
    )
    if utterance is None:
        return None
    requested_utterance_ref = normalized_arguments.get("utterance_ref")
    if requested_utterance_ref is not None and requested_utterance_ref != utterance.ref_id:
        return None
    actor_id = normalized_arguments.get("actor_id")
    if actor_id is not None and utterance.actor_ref != f"discord_user:{actor_id}":
        return None
    source = normalized_arguments.get("source")
    if isinstance(source, dict):
        metadata = source.get("metadata")
        if not isinstance(metadata, dict):
            return None
        if any(
            str(metadata.get(key, "")) != expected
            for key, expected in (
                ("guild_id", guild_id),
                ("channel_id", channel_id),
                ("message_id", message_id),
            )
        ):
            return None
        source_actor = metadata.get("user_id")
        if source_actor is not None and utterance.actor_ref != f"discord_user:{source_actor}":
            return None
    return utterance


class ProvenanceFastMCP(FastMCP[Any]):
    def __init__(
        self,
        name: str,
        *,
        caller_profile: Literal["interactive", "triage"],
        **kwargs: Any,
    ) -> None:
        self.caller_profile = caller_profile
        super().__init__(name, **kwargs)

    async def list_tools(self) -> list[Tool]:
        registered = await super().list_tools()
        for tool in registered:
            tool.inputSchema["additionalProperties"] = False
            if self.caller_profile == "interactive":
                tool.inputSchema.setdefault("properties", {})[BINDING_ARGUMENT] = {
                    "type": "string", "maxLength": 4096, "x-docket-internal": True,
                    "description": "Trusted gateway correlation envelope; never model supplied.",
                }
        return registered

    @staticmethod
    def _finish_invocation(
        session: Session,
        invocation_id: uuid.UUID,
        *,
        status: str,
        normalized_argument_hash: str | None,
        result_refs: list[str],
        result_disposition: str | None,
        error_code: str | None,
        domain_state: str | None = None,
        semantic_request_ref: str | None = None,
    ) -> None:
        invocation = session.get(
            ToolInvocation, invocation_id, with_for_update=True, populate_existing=True,
        )
        if invocation is None:
            raise RuntimeError("ToolInvocation disappeared before completion")
        late_outcome = is_gateway_unknown(invocation)
        if invocation.transport_state != "running" and not late_outcome:
            return
        recovered = (late_outcome or status == "failed") and recover_assembly_outcome(
            session, invocation,
        )
        if recovered:
            semantic_request_ref = invocation.semantic_request_ref
        invocation.normalized_argument_hash = normalized_argument_hash
        if not recovered:
            invocation.result_refs = result_refs
            invocation.result_disposition = result_disposition
            # Reaching this method means the authenticated MCP request received a
            # durable Docket outcome. Domain failure is not transport failure.
            invocation.transport_state = "completed"
            invocation.domain_state = domain_state or _domain_state(status)
            invocation.error_code = error_code
            invocation.completed_at = utc_now()
        if semantic_request_ref is not None:
            invocation.semantic_request_ref = semantic_request_ref
            operation = bound_assembly_operation(session, invocation)
            attempt = session.scalar(
                select(SemanticRequestAttempt)
                .where(
                    SemanticRequestAttempt.semantic_request_ref == semantic_request_ref,
                    SemanticRequestAttempt.tool_call_ref.is_(None),
                    SemanticRequestAttempt.ref_id == operation.semantic_request_attempt_ref,
                )
                .with_for_update()
            ) if operation is not None else None
            if attempt is not None:
                attempt.tool_call_ref = invocation.ref_id
        if invocation.trace_ref is not None:
            # Do not depend on a later wrapper callback to show an authenticated
            # outcome. Preserve the conversation's separate delivery state.
            trace = session.scalar(select(ConversationalToolTrace).where(
                ConversationalToolTrace.ref_id == invocation.trace_ref,
            ).with_for_update())
            if trace is not None:
                trace.version += 1
                session.add(OutboxEvent(
                    event_type="discord.mcp_trace.requested",
                    aggregate_type="conversational_tool_trace",
                    aggregate_id=trace.id,
                    deduplication_key=f"conversational_tool_trace:{trace.ref_id}:v{trace.version}",
                    payload={"trace_ref": trace.ref_id, "trace_version": trace.version},
                    status=OutboxStatus.PENDING.value,
                ))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        binding_provided = BINDING_ARGUMENT in arguments
        binding_token = arguments.get(BINDING_ARGUMENT)
        arguments = {key: value for key, value in arguments.items() if key != BINDING_ARGUMENT}
        public_arguments = {
            key: value
            for key, value in arguments.items()
            if key not in INFRASTRUCTURE_ARGUMENT_NAMES
        }
        received_hash = sha256_json(public_arguments)
        context = self.get_context()
        try:
            mcp_request_id = context.request_id
        except ValueError:
            mcp_request_id = None

        execution_completion_token: str | None = None
        admission_error: DocketError | None = None
        admission_disposition = "rejected_validation"
        with session_scope() as session:
            invocation = ToolInvocation(
                tool_name=name,
                tool_contract_version=CONTRACT_VERSION,
                tool_contract_hash=contract_hash(self.caller_profile),
                caller_profile=self.caller_profile,
                received_argument_hash=received_hash,
                mcp_request_id=mcp_request_id,
            )
            session.add(invocation)
            session.flush()
            invocation_id = invocation.id
            if binding_provided:
                try:
                    bind_invocation(session, invocation, binding_token, arguments=arguments)
                except DocketError as exc:
                    admission_error = exc
            try:
                execution_completion_token = None if admission_error is not None else (
                    ContinuityService(session)
                    .acquire_execution_lease(
                        lease_key=f"tool:{invocation.ref_id}",
                        lease_kind="tool_invocation",
                        subject_ref=invocation.ref_id,
                    )
                    .completion_token
                )
            except DocketError as exc:
                if exc.code != "deployment_drain_active":
                    raise
                admission_error = exc
                admission_disposition = "deferred_drain"
            if admission_error is not None:
                self._finish_invocation(
                    session,
                    invocation_id,
                    status="rejected_validation",
                    normalized_argument_hash=None,
                    result_refs=[],
                    result_disposition=admission_disposition,
                    error_code=admission_error.code,
                )

        if admission_error is not None:
            return _domain_error_result(
                code=admission_error.code,
                message=admission_error.message,
                disposition=admission_disposition,
                details=admission_error.details,
            )

        normalized_hash: str | None = None
        normalized_arguments: dict[str, Any] | None = None
        normalization_error: Exception | None = None
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            try:
                unexpected = set(arguments) - set(tool.fn_metadata.arg_model.model_fields)
                if unexpected:
                    raise ValidationError.from_exception_data(name, [
                        {"type": "extra_forbidden", "loc": (field,), "input": None}
                        for field in sorted(unexpected)
                    ])
                preparsed = tool.fn_metadata.pre_parse_json(arguments)
                normalized = tool.fn_metadata.arg_model.model_validate(preparsed)
                normalized_arguments = normalized.model_dump(mode="json", by_alias=True)
                normalized_hash = sha256_json(
                    {
                        key: value
                        for key, value in normalized_arguments.items()
                        if key not in INFRASTRUCTURE_ARGUMENT_NAMES
                    }
                )
            except Exception as exc:
                normalized_hash = None
                normalization_error = exc

        if normalization_error is not None:
            validation_issues = _validation_issues(normalization_error)
            with session_scope() as session:
                _terminalize_invalid_assembly_operation(
                    session,
                    name=name,
                    arguments=arguments,
                    argument_hash=received_hash,
                    issues=validation_issues,
                )
                self._finish_invocation(
                    session,
                    invocation_id,
                    status="rejected_validation",
                    normalized_argument_hash=None,
                    result_refs=[],
                    result_disposition="rejected_validation",
                    error_code="validation_error",
                )
                if execution_completion_token is not None:
                    ContinuityService(session).complete_execution_lease(
                        execution_completion_token,
                        metadata={"disposition": "rejected_validation"},
                    )
            return _domain_error_result(
                code="validation_error",
                message="Tool arguments do not satisfy the registered Pydantic schema.",
                disposition="rejected_validation",
                details={"issues": validation_issues},
            )

        if (
            self.caller_profile == "interactive"
            and name in INTERACTIVE_AUTHORITY_TOOLS
            and normalized_arguments is not None
        ):
            attachment_error = False
            with session_scope() as session:
                utterance = _operator_utterance_authority(session, normalized_arguments)
                if utterance is None:
                    self._finish_invocation(
                        session,
                        invocation_id,
                        status="rejected_authority",
                        normalized_argument_hash=normalized_hash,
                        result_refs=[],
                        result_disposition="rejected_authority",
                        error_code="operator_utterance_authority_required",
                    )
                    if execution_completion_token is not None:
                        ContinuityService(session).complete_execution_lease(
                            execution_completion_token,
                            metadata={"disposition": "rejected_authority"},
                        )
                else:
                    bound_invocation = session.get(ToolInvocation, invocation_id)
                    if bound_invocation is None:
                        raise RuntimeError("ToolInvocation disappeared before authority binding")
                    bound_invocation.actor_ref = utterance.actor_ref
                    bound_invocation.utterance_refs = [utterance.ref_id]
                    semantic_request_ref = normalized_arguments.get("semantic_request_ref")
                    if isinstance(semantic_request_ref, str):
                        bound_invocation.semantic_request_ref = semantic_request_ref
                    attachment_evidence = list(
                        session.scalars(
                            select(AttachmentEvidence).where(
                                AttachmentEvidence.ref_id.in_(utterance.attachment_source_refs)
                            )
                        )
                    )
                    evidence_by_ref = {
                        attachment.ref_id: attachment for attachment in attachment_evidence
                    }
                    unavailable_refs = [
                        ref
                        for ref in utterance.attachment_source_refs
                        if ref not in evidence_by_ref
                        or evidence_by_ref[ref].ingest_state != "available"
                    ]
                    if name in INTERACTIVE_ATTACHMENT_TOOLS and unavailable_refs:
                        attachment_error = True
                        self._finish_invocation(
                            session,
                            invocation_id,
                            status="rejected_validation",
                            normalized_argument_hash=normalized_hash,
                            result_refs=unavailable_refs,
                            result_disposition="attachment_evidence_unavailable",
                            error_code="attachment_evidence_unavailable",
                        )
                        if execution_completion_token is not None:
                            ContinuityService(session).complete_execution_lease(
                                execution_completion_token,
                                metadata={"disposition": "attachment_evidence_unavailable"},
                            )
            if utterance is None:
                return _domain_error_result(
                    code="operator_utterance_authority_required",
                    message=(
                        "Mutating Docket calls require the persisted authenticated "
                        "OperatorUtterance for the current request."
                    ),
                    disposition="rejected_authority",
                )
            if attachment_error:
                return _domain_error_result(
                    code="attachment_evidence_unavailable",
                    message=(
                        "Canonical mutation is blocked until every attachment supplied "
                        "with this utterance has durable plaintext evidence."
                    ),
                    disposition="attachment_evidence_unavailable",
                )

        try:
            result = await super().call_tool(name, arguments)
        except Exception as exc:
            validation_failure = normalized_hash is None
            error_code = "validation_error" if validation_failure else "internal_error"
            status = "rejected_validation" if validation_failure else "failed"
            recovered_result: dict[str, Any] | None = None
            with session_scope() as session:
                self._finish_invocation(
                    session,
                    invocation_id,
                    status=status,
                    normalized_argument_hash=normalized_hash,
                    result_refs=[],
                    result_disposition=status,
                    error_code=error_code,
                )
                completed = session.get(ToolInvocation, invocation_id)
                operation = bound_assembly_operation(session, completed) if completed else None
                if (
                    completed is not None and completed.domain_state != "unknown"
                    and operation is not None and operation.completed_at is not None
                    and operation.state in {"completed", "rejected"}
                    and completed.result_disposition == operation.result_disposition
                ):
                    recovered_result = {**operation.result_json, "reconciled": True}
                if execution_completion_token is not None:
                    ContinuityService(session).complete_execution_lease(
                        execution_completion_token,
                        metadata={"disposition": (
                            recovered_result.get("disposition", status)
                            if recovered_result else status
                        )},
                    )
            if recovered_result is not None:
                return _compact_result(
                    recovered_result, recovered_result, audit=False, page_limit=25,
                )[0]
            return _domain_error_result(
                code=error_code,
                message=(
                    "Tool arguments do not satisfy the registered Pydantic schema."
                    if validation_failure
                    else "Docket encountered an internal processing failure."
                ),
                disposition=status,
                details={"issues": _validation_issues(exc)} if validation_failure else None,
            )

        envelope = _result_envelope(result)
        requested_page_limit = (
            normalized_arguments.get("limit", 25) if normalized_arguments is not None else 25
        )
        page_limit = (
            min(max(requested_page_limit, 1), 100) if isinstance(requested_page_limit, int) else 25
        )
        result, envelope = _compact_result(
            result,
            envelope,
            audit=normalized_arguments is not None and normalized_arguments.get("view") == "audit",
            page_limit=page_limit,
        )
        error: dict[str, Any] = {}
        if envelope is not None and envelope.get("ok") is False:
            candidate = envelope.get("error")
            if isinstance(candidate, dict):
                error = candidate
        error_code_value = error.get("code")
        response_error_code = str(error_code_value)[:128] if error_code_value is not None else None
        status = (
            _terminal_status(response_error_code)
            if response_error_code is not None
            else "succeeded"
        )
        raw_disposition = envelope.get("disposition") if envelope is not None else None
        result_disposition = str(raw_disposition)[:64] if isinstance(raw_disposition, str) else None
        raw_semantic_request_ref = (
            envelope.get("semantic_request_ref") if envelope is not None else None
        )
        result_semantic_request_ref = (
            str(raw_semantic_request_ref) if isinstance(raw_semantic_request_ref, str) else None
        )
        domain_state = _domain_state(status)
        if name in INTERACTIVE_CANONICAL_MUTATION_TOOLS and result_disposition is None:
            # The durable result may have committed even if a faulty tool omitted
            # its disposition. Preserve that uncertainty rather than calling it
            # success or converting it into a fresh authority request.
            result_disposition = "unknown"
            domain_state = "unknown"
        elif result_disposition is None and status != "succeeded":
            result_disposition = status
        with session_scope() as session:
            self._finish_invocation(
                session,
                invocation_id,
                status=status,
                normalized_argument_hash=normalized_hash,
                # Rejected/failed envelopes may mention preallocated refs from a
                # transaction that rolled back. They are diagnostic details, not
                # durable results. The call and its error code remain auditable.
                result_refs=(_collect_result_refs(envelope or {}) if status == "succeeded" else []),
                result_disposition=result_disposition,
                error_code=response_error_code,
                domain_state=domain_state,
                semantic_request_ref=result_semantic_request_ref,
            )
            if execution_completion_token is not None:
                ContinuityService(session).complete_execution_lease(
                    execution_completion_token,
                    metadata={"disposition": result_disposition or status},
                )
        return result
