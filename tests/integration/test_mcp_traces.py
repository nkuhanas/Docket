import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import McpTraceUpdate
from docket.models import (
    DiscordDailyThread,
    DiscordMcpTrace,
    OutboxEvent,
    ToolInvocation,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.mcp_traces import McpTraceService, trace_id_for_source
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _update(
    *,
    trace_id: uuid.UUID,
    ordinal: int | None = None,
    transport_state: str | None = None,
    turn_status: str = "running",
    source_channel_id: str | None = None,
    received_argument_hash: str | None = None,
) -> McpTraceUpdate:
    settings = get_settings()
    call = None
    if ordinal is not None and transport_state is not None:
        call = {
            "call_id": f"call-{ordinal}",
            "ordinal": ordinal,
            "tool_name": "docket_search_records",
            "transport_state": transport_state,
            "elapsed_ms": 0 if transport_state == "running" else 125,
            "disposition": "succeeded" if transport_state == "completed" else None,
            "transport_error_code": None,
            "argument_preview": '{"fields":["query"]}',
            "received_argument_hash": received_argument_hash,
        }
    return McpTraceUpdate.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "source_channel_id": source_channel_id or settings.chat_channel_id,
            "source_message_id": "777777777777777777",
            "actor_id": settings.operator_discord_user_id,
            "tool_contract_version": CONTRACT_VERSION,
            "tool_contract_hash": contract_hash("interactive"),
            "caller_profile": "interactive",
            "updated_at": datetime.now(UTC).isoformat(),
            "turn_status": turn_status,
            "call": call,
        }
    )


@pytest.mark.integration
def test_mcp_trace_accepts_a_bound_docket_daily_thread(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    thread_id = "888888888888888888"
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        thread_id,
        "777777777777777777",
    )
    with session_factory.begin() as session:
        session.add(
            DiscordDailyThread(
                guild_id=settings.discord_guild_id,
                channel_id=settings.queue_channel_id,
                local_date=date(2026, 7, 24),
                thread_name="2026-07-24",
                thread_id=thread_id,
                status="active",
            )
        )
        result = McpTraceService(session).update(
            trace_id,
            _update(
                trace_id=trace_id,
                ordinal=1,
                transport_state="running",
                source_channel_id=thread_id,
            ),
        )
        assert result["trace_version"] == 1

    unknown_channel = "999999999999999999"
    unknown_trace = trace_id_for_source(
        settings.discord_guild_id,
        unknown_channel,
        "777777777777777777",
    )
    with pytest.raises(DocketError) as rejected, session_factory.begin() as session:
        McpTraceService(session).update(
            unknown_trace,
            _update(
                trace_id=unknown_trace,
                ordinal=1,
                transport_state="running",
                source_channel_id=unknown_channel,
            ),
        )
    assert rejected.value.code == "invalid_mcp_trace_context"


@pytest.mark.integration
def test_mcp_trace_is_monotonic_redacted_and_projected(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        settings.chat_channel_id,
        "777777777777777777",
    )
    with session_factory.begin() as session:
        first = McpTraceService(session).update(
            trace_id,
            _update(trace_id=trace_id, ordinal=1, transport_state="running"),
        )
        assert first["trace_version"] == 1
    with session_factory.begin() as session:
        terminal = McpTraceService(session).update(
            trace_id,
            _update(trace_id=trace_id, ordinal=1, transport_state="completed"),
        )
        assert terminal["trace_version"] == 2
    with session_factory.begin() as session:
        completed = McpTraceService(session).update(
            trace_id,
            _update(trace_id=trace_id, turn_status="completed"),
        )
        assert completed["trace_version"] == 3
    with session_factory.begin() as session:
        replay = McpTraceService(session).update(
            trace_id,
            _update(trace_id=trace_id, turn_status="completed"),
        )
        assert replay["disposition"] == "replayed_request"

    with session_factory() as session:
        trace = session.get(DiscordMcpTrace, trace_id)
        assert trace is not None
        assert trace.status == "completed"
        assert trace.tool_contract_version == CONTRACT_VERSION
        assert trace.tool_contract_hash == contract_hash("interactive")
        assert trace.caller_profile == "interactive"
        assert trace.last_ordinal == 1
        assert trace.calls == [
            {
                "call_id": "call-1",
                "ordinal": 1,
                "tool_name": "docket_search_records",
                "transport_state": "completed",
                "domain_state": "unknown",
                "elapsed_ms": 125,
                "disposition": None,
                "transport_error_code": None,
                "domain_error_code": None,
                "argument_preview": '{"fields":["query"]}',
                "received_argument_hash": None,
                "tool_call_ref": None,
            }
        ]
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 3

    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    while runner.run_due_once():
        pass
    projected = backend.mcp_traces[str(trace_id)]["render"]
    assert projected["status"] == "Completed"
    assert projected["calls"] == [
        {
            "ordinal": 1,
            "tool_name": "docket_search_records",
            "transport_state": "completed",
            "domain_state": "unknown",
            "elapsed_ms": 125,
            "outcome": "unknown",
            "tool_call_ref": "unreconciled",
            "transport_error_code": "none",
            "argument_preview": '{"fields":["query"]}',
        }
    ]
    serialized = str(projected)
    assert "call-1" not in serialized
    assert settings.operator_discord_user_id not in serialized
    assert "777777777777777777" not in serialized

    with pytest.raises(DocketError) as regression, session_factory.begin() as session:
        McpTraceService(session).update(
            trace_id,
            _update(trace_id=trace_id, ordinal=1, transport_state="failed"),
        )
    assert regression.value.code == "mcp_trace_state_regression"


@pytest.mark.integration
def test_mcp_trace_projection_caps_rows_and_reports_overflow(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        settings.chat_channel_id,
        "777777777777777777",
    )
    for ordinal in range(1, 22):
        with session_factory.begin() as session:
            McpTraceService(session).update(
                trace_id,
                _update(
                    trace_id=trace_id,
                    ordinal=ordinal,
                    transport_state="running",
                ),
            )
    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_id,
            _update(trace_id=trace_id, turn_status="interrupted"),
        )

    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    while runner.run_due_once():
        pass
    projected = backend.mcp_traces[str(trace_id)]["render"]
    assert len(projected["calls"]) == 20
    assert projected["overflow_count"] == 1
    assert projected["status"] == "Interrupted"
    assert all(
        call["transport_state"] == "timed_out" for call in projected["calls"]
    )
    assert all(call["domain_state"] == "unknown" for call in projected["calls"])


@pytest.mark.integration
@pytest.mark.parametrize(
    ("invocation_status", "error_code", "expected_domain_state"),
    [
        ("rejected_validation", "attention_case_items_unresolved", "rejected"),
        ("failed", "internal_error", "failed"),
    ],
)
def test_mcp_trace_reconciles_authoritative_rejection_and_runtime_failure(
    session_factory: sessionmaker[Session],
    invocation_status: str,
    error_code: str,
    expected_domain_state: str,
) -> None:
    settings = get_settings()
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        settings.chat_channel_id,
        "777777777777777777",
    )
    argument_hash = "a" * 64
    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_id,
            _update(
                trace_id=trace_id,
                ordinal=1,
                transport_state="running",
                received_argument_hash=argument_hash,
            ),
        )
    with session_factory.begin() as session:
        invocation = ToolInvocation(
            tool_name="docket_search_records",
            tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"),
            caller_profile="interactive",
            status=invocation_status,
            received_argument_hash=argument_hash,
            result_refs=[],
            error_code=error_code,
        )
        session.add(invocation)
        session.flush()
        invocation_ref = invocation.ref_id
    with session_factory.begin() as session:
        result = McpTraceService(session).update(
            trace_id,
            _update(
                trace_id=trace_id,
                ordinal=1,
                transport_state="completed",
                received_argument_hash=argument_hash,
            ),
        )
        assert result["tool_call_ref"] == invocation_ref
    with session_factory() as session:
        trace = session.get(DiscordMcpTrace, trace_id)
        assert trace is not None
        assert trace.calls[0]["transport_state"] == "completed"
        assert trace.calls[0]["domain_state"] == expected_domain_state
        assert trace.calls[0]["tool_call_ref"] == invocation_ref
        assert trace.calls[0]["domain_error_code"] == error_code
        invocation = session.scalar(
            select(ToolInvocation).where(ToolInvocation.ref_id == invocation_ref)
        )
        assert invocation is not None
        assert invocation.trace_id == trace_id
        assert invocation.trace_call_id == "call-1"

    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    while runner.run_due_once():
        pass
    projected_call = backend.mcp_traces[str(trace_id)]["render"]["calls"][0]
    assert projected_call["transport_state"] == "completed"
    assert projected_call["domain_state"] == expected_domain_state
    assert projected_call["outcome"] == error_code
    assert projected_call["tool_call_ref"] == invocation_ref
