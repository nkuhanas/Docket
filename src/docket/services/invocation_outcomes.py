"""Recover invocation outcomes from their exact durable assembly operation.

A request's later state is not evidence of what an earlier tool call did.
Recovery reads committed records only; it never replays a semantic operation.
"""

from __future__ import annotations

import re
from datetime import UTC

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from docket.models import AssemblyOperation, ChangeSet, OperatorUtterance, ToolInvocation


def is_gateway_unknown(invocation: ToolInvocation) -> bool:
    return (
        invocation.domain_state == "unknown"
        and invocation.error_code == "gateway_interrupted"
    )


def gateway_recovery_pending() -> ColumnElement[bool]:
    """Do not continually lock evidence-free unknown calls on retired gateways."""
    original = aliased(ToolInvocation)
    terminal = select(AssemblyOperation.id).join(original, and_(
        original.trace_ref == AssemblyOperation.trace_ref,
        original.trace_call_id == AssemblyOperation.upstream_tool_call_id,
        original.utterance_refs[0].as_string() == AssemblyOperation.source_utterance_ref,
        original.tool_name == AssemblyOperation.tool_name,
        original.received_argument_hash == AssemblyOperation.argument_hash,
    )).where(
        AssemblyOperation.state.in_(("completed", "rejected")),
        AssemblyOperation.completed_at.is_not(None),
        AssemblyOperation.result_disposition != "unknown",
        original.trace_ref == ToolInvocation.trace_ref,
        original.trace_ordinal == ToolInvocation.trace_ordinal,
        original.tool_name == ToolInvocation.tool_name,
        original.received_argument_hash == ToolInvocation.received_argument_hash,
        original.actor_ref == ToolInvocation.actor_ref,
        original.utterance_refs[0].as_string() == ToolInvocation.utterance_refs[0].as_string(),
        original.gateway_instance_ref.is_not_distinct_from(ToolInvocation.gateway_instance_ref),
        original.caller_profile == ToolInvocation.caller_profile,
        original.tool_contract_version == ToolInvocation.tool_contract_version,
        original.tool_contract_hash == ToolInvocation.tool_contract_hash,
        or_(ToolInvocation.trace_call_id.is_(None), original.id == ToolInvocation.id),
    ).correlate(ToolInvocation).exists()
    return or_(
        ToolInvocation.transport_state == "running",
        and_(
            ToolInvocation.domain_state == "unknown",
            ToolInvocation.error_code == "gateway_interrupted",
            terminal,
        ),
    )


def bound_assembly_operation(
    session: Session, invocation: ToolInvocation,
) -> AssemblyOperation | None:
    if (
        invocation.caller_profile != "interactive"
        or invocation.trace_ref is None
        or invocation.trace_ordinal is None
        or len(invocation.utterance_refs) != 1
    ):
        return None
    original = invocation
    if invocation.trace_call_id is None:
        # Retransmission has its own call_, but only an exact original signed
        # binding can recover its upstream operation identity. Never match a
        # nearby invocation by tool name, request ref or argument hash alone.
        originals = list(session.scalars(select(ToolInvocation).where(
            ToolInvocation.trace_ref == invocation.trace_ref,
            ToolInvocation.trace_ordinal == invocation.trace_ordinal,
            ToolInvocation.trace_call_id.is_not(None),
        )))
        if len(originals) != 1:
            return None
        original = originals[0]
        if any(getattr(original, field) != getattr(invocation, field) for field in (
            "tool_name", "received_argument_hash", "actor_ref", "utterance_refs",
            "gateway_instance_ref", "caller_profile", "tool_contract_version",
            "tool_contract_hash",
        )):
            return None
    utterance = session.scalar(select(OperatorUtterance).where(
        OperatorUtterance.ref_id == invocation.utterance_refs[0],
    ))
    if utterance is None or utterance.transport != "discord" or (
        utterance.actor_ref != invocation.actor_ref
    ):
        return None
    return session.scalar(select(AssemblyOperation).where(
        AssemblyOperation.trace_ref == original.trace_ref,
        AssemblyOperation.upstream_tool_call_id == original.trace_call_id,
        AssemblyOperation.source_utterance_ref == utterance.ref_id,
        AssemblyOperation.tool_name == invocation.tool_name,
        AssemblyOperation.argument_hash == invocation.received_argument_hash,
    ))


def recover_assembly_outcome(session: Session, invocation: ToolInvocation) -> bool:
    """Caller holds the invocation row lock; known terminal results are final."""
    if invocation.transport_state != "running" and not is_gateway_unknown(invocation):
        return False
    operation = bound_assembly_operation(session, invocation)
    if operation is None or operation.state not in {"completed", "rejected"} or (
        operation.completed_at is None
    ):
        return False
    result = operation.result_json
    disposition = operation.result_disposition
    if result.get("disposition") != disposition:
        return False
    succeeded = {
        "ready_to_commit", "saved_with_errors", "no_op", "reviewed",
        "committed", "already_committed", "replayed_request",
    }
    rejected = {"draft_revision_conflict", "rejected_validation", "rejected_conflict"}
    if operation.state == "completed" and result.get("ok") is True and disposition in succeeded:
        domain_state = "succeeded"
        error_code = None
    elif operation.state == "rejected" and result.get("ok") is False and disposition in rejected:
        domain_state = "rejected"
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        error_code = (
            code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,128}", code) else None
        )
        if any(word in (error_code or "") for word in ("internal", "runtime", "service_exception")):
            domain_state = "failed"
    else:
        # A recorded unknown outcome is still unknown; it is not a rejection
        # or success merely because its invocation/operation stopped running.
        return False
    changeset: ChangeSet | None = None
    if disposition in {"committed", "already_committed", "replayed_request"}:
        committed_ref = operation.change_set_ref or result.get("changeset_ref")
        changeset = session.scalar(select(ChangeSet).where(
            ChangeSet.ref_id == committed_ref,
        )) if isinstance(committed_ref, str) else None
        if changeset is None or changeset.state != "committed" or (
            changeset.semantic_request_ref != operation.semantic_request_ref
        ):
            return False
    invocation.transport_state = "completed"
    invocation.domain_state = domain_state
    invocation.result_disposition = disposition
    invocation.error_code = error_code
    # A retry may start after the original operation already finished. Do not
    # give that new invocation a completion timestamp before its own start.
    invocation.completed_at = max(
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        for value in (operation.completed_at, invocation.started_at)
    )
    invocation.semantic_request_ref = operation.semantic_request_ref
    # Do not copy receipts, titles, arguments or source text into call_. These
    # exact typed links suffice to inspect the separately retained evidence.
    invocation.result_refs = [ref for ref in (
        operation.semantic_request_ref, operation.semantic_request_attempt_ref,
        changeset.ref_id if disposition in {"committed", "already_committed", "replayed_request"}
        and changeset is not None else operation.change_set_ref,
    ) if ref is not None]
    return True
