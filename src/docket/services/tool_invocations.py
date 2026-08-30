from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.public_refs import is_public_ref
from docket.models import SemanticRequestAttempt, ToolInvocation
from docket.models.base import utc_now
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _result_refs(value: Any) -> list[str]:
    refs: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str) and is_public_ref(item):
            if item not in refs:
                refs.append(item)
            return
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list | tuple):
            for child in item:
                visit(child)

    visit(value)
    return refs


def _status(disposition: str, ok: bool) -> tuple[str, str]:
    if ok:
        return "succeeded", "succeeded"
    if disposition == "rejected_authority":
        return "rejected_authority", "rejected"
    if disposition == "rejected_conflict":
        return "rejected_conflict", "rejected"
    if disposition in {"rejected_validation", "blocked_version"}:
        return "rejected_validation", "rejected"
    return "failed", "failed"


class ToolInvocationService:
    """Record deterministic internal execution through a public tool contract.

    Semantic-option selections are compiled by Docket rather than by Hermes, but
    they still execute the exact ``docket_commit_changeset`` contract. Recording
    that boundary here keeps rejected and successful attempts in the same
    forensic ledger as MCP-originated calls.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def start_changeset_execution(
        self,
        *,
        request_id: UUID,
        trace_ref: str,
        utterance_ref: str,
        actor_ref: str,
        intent_session_ref: str,
        semantic_request_ref: str,
        case_ref: str | None,
        gateway_instance_ref: str | None,
        arguments: dict[str, Any],
    ) -> ToolInvocation:
        trace_ordinal = int(
            self.session.scalar(
                select(func.max(ToolInvocation.trace_ordinal)).where(
                    ToolInvocation.trace_ref == trace_ref
                )
            )
            or 0
        ) + 1
        invocation = ToolInvocation(
            tool_name="docket_commit_changeset",
            tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"),
            caller_profile="interactive",
            actor_ref=actor_ref,
            utterance_refs=[utterance_ref],
            intent_session_ref=intent_session_ref,
            case_ref=case_ref,
            received_argument_hash=sha256_json(arguments),
            normalized_argument_hash=None,
            result_refs=[],
            transport_state="running",
            domain_state="unknown",
            semantic_request_ref=semantic_request_ref,
            gateway_instance_ref=gateway_instance_ref,
            mcp_request_id=str(request_id),
            trace_ref=trace_ref,
            trace_call_id=f"semantic-request:{semantic_request_ref}:{request_id}",
            trace_ordinal=trace_ordinal,
        )
        self.session.add(invocation)
        self.session.flush()
        return invocation

    def finish(
        self,
        invocation_ref: str,
        *,
        normalized_arguments: dict[str, Any] | None,
        result: dict[str, Any],
        error_code: str | None = None,
    ) -> ToolInvocation:
        invocation = self.session.scalar(
            select(ToolInvocation)
            .where(ToolInvocation.ref_id == invocation_ref)
            .with_for_update()
        )
        if invocation is None:
            raise RuntimeError("ToolInvocation disappeared before completion")
        if invocation.transport_state != "running":
            return invocation
        disposition = str(result.get("disposition") or "unknown")[:64]
        ok = bool(result.get("ok") is True)
        _status_name, domain_state = _status(disposition, ok)
        invocation.normalized_argument_hash = (
            sha256_json(normalized_arguments)
            if normalized_arguments is not None
            else None
        )
        invocation.result_refs = _result_refs(result) if ok else []
        invocation.result_disposition = disposition
        invocation.transport_state = "completed"
        invocation.domain_state = domain_state
        invocation.error_code = error_code[:128] if error_code is not None else None
        invocation.completed_at = utc_now()
        attempt = self.session.scalar(
            select(SemanticRequestAttempt)
            .where(
                SemanticRequestAttempt.semantic_request_ref
                == invocation.semantic_request_ref,
                SemanticRequestAttempt.tool_call_ref.is_(None),
            )
            .order_by(SemanticRequestAttempt.attempt_number.desc())
            .with_for_update()
        )
        if attempt is not None:
            attempt.tool_call_ref = invocation.ref_id
            if attempt.completed_at is None and attempt.state != "pending":
                attempt.completed_at = invocation.completed_at
        return invocation
