from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import (
    McpTraceCallUpdate,
    McpTraceCheckpoint,
    McpTraceContext,
    McpTraceUpdate,
    TraceTimingInput,
)
from docket.models import (
    ConversationalToolTrace,
    DiscordDailyThread,
    OperatorUtterance,
    ToolInvocation,
    TraceExecutionSegment,
    TraceTimingObservation,
)
from docket.models.base import utc_now
from docket.services.trace_correlation import correlated_calls
from docket.services.trace_executions import TraceExecutionService, refresh_trace
from docket.tool_contracts import CONTRACT_VERSION, contract_hash, contract_tool_names

VISIBLE_TRACE_CALLS = 20

DOCKET_MCP_TOOL_NAMES = contract_tool_names("interactive")
TRACE_DISPOSITIONS = frozenset(
    {
        "archived",
        "already_committed",
        "assembled_draft_exists",
        "attachment_evidence_unavailable",
        "blocked_version",
        "committed",
        "created",
        "configured",
        "deferred_drain",
        "disabled",
        "draft_revision_conflict",
        "duplicate_suppressed",
        "execution_deferred",
        "failed",
        "matched_existing",
        "needs_clarification",
        "no_op",
        "proposed",
        "execution_queued",
        "rejected_authority",
        "rejected_conflict",
        "rejected_validation",
        "replayed_request",
        "reviewed",
        "restored",
        "ready_to_commit",
        "saved_with_errors",
        "stored",
        "succeeded",
        "unknown",
        "updated",
    }
)
TRACE_ERROR_CODES = frozenset(
    {
        "authorization_failed",
        "blocked",
        "cancelled",
        "docket_error",
        "invalid_result",
        "timeout",
        "transport_error",
        "unknown_error",
    }
)


class McpTraceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _validate_context(self, trace_ref: str, request: McpTraceContext) -> None:
        settings = get_settings()
        trusted_channel = request.source_channel_id == settings.chat_channel_id
        if not trusted_channel:
            trusted_channel = (
                self.session.scalar(
                    select(DiscordDailyThread.id)
                    .where(
                        DiscordDailyThread.guild_id == settings.discord_guild_id,
                        DiscordDailyThread.channel_id == settings.queue_channel_id,
                        DiscordDailyThread.thread_id == request.source_channel_id,
                        DiscordDailyThread.status.in_(("active", "archived")),
                    )
                    .limit(1)
                )
                is not None
            )
        if (
            request.guild_id != settings.discord_guild_id
            or not trusted_channel
            or request.actor_id != settings.operator_discord_user_id
            or not trace_ref.startswith("trace_")
        ):
            raise DocketError(
                code="invalid_mcp_trace_context",
                message=(
                    "The MCP trace is not bound to the configured Docket chat or "
                    "a Docket-owned daily thread."
                ),
            )
        if not request.source_message_id.isascii() or not request.source_message_id.isdecimal():
            raise DocketError(
                code="invalid_mcp_trace_context",
                message="The MCP trace source message identifier is malformed.",
            )
        if (
            request.caller_profile != "interactive"
            or request.tool_contract_version != CONTRACT_VERSION
            or request.tool_contract_hash != contract_hash("interactive")
        ):
            raise DocketError(
                code="invalid_tool_contract",
                message="The MCP trace is not bound to the repository interactive contract.",
            )

    @staticmethod
    def _validate_call(call: McpTraceCallUpdate) -> None:
        if call.tool_name not in DOCKET_MCP_TOOL_NAMES:
            raise DocketError(
                code="invalid_mcp_trace_tool",
                message="The MCP trace names a tool outside Docket's public MCP surface.",
            )
        if call.disposition is not None and call.disposition not in TRACE_DISPOSITIONS:
            raise DocketError(
                code="invalid_mcp_trace_disposition",
                message="The MCP trace disposition is not allowlisted.",
            )
        if call.execution_boundary == "local_rejection" and call.disposition not in {
            None, "failed", "rejected_validation", "rejected_authority", "rejected_conflict",
        }:
            raise DocketError(
                code="invalid_mcp_trace_disposition",
                message="A local rejection cannot claim a successful domain effect.",
            )
        if (
            call.transport_error_code is not None
            and call.transport_error_code not in TRACE_ERROR_CODES
        ):
            raise DocketError(
                code="invalid_mcp_trace_error",
                message="The MCP trace error code is not allowlisted.",
            )

    @staticmethod
    def _stored_call(call: McpTraceCallUpdate) -> dict[str, Any]:
        return {
            "call_id": call.call_id,
            "ordinal": call.ordinal,
            "tool_name": call.tool_name,
            "execution_boundary": call.execution_boundary,
            "transport_state": call.transport_state,
            "domain_state": "unknown",
            "elapsed_ms": call.elapsed_ms,
            "reported_disposition": call.disposition,
            "disposition": call.disposition,
            "transport_error_code": call.transport_error_code,
            "domain_error_code": None,
            "argument_preview": call.argument_preview,
            "received_argument_hash": call.received_argument_hash,
            "tool_call_ref": None,
        }

    @staticmethod
    def _incoming_call(call: McpTraceCallUpdate) -> dict[str, Any]:
        return {
            "call_id": call.call_id,
            "ordinal": call.ordinal,
            "tool_name": call.tool_name,
            "execution_boundary": call.execution_boundary,
            "transport_state": call.transport_state,
            "elapsed_ms": call.elapsed_ms,
            "reported_disposition": call.disposition,
            "transport_error_code": call.transport_error_code,
            "argument_preview": call.argument_preview,
            "received_argument_hash": call.received_argument_hash,
        }

    def _apply_call(
        self,
        trace: TraceExecutionSegment,
        call: McpTraceCallUpdate,
        *,
        checkpoint: bool = False,
    ) -> bool:
        self._validate_call(call)
        calls = [dict(item) for item in trace.calls]
        match = next(
            (
                item
                for item in calls
                if item.get("call_id") == call.call_id
                or int(item.get("ordinal", 0)) == call.ordinal
            ),
            None,
        )
        if match is None:
            if trace.status != "running":
                raise DocketError(
                    code="mcp_trace_terminal",
                    message="A terminal MCP trace cannot accept another call.",
                )
            if call.ordinal != trace.last_ordinal + 1 or (
                call.transport_state != "running" and not checkpoint
            ):
                raise DocketError(
                    code="nonmonotonic_mcp_trace",
                    message="MCP trace calls must begin in monotonic ordinal order.",
                )
            calls.append(self._stored_call(call))
            trace.calls = calls
            trace.last_ordinal = call.ordinal
            return True

        if match.get("call_id") != call.call_id or int(match.get("ordinal", 0)) != call.ordinal:
            raise DocketError(
                code="mcp_trace_call_conflict",
                message="The MCP trace call identifier or ordinal was reused.",
            )
        if (
            match.get("tool_name") != call.tool_name
            or match.get("execution_boundary") != call.execution_boundary
            or match.get("received_argument_hash") != call.received_argument_hash
            or match.get("argument_preview") != call.argument_preview
        ):
            raise DocketError(
                code="mcp_trace_call_conflict",
                message="The MCP trace call tool or argument binding changed.",
            )
        current_state = str(match.get("transport_state", match.get("state")))
        if current_state != "running" and call.transport_state == "running":
            # An acknowledged checkpoint can overtake its asynchronous start
            # callback. The old observation cannot regress durable completion.
            return False
        if current_state == call.transport_state:
            if any(match.get(key) != value for key, value in self._incoming_call(call).items()):
                raise DocketError(
                    code="mcp_trace_call_conflict",
                    message="A replayed MCP trace call changed terminal details.",
                )
            return False
        if current_state != "running" or call.transport_state == "running":
            raise DocketError(
                code="mcp_trace_state_regression",
                message="An MCP trace call cannot regress or change terminal state.",
            )
        match.update(self._incoming_call(call))
        match["disposition"] = call.disposition
        trace.calls = calls
        return True

    def _validate_invocation_context(
        self, trace: TraceExecutionSegment, invocation: ToolInvocation,
    ) -> None:
        parent = self.session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == trace.trace_ref,
        ))
        if parent is None:
            raise DocketError(code="mcp_trace_binding_mismatch", message="Trace source is missing.")
        expected_source = (
            f"discord_message:{parent.guild_id}:{parent.source_channel_id}:{parent.source_message_id}"
        )
        utterance = self.session.scalar(select(OperatorUtterance).where(
            OperatorUtterance.ref_id.in_(invocation.utterance_refs),
            OperatorUtterance.source_message_ref == expected_source,
            OperatorUtterance.actor_ref == f"discord_user:{parent.actor_id}",
            OperatorUtterance.transport == "discord",
        ))
        if (
            len(invocation.utterance_refs) != 1 or utterance is None
            or invocation.actor_ref != utterance.actor_ref
            or invocation.gateway_instance_ref != trace.gateway_instance_ref
            or invocation.trace_execution_id != trace.id
        ):
            raise DocketError(
                code="mcp_trace_binding_mismatch",
                message="The trace source disagrees with its authenticated invocation binding.",
            )

    def _link_tool_invocation(
        self,
        trace: TraceExecutionSegment,
        call: McpTraceCallUpdate,
    ) -> ToolInvocation | None:
        existing = correlated_calls(list(self.session.scalars(
            select(ToolInvocation).where(ToolInvocation.trace_execution_id == trace.id)
        ))).get((trace.id, call.call_id))
        if existing is not None and (
            existing.tool_name != call.tool_name
            or existing.trace_ordinal != call.ordinal
            or existing.received_argument_hash != call.received_argument_hash
            or call.execution_boundary == "local_rejection"
        ):
            raise DocketError(
                code="mcp_trace_call_conflict",
                message="The callback disagrees with its authenticated invocation binding.",
            )
        if existing is not None:
            self._validate_invocation_context(trace, existing)
        return existing

    @staticmethod
    def _domain_state(invocation: ToolInvocation | None) -> str:
        if invocation is None:
            return "unknown"
        return invocation.domain_state

    def _reconcile_calls(self, trace: TraceExecutionSegment) -> bool:
        changed = False
        calls = [dict(item) for item in trace.calls]
        invocations = correlated_calls(list(self.session.scalars(
            select(ToolInvocation).where(ToolInvocation.trace_execution_id == trace.id)
        )))
        for call in calls:
            call_id = str(call.get("call_id", ""))
            invocation = invocations.get((trace.id, call_id))
            if invocation is not None and (
                invocation.tool_name != call.get("tool_name")
                or invocation.trace_ordinal != call.get("ordinal")
                or invocation.received_argument_hash != call.get("received_argument_hash")
                or call.get("execution_boundary") == "local_rejection"
            ):
                raise DocketError(
                    code="mcp_trace_call_conflict",
                    message="The retained trace disagrees with its invocation binding.",
                )
            if invocation is not None:
                self._validate_invocation_context(trace, invocation)
            domain_state = self._domain_state(invocation)
            authoritative = {
                "domain_state": domain_state,
                "tool_call_ref": invocation.ref_id if invocation is not None else None,
                "domain_error_code": (
                    invocation.error_code
                    if invocation is not None and domain_state in {"rejected", "failed"}
                    else None
                ),
            }
            if invocation is not None:
                authoritative["disposition"] = (
                    invocation.result_disposition if domain_state != "unknown" else None
                )
            elif call.get("execution_boundary") == "local_rejection":
                if "reported_disposition" in call:
                    authoritative["disposition"] = call["reported_disposition"]
                # Do not erase older retained local evidence or invent a
                # separate report when no such observation was captured.
            elif call.get("disposition") != "rejected_validation":
                # Unlinked transport observations cannot establish Docket's
                # domain outcome. Keep the original report separately so a
                # checkpoint replay need not contradict reconciliation.
                authoritative["disposition"] = None
            if any(call.get(key) != value for key, value in authoritative.items()):
                call.update(authoritative)
                changed = True
        if changed:
            trace.calls = calls
        return changed

    @staticmethod
    def _finish_running_calls(trace: TraceExecutionSegment) -> bool:
        changed = False
        calls = [dict(item) for item in trace.calls]
        for call in calls:
            if call.get("transport_state", call.get("state")) == "running":
                call.update(
                    {
                        "transport_state": "timed_out",
                        "elapsed_ms": min(int(call.get("elapsed_ms", 0)), 600_000),
                        "disposition": None,
                        "transport_error_code": "timeout",
                    }
                )
                call.pop("state", None)
                changed = True
        if changed:
            trace.calls = calls
        return changed

    def _trace(self, trace_ref: str, request: McpTraceContext) -> TraceExecutionSegment:
        self._validate_context(trace_ref, request)
        parent, segment = TraceExecutionService(self.session).require(
            trace_ref=trace_ref, execution_index=request.execution_index,
            execution_completion_token=request.execution_completion_token,
            gateway_instance_ref=request.gateway_instance_ref,
        )
        def utc(value: datetime) -> datetime:
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if (
            parent.guild_id != request.guild_id
            or parent.source_channel_id != request.source_channel_id
            or parent.source_message_id != request.source_message_id
            or parent.actor_id != request.actor_id
            or segment.tool_contract_version != request.tool_contract_version
            or segment.tool_contract_hash != request.tool_contract_hash
            or segment.caller_profile != request.caller_profile
            or utc(segment.started_at) != request.turn_started_at
        ):
            raise DocketError(code="mcp_trace_binding_mismatch",
                              message="The trace execution binding changed.")
        return segment

    def update(self, trace_ref: str, request: McpTraceUpdate) -> dict[str, Any]:
        trace = self._trace(trace_ref, request)
        changed = False
        tool_call_ref: str | None = None
        if request.call is not None:
            changed = self._apply_call(trace, request.call)
            invocation = self._link_tool_invocation(trace, request.call)
            tool_call_ref = invocation.ref_id if invocation is not None else None
            changed = self._reconcile_calls(trace) or changed
        if request.timing is not None:
            changed = self._apply_timing(trace, request.timing, request.updated_at) or changed
        return self._finish_update(trace, request, changed, tool_call_ref)

    def _apply_timing(
        self, trace: TraceExecutionSegment, timing: TraceTimingInput, captured_at: datetime,
    ) -> bool:
        def utc(value: datetime) -> datetime:
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

        if (
            timing.started_at < utc(trace.started_at) or timing.ended_at > utc(captured_at)
        ):
            raise DocketError(
                code="invalid_trace_timing", message="Timing is outside its captured turn.",
            )
        prior = self.session.get(TraceTimingObservation, timing.span_id)
        if prior is not None:
            if (
                prior.trace_ref != trace.trace_ref or prior.trace_execution_id != trace.id
                or prior.phase != timing.phase
                or utc(prior.started_at) != timing.started_at
                or utc(prior.ended_at) != timing.ended_at
            ):
                raise DocketError(
                    code="trace_timing_conflict", message="Timing evidence cannot be rewritten.",
                )
            return False
        if trace.status != "running":
            raise DocketError(
                code="mcp_trace_terminal", message="A terminal trace cannot add timing.",
            )
        self.session.add(
            TraceTimingObservation(
                id=timing.span_id,
                trace_ref=trace.trace_ref,
                trace_execution_id=trace.id,
                phase=timing.phase,
                started_at=timing.started_at,
                ended_at=timing.ended_at,
            )
        )
        self.session.flush()
        return True

    def checkpoint(self, trace_ref: str, request: McpTraceCheckpoint) -> dict[str, Any]:
        # A dropped callback queue is not durable evidence. Recover the actual
        # bounded observations from the same captured utterance, in one page
        # transaction. Never fill ordinal gaps or manufacture call_ records.
        source_ref = (
            f"discord_message:{request.guild_id}:{request.source_channel_id}:"
            f"{request.source_message_id}"
        )
        utterance = self.session.scalar(select(OperatorUtterance).where(
            OperatorUtterance.ref_id == request.utterance_ref,
            OperatorUtterance.source_message_ref == source_ref,
            OperatorUtterance.actor_ref == f"discord_user:{request.actor_id}",
            OperatorUtterance.transport == "discord",
        ))
        if utterance is None:
            raise DocketError(
                code="mcp_trace_binding_mismatch",
                message="Trace checkpoint does not match its captured Operator utterance.",
            )
        with self.session.begin_nested():
            trace = self._trace(trace_ref, request)
            changed = False
            for call in request.calls:
                self._link_tool_invocation(trace, call)
                changed = self._apply_call(trace, call, checkpoint=True) or changed
            for timing in request.timings:
                changed = self._apply_timing(trace, timing, request.updated_at) or changed
            return self._finish_update(trace, request, changed, None)

    def _finish_update(
        self,
        trace: TraceExecutionSegment,
        request: McpTraceContext,
        changed: bool,
        tool_call_ref: str | None,
    ) -> dict[str, Any]:
        if request.turn_status != "running":
            target_status = request.turn_status
            if trace.status == "running":
                changed = self._finish_running_calls(trace) or changed
                trace.status = target_status
                trace.completed_at = utc_now()
                changed = True
            elif trace.status != target_status:
                raise DocketError(
                    code="mcp_trace_state_regression",
                    message="A terminal MCP trace cannot change terminal state.",
                )
        changed = self._reconcile_calls(trace) or changed
        parent = self.session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == trace.trace_ref,
        ).with_for_update())
        if parent is None:
            raise RuntimeError("Trace source disappeared")
        if changed:
            refresh_trace(self.session, parent)
        return {
            "ok": True,
            "trace_ref": parent.ref_id,
            "execution_index": trace.execution_index,
            "execution_status": trace.status,
            "trace_status": parent.status,
            "trace_version": parent.version,
            "disposition": "updated" if changed else "replayed_request",
            "tool_call_ref": tool_call_ref,
        }
