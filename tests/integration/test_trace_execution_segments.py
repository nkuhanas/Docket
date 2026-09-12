import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from trace_support import bind_execution, callback_binding

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import McpTraceCheckpoint
from docket.models import (
    ConversationalToolTrace,
    GatewayLifetime,
    OperatorUtterance,
    TraceExecutionSegment,
)
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.mcp_traces import McpTraceService
from docket.services.trace_views import TraceViewService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def source(session):
    settings = get_settings()
    message = "777777777777778880"
    row = OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}",
        transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message}"
        ),
        conversation_ref=f"discord_conversation:{settings.chat_channel_id}",
        said_at=datetime.now(UTC),
        verbatim_text="Synthetic recovery request.",
        content_hash="a" * 64,
        request_key=f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message}:0",
    )
    session.add(row)
    session.flush()
    return row


def checkpoint(binding, utterance_ref, *, terminal="running", ordinal=1):
    settings = get_settings()
    return McpTraceCheckpoint(
        request_id=uuid.uuid4(),
        guild_id=settings.discord_guild_id,
        source_channel_id=settings.chat_channel_id,
        source_message_id="777777777777778880",
        actor_id=settings.operator_discord_user_id,
        utterance_ref=utterance_ref,
        tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"),
        caller_profile="interactive",
        updated_at=datetime.now(UTC),
        turn_status=terminal,
        **callback_binding(binding),
        calls=[
            {
                "call_id": "same-upstream-id",
                "ordinal": ordinal,
                "tool_name": "docket_stage_changes",
                "execution_boundary": "local_rejection",
                "transport_state": "completed",
                "disposition": "rejected_validation",
                "elapsed_ms": 2,
            }
        ],
    )


def test_cold_restart_retains_parent_and_independent_execution_ordinals(session_factory):
    with session_factory.begin() as session:
        utterance = source(session)
        original = bind_execution(session, utterance)
        utterance_ref = utterance.ref_id
        McpTraceService(session).checkpoint(
            original["trace_ref"], checkpoint(original, utterance_ref)
        )
    with session_factory.begin() as session:
        gateway = session.scalar(
            select(GatewayLifetime).where(
                GatewayLifetime.ref_id == original["gateway_instance_ref"],
            )
        )
        gateway.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
    with session_factory.begin() as session:
        replacement_gateway = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )["ref"]
        utterance = session.scalar(select(OperatorUtterance))
        resumed = bind_execution(session, utterance, gateway=replacement_gateway)
        assert resumed["trace_ref"] == original["trace_ref"]
        assert resumed["execution_index"] == 2
        McpTraceService(session).checkpoint(
            resumed["trace_ref"],
            checkpoint(
                resumed,
                utterance_ref,
                terminal="completed",
            ),
        )
    with session_factory.begin() as session:
        with pytest.raises(DocketError) as error:
            McpTraceService(session).checkpoint(
                original["trace_ref"], checkpoint(original, utterance_ref)
            )
        assert error.value.code == "gateway_lifetime_fenced"
    with session_factory() as session:
        segments = list(
            session.scalars(
                select(TraceExecutionSegment).order_by(
                    TraceExecutionSegment.execution_index,
                )
            )
        )
        assert [row.status for row in segments] == ["interrupted", "completed"]
        assert [row.calls[0]["ordinal"] for row in segments] == [1, 1]
        assert segments[0].gateway_instance_ref == original["gateway_instance_ref"]
        parent = session.scalar(select(ConversationalToolTrace))
        assert parent.status == "completed"
        view = TraceViewService(session).snapshot(parent)
        assert view["counts"]["executions"] == 2
        assert view["counts"]["local_rejections"] == 2
        assert [row["execution_index"] for row in view["rows"]] == [1, 2]
        assert session.scalar(select(func.count(ConversationalToolTrace.id))) == 1
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 1


def test_execution_binding_replay_and_cross_execution_callback_rejection(session_factory):
    with session_factory.begin() as session:
        utterance = source(session)
        first = bind_execution(session, utterance, label="first")
        replay = bind_execution(session, utterance, label="first")
        second = bind_execution(session, utterance, label="second")
        assert replay == first
        assert second["execution_index"] == 2
        bad = {**first, "execution_completion_token": second["execution_completion_token"]}
        with pytest.raises(DocketError) as error:
            McpTraceService(session).checkpoint(
                first["trace_ref"], checkpoint(bad, utterance.ref_id)
            )
        assert error.value.code == "trace_execution_binding_mismatch"
        assert session.scalar(select(func.count(TraceExecutionSegment.id))) == 2


def test_execution_binding_cannot_be_rewritten(session_factory):
    with session_factory.begin() as session:
        bind_execution(session, source(session))
    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        segment = session.scalar(select(TraceExecutionSegment))
        segment.gateway_instance_ref = "gwy_" + "0" * 26


def test_execution_history_is_bounded_complete_and_revision_bound(session_factory):
    with session_factory.begin() as session:
        utterance = source(session)
        for index in range(31):
            bind_execution(session, utterance, label=f"history-{index}")
        parent = session.scalar(select(ConversationalToolTrace))
        view = TraceViewService(session)
        first = view.read(parent, cursor=None, limit=25, collection="executions")
        assert first["total_if_known"] == 31 and first["omitted_execution_count"] == 28
        pages, cursor = [first], first.get("cursor")
        while cursor:
            page = view.read(parent, cursor=cursor, limit=25, collection="executions")
            pages.append(page)
            cursor = page.get("cursor")
        assert [row["execution_index"] for page in pages for row in page["items"]] == list(
            range(1, 32)
        )
        assert all(len(json.dumps(page).encode()) < 16_384 for page in pages)
        assert "execution_completion_token" not in json.dumps(pages)
        assert "trace_execution_id" not in json.dumps(pages)
        assert all(row["retained_wrapper_calls"] == 0 for page in pages for row in page["items"])
        with pytest.raises(DocketError) as foreign:
            view.read(parent, cursor=first["cursor"], limit=25, collection="calls")
        assert foreign.value.code == "invalid_trace_cursor"
        bind_execution(session, utterance, label="history-new")
        with pytest.raises(DocketError) as stale:
            view.read(parent, cursor=first["cursor"], limit=25, collection="executions")
        assert stale.value.code == "trace_revision_changed"
