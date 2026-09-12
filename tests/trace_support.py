"""Synthetic admitted-execution fixtures, never a runtime compatibility path."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    ConversationalToolTrace,
    ExecutionLease,
    GatewayLifetime,
    TraceExecutionSegment,
)
from docket.services.continuity import ContinuityService
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.trace_executions import TraceExecutionService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def bind_execution(session, utterance, *, label=None, started_at=None, gateway=None):
    """A label identifies a fixture execution, not another message-wide trace.

    Tests that predate execution segments pass their old per-run labels here;
    all actual service calls use the server-returned parent and execution binding.
    """
    settings = get_settings()
    label = label or uuid.uuid4().hex
    key = f"test:trace:{utterance.ref_id}:{label}"
    lease = session.scalar(select(ExecutionLease).where(ExecutionLease.lease_key == key))
    if lease is None:
        if gateway is None:
            live = session.scalar(
                select(GatewayLifetime).where(
                    GatewayLifetime.instance_kind == "hermes_discord_gateway",
                    GatewayLifetime.status == "active",
                )
            )
            gateway = (
                live.ref_id
                if live
                else GatewayLifetimeService(session).register(
                    registration_key=uuid.uuid4(),
                    instance_kind="hermes_discord_gateway",
                )["ref"]
            )
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key=key,
            lease_kind="interactive_turn",
            subject_ref=utterance.ref_id,
            gateway_instance_ref=gateway,
        )
    start = started_at or datetime.now(UTC)
    parts = utterance.source_message_ref.split(":")
    if (
        label.startswith("trace_")
        and session.scalar(
            select(ConversationalToolTrace).where(
                ConversationalToolTrace.guild_id == parts[1],
                ConversationalToolTrace.source_channel_id == parts[2],
                ConversationalToolTrace.source_message_id == parts[3],
            )
        )
        is None
    ):
        # Stable synthetic first-ref fixtures only. Production always lets Docket
        # allocate it; the resumed execution still receives this same parent.
        session.add(
            ConversationalToolTrace(
                ref_id=label,
                guild_id=parts[1],
                source_channel_id=parts[2],
                source_message_id=parts[3],
                actor_id=settings.operator_discord_user_id,
                started_at=start,
                version=0,
            )
        )
        session.flush()
    result = TraceExecutionService(session).bind(
        utterance_ref=utterance.ref_id,
        execution_completion_token=lease.completion_token,
        gateway_instance_ref=lease.gateway_instance_ref,
        turn_started_at=start,
        tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"),
    )
    segment = session.scalar(
        select(TraceExecutionSegment).where(
            TraceExecutionSegment.execution_lease_id == lease.id,
        )
    )
    return {
        "trace_ref": result["trace_ref"],
        "execution_index": result["execution_index"],
        "execution_completion_token": lease.completion_token,
        "gateway_instance_ref": lease.gateway_instance_ref,
        "turn_started_at": datetime.fromisoformat(result["turn_started_at"]),
        "trace_execution_id": segment.id,
    }


def callback_binding(binding):
    return {
        key: binding[key]
        for key in (
            "execution_index",
            "execution_completion_token",
            "gateway_instance_ref",
            "turn_started_at",
        )
    }


def segment_for(session, ref, index=1):
    return session.scalar(
        select(TraceExecutionSegment).where(
            TraceExecutionSegment.trace_ref == ref,
            TraceExecutionSegment.execution_index == index,
        )
    )
