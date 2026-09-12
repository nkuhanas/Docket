"""Source-wide trace identity and exact, immutable execution ownership.

Only trusted infrastructure sees the completion token. Neither this service nor
its trace bindings grant semantic authority or start model/provider work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.enums import OutboxStatus
from docket.domain.errors import DocketError
from docket.models import (
    ConversationalToolTrace,
    DeferredIngress,
    DiscordDailyThread,
    ExecutionLease,
    OperatorUtterance,
    OutboxEvent,
    TraceExecutionSegment,
)
from docket.models.base import utc_now
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def refresh_trace(session: Session, trace: ConversationalToolTrace) -> None:
    """Refresh one projection, never rewrite another execution's state."""
    session.flush()
    latest = session.scalar(
        select(TraceExecutionSegment)
        .where(
            TraceExecutionSegment.trace_ref == trace.ref_id,
        )
        .order_by(TraceExecutionSegment.execution_index.desc())
        .limit(1)
    )
    if latest is not None:
        trace.status, trace.completed_at = latest.status, latest.completed_at
    trace.version += 1
    session.add(
        OutboxEvent(
            event_type="discord.mcp_trace.requested",
            aggregate_type="conversational_tool_trace",
            aggregate_id=trace.id,
            deduplication_key=f"conversational_tool_trace:{trace.ref_id}:v{trace.version}",
            payload={"trace_ref": trace.ref_id, "trace_version": trace.version},
            status=OutboxStatus.PENDING.value,
        )
    )


class TraceExecutionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _invalid(code: str = "trace_execution_binding_mismatch") -> DocketError:
        return DocketError(
            code=code,
            message="Trace capture requires this message's admitted execution.",
            details={"next_action": "recover_admitted_execution", "authority_preserved": True},
        )

    def _source(self, utterance_ref: str) -> tuple[OperatorUtterance, str, str, str]:
        source = self.session.scalar(
            select(OperatorUtterance)
            .where(
                OperatorUtterance.ref_id == utterance_ref,
            )
            .with_for_update()
        )
        settings = get_settings()
        if (
            source is None
            or source.transport != "discord"
            or source.actor_ref != (f"discord_user:{settings.operator_discord_user_id}")
        ):
            raise self._invalid()
        parts = source.source_message_ref.split(":")
        if (
            len(parts) != 4
            or parts[0] != "discord_message"
            or parts[1] != settings.discord_guild_id
        ):
            raise self._invalid()
        guild, channel, message = parts[1:]
        trusted = channel in {settings.chat_channel_id, settings.queue_channel_id} or (
            self.session.scalar(
                select(DiscordDailyThread.id).where(
                    DiscordDailyThread.guild_id == guild,
                    DiscordDailyThread.channel_id == settings.queue_channel_id,
                    DiscordDailyThread.thread_id == channel,
                    DiscordDailyThread.status.in_(("active", "archived")),
                )
            )
            is not None
        )
        if not trusted:
            raise self._invalid()
        return source, guild, channel, message

    def _lease(
        self,
        token: str,
        utterance_ref: str,
        gateway_ref: str,
        *,
        active: bool,
    ) -> ExecutionLease:
        GatewayLifetimeService(self.session).require_live(gateway_ref)
        lease = self.session.scalar(
            select(ExecutionLease)
            .where(
                ExecutionLease.completion_token == token,
            )
            .with_for_update()
        )
        if (
            lease is None
            or lease.lease_kind != "interactive_turn"
            or (
                lease.subject_ref != utterance_ref
                or lease.gateway_instance_ref != gateway_ref
                or lease.status not in ({"active"} if active else {"active", "completed"})
            )
        ):
            raise self._invalid()
        now = self.session.scalar(select(func.current_timestamp()))
        if active and (not isinstance(now, datetime) or _utc(lease.lease_expires_at) <= _utc(now)):
            raise self._invalid("trace_execution_expired")
        if active:
            ingress = self.session.scalar(
                select(DeferredIngress).where(
                    DeferredIngress.utterance_ref == utterance_ref,
                )
            )
            if ingress is not None and (
                ingress.status != "claimed"
                or ingress.claimed_by_gateway_ref != gateway_ref
                or lease.lease_key != f"interactive:{utterance_ref}:{ingress.claim_token}"
            ):
                raise self._invalid()
        return lease

    def bind(
        self,
        *,
        utterance_ref: str,
        execution_completion_token: str,
        gateway_instance_ref: str,
        tool_contract_version: str,
        tool_contract_hash: str,
        turn_started_at: datetime,
    ) -> dict[str, Any]:
        source, guild, channel, message = self._source(utterance_ref)
        if tool_contract_version != CONTRACT_VERSION or tool_contract_hash != contract_hash(
            "interactive"
        ):
            raise self._invalid("invalid_tool_contract")
        lease = self._lease(
            execution_completion_token, utterance_ref, gateway_instance_ref, active=True
        )
        trace = self.session.scalar(
            select(ConversationalToolTrace)
            .where(
                ConversationalToolTrace.guild_id == guild,
                ConversationalToolTrace.source_channel_id == channel,
                ConversationalToolTrace.source_message_id == message,
            )
            .with_for_update()
        )
        existing = self.session.scalar(
            select(TraceExecutionSegment).where(
                TraceExecutionSegment.execution_lease_id == lease.id,
            )
        )
        if existing is not None:
            if (
                trace is None
                or existing.trace_ref != trace.ref_id
                or (
                    existing.gateway_instance_ref != gateway_instance_ref
                    or existing.tool_contract_version != tool_contract_version
                    or existing.tool_contract_hash != tool_contract_hash
                )
            ):
                raise self._invalid()
            return self._result(trace, existing, replayed=True)
        if trace is None:
            trace = ConversationalToolTrace(
                guild_id=guild,
                source_channel_id=channel,
                source_message_id=message,
                actor_id=source.actor_ref.removeprefix("discord_user:"),
                status="running",
                started_at=turn_started_at,
                version=0,
            )
            self.session.add(trace)
            self.session.flush()
        elif trace.actor_id != source.actor_ref.removeprefix("discord_user:"):
            raise self._invalid()
        previous = self.session.scalar(
            select(TraceExecutionSegment)
            .where(
                TraceExecutionSegment.trace_ref == trace.ref_id,
            )
            .order_by(TraceExecutionSegment.execution_index.desc())
            .limit(1)
            .with_for_update()
        )
        if previous is not None and GatewayLifetimeService(
            self.session
        ).utterance_execution_finalized(
            utterance_ref=utterance_ref,
        ):
            raise self._invalid("utterance_execution_finalized")
        if previous is not None and previous.status == "running":
            previous_lease = (
                self.session.get(ExecutionLease, previous.execution_lease_id)
                if (previous.execution_lease_id is not None)
                else None
            )
            if previous_lease is None or previous_lease.status != "active":
                # Concurrent admitted attempts keep independent state; the
                # ingress service owns exclusive message dispatch. A new binding
                # cannot itself terminate another still-admitted execution.
                previous.status, previous.completed_at = "interrupted", utc_now()
        segment = TraceExecutionSegment(
            trace_ref=trace.ref_id,
            execution_index=1 if previous is None else previous.execution_index + 1,
            execution_lease_id=lease.id,
            binding_basis="admitted_execution",
            gateway_instance_ref=gateway_instance_ref,
            tool_contract_version=tool_contract_version,
            tool_contract_hash=tool_contract_hash,
            caller_profile="interactive",
            started_at=turn_started_at,
            status="running",
            calls=[],
            last_ordinal=0,
        )
        self.session.add(segment)
        refresh_trace(self.session, trace)
        return self._result(trace, segment, replayed=False)

    @staticmethod
    def _result(
        trace: ConversationalToolTrace, segment: TraceExecutionSegment, *, replayed: bool
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "trace_ref": trace.ref_id,
            "execution_index": segment.execution_index,
            "turn_started_at": _utc(segment.started_at).isoformat(),
            "next_ordinal": segment.last_ordinal + 1,
            "trace_version": trace.version,
            "disposition": "replayed_request" if replayed else "bound",
        }

    def require(
        self,
        *,
        trace_ref: str,
        execution_index: int,
        execution_completion_token: str,
        gateway_instance_ref: str,
        active: bool = False,
    ) -> tuple[ConversationalToolTrace, TraceExecutionSegment]:
        # Source-first locking agrees with admission, MCP binding and staging.
        trace = self.session.scalar(
            select(ConversationalToolTrace).where(
                ConversationalToolTrace.ref_id == trace_ref,
            )
        )
        if trace is None:
            raise self._invalid("trace_execution_not_bound")
        source = self.session.scalar(
            select(OperatorUtterance)
            .where(
                OperatorUtterance.source_message_ref
                == (
                    f"discord_message:{trace.guild_id}:{trace.source_channel_id}:{trace.source_message_id}"
                ),
                OperatorUtterance.actor_ref == f"discord_user:{trace.actor_id}",
                OperatorUtterance.transport == "discord",
            )
            .with_for_update()
        )
        if source is None:
            raise self._invalid()
        lease = self._lease(
            execution_completion_token, source.ref_id, gateway_instance_ref, active=active
        )
        trace = self.session.scalar(
            select(ConversationalToolTrace)
            .where(
                ConversationalToolTrace.ref_id == trace_ref,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        segment = self.session.scalar(
            select(TraceExecutionSegment)
            .where(
                TraceExecutionSegment.trace_ref == trace_ref,
                TraceExecutionSegment.execution_index == execution_index,
            )
            .with_for_update()
        )
        if (
            trace is None
            or segment is None
            or segment.binding_basis != "admitted_execution"
            or (
                segment.execution_lease_id != lease.id
                or segment.gateway_instance_ref != gateway_instance_ref
            )
        ):
            raise self._invalid()
        if active and segment.status != "running":
            raise self._invalid("trace_execution_terminal")
        return trace, segment
