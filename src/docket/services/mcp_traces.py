from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.enums import OutboxStatus
from docket.domain.errors import DocketError
from docket.internal_api.schemas import McpTraceCallUpdate, McpTraceUpdate
from docket.models import DiscordDailyThread, DiscordMcpTrace, OutboxEvent
from docket.models.base import utc_now

MCP_TRACE_NAMESPACE = uuid.UUID("326f8ee5-f0d5-4d08-b777-31dbac1f8265")
MAX_TRACE_CALLS = 100
VISIBLE_TRACE_CALLS = 20

DOCKET_MCP_TOOL_NAMES = frozenset(
    {
        "docket_archive_record",
        "docket_get_action",
        "docket_get_calendar_profile",
        "docket_get_calendar_sync_status",
        "docket_get_queue_item",
        "docket_get_record",
        "docket_ignore_queue_item",
        "docket_list_accounts",
        "docket_list_calendar_events",
        "docket_list_queue_items",
        "docket_list_reminder_rules",
        "docket_propose_action",
        "docket_propose_calendar_event",
        "docket_propose_course_reconciliation",
        "docket_propose_term_schedule",
        "docket_restore_record",
        "docket_search_records",
        "docket_set_calendar_profile",
        "docket_snooze_queue_item",
        "docket_store_record",
        "docket_store_term_schedule",
        "docket_update_record",
    }
)
TRACE_DISPOSITIONS = frozenset(
    {
        "archived",
        "created",
        "disabled",
        "duplicate_suppressed",
        "matched_existing",
        "no_op",
        "proposed",
        "replayed_request",
        "restored",
        "stored",
        "succeeded",
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


def trace_id_for_source(guild_id: str, channel_id: str, message_id: str) -> uuid.UUID:
    return uuid.uuid5(MCP_TRACE_NAMESPACE, f"{guild_id}:{channel_id}:{message_id}")


class McpTraceService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _validate_context(self, trace_id: uuid.UUID, request: McpTraceUpdate) -> None:
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
            or trace_id
            != trace_id_for_source(
                request.guild_id,
                request.source_channel_id,
                request.source_message_id,
            )
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
        if call.error_code is not None and call.error_code not in TRACE_ERROR_CODES:
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
            "state": call.state,
            "elapsed_ms": call.elapsed_ms,
            "disposition": call.disposition,
            "error_code": call.error_code,
        }

    def _apply_call(
        self,
        trace: DiscordMcpTrace,
        call: McpTraceCallUpdate,
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
            if call.ordinal != trace.last_ordinal + 1 or call.state != "running":
                raise DocketError(
                    code="nonmonotonic_mcp_trace",
                    message="MCP trace calls must begin in monotonic ordinal order.",
                )
            if len(calls) >= MAX_TRACE_CALLS:
                raise DocketError(
                    code="mcp_trace_limit_exceeded",
                    message="The MCP trace exceeded its bounded call limit.",
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
        if match.get("tool_name") != call.tool_name:
            raise DocketError(
                code="mcp_trace_call_conflict",
                message="The MCP trace call tool binding changed.",
            )
        current_state = str(match.get("state"))
        if current_state == call.state:
            if self._stored_call(call) != match:
                raise DocketError(
                    code="mcp_trace_call_conflict",
                    message="A replayed MCP trace call changed terminal details.",
                )
            return False
        if current_state != "running" or call.state == "running":
            raise DocketError(
                code="mcp_trace_state_regression",
                message="An MCP trace call cannot regress or change terminal state.",
            )
        match.update(self._stored_call(call))
        trace.calls = calls
        return True

    @staticmethod
    def _finish_running_calls(trace: DiscordMcpTrace) -> bool:
        changed = False
        calls = [dict(item) for item in trace.calls]
        for call in calls:
            if call.get("state") == "running":
                call.update(
                    {
                        "state": "timed_out",
                        "elapsed_ms": min(int(call.get("elapsed_ms", 0)), 600_000),
                        "disposition": None,
                        "error_code": "timeout",
                    }
                )
                changed = True
        if changed:
            trace.calls = calls
        return changed

    def update(self, trace_id: uuid.UUID, request: McpTraceUpdate) -> dict[str, Any]:
        self._validate_context(trace_id, request)
        trace = self.session.scalar(
            select(DiscordMcpTrace).where(DiscordMcpTrace.id == trace_id).with_for_update()
        )
        now = utc_now()
        if trace is None:
            trace = DiscordMcpTrace(
                id=trace_id,
                guild_id=request.guild_id,
                source_channel_id=request.source_channel_id,
                source_message_id=request.source_message_id,
                actor_id=request.actor_id,
                status="running",
                calls=[],
                last_ordinal=0,
                version=0,
                started_at=now,
            )
            self.session.add(trace)
            self.session.flush()
        elif (
            trace.guild_id != request.guild_id
            or trace.source_channel_id != request.source_channel_id
            or trace.source_message_id != request.source_message_id
            or trace.actor_id != request.actor_id
        ):
            raise DocketError(
                code="mcp_trace_binding_mismatch",
                message="The MCP trace source binding changed.",
            )

        changed = False
        if request.call is not None:
            changed = self._apply_call(trace, request.call)
        if request.turn_status != "running":
            target_status = request.turn_status
            if trace.status == "running":
                changed = self._finish_running_calls(trace) or changed
                trace.status = target_status
                trace.completed_at = now
                changed = True
            elif trace.status != target_status:
                raise DocketError(
                    code="mcp_trace_state_regression",
                    message="A terminal MCP trace cannot change terminal state.",
                )
        if not changed:
            return {
                "ok": True,
                "trace_id": str(trace.id),
                "trace_status": trace.status,
                "trace_version": trace.version,
                "disposition": "replayed_request",
            }

        trace.version += 1
        self.session.add(
            OutboxEvent(
                event_type="discord.mcp_trace.requested",
                aggregate_type="discord_mcp_trace",
                aggregate_id=trace.id,
                deduplication_key=f"discord_mcp_trace:{trace.id}:v{trace.version}",
                payload={
                    "trace_id": str(trace.id),
                    "trace_version": trace.version,
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        return {
            "ok": True,
            "trace_id": str(trace.id),
            "trace_status": trace.status,
            "trace_version": trace.version,
            "disposition": "updated",
        }
