from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import McpTraceUpdate
from docket.models import (
    ConversationalToolTrace,
    DiscordDailyThread,
    OutboxEvent,
    ToolInvocation,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.mcp_traces import McpTraceService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _update(
    *,
    ordinal: int | None = None,
    transport_state: str | None = None,
    turn_status: str = "running",
    source_channel_id: str | None = None,
    received_argument_hash: str | None = None,
    tool_name: str = "docket_search_history",
    source_message_id: str = "777777777777777777",
) -> McpTraceUpdate:
    settings = get_settings()
    call = None
    if ordinal is not None and transport_state is not None:
        call = {
            "call_id": f"call-{ordinal}",
            "ordinal": ordinal,
            "tool_name": tool_name,
            "transport_state": transport_state,
            "elapsed_ms": 0 if transport_state == "running" else 125,
            "disposition": "succeeded" if transport_state == "completed" else None,
            "transport_error_code": None,
            "argument_preview": '{"fields":["query"]}',
            "received_argument_hash": received_argument_hash,
        }
    return McpTraceUpdate.model_validate(
        {
            "request_id": "00000000-0000-0000-0000-000000000001",
            "guild_id": settings.discord_guild_id,
            "source_channel_id": source_channel_id or settings.chat_channel_id,
            "source_message_id": source_message_id,
            "actor_id": settings.operator_discord_user_id,
            "tool_contract_version": CONTRACT_VERSION,
            "tool_contract_hash": contract_hash("interactive"),
            "caller_profile": "interactive",
            "updated_at": datetime.now(UTC).isoformat(),
            "turn_status": turn_status,
            "call": call,
        }
    )


def _project_all(session_factory: sessionmaker[Session]) -> FakeDiscordBackend:
    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        get_settings(),
    )
    while runner.run_due_once():
        pass
    return backend


@pytest.mark.integration
def test_mcp_trace_accepts_only_a_trusted_docket_conversation(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    thread_id = "888888888888888888"
    trace_ref = new_public_ref("trace")
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
            trace_ref,
            _update(ordinal=1, transport_state="running", source_channel_id=thread_id),
        )
        assert result["trace_ref"] == trace_ref
        assert result["trace_version"] == 1

    with pytest.raises(DocketError) as rejected, session_factory.begin() as session:
        McpTraceService(session).update(
            new_public_ref("trace"),
            _update(
                ordinal=1,
                transport_state="running",
                source_channel_id="999999999999999999",
            ),
        )
    assert rejected.value.code == "invalid_mcp_trace_context"


@pytest.mark.integration
def test_mcp_trace_is_monotonic_redacted_and_projected(
    session_factory: sessionmaker[Session],
) -> None:
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        assert (
            McpTraceService(session).update(
                trace_ref, _update(ordinal=1, transport_state="running")
            )["trace_version"]
            == 1
        )
    with session_factory.begin() as session:
        assert (
            McpTraceService(session).update(
                trace_ref, _update(ordinal=1, transport_state="completed")
            )["trace_version"]
            == 2
        )
    with session_factory.begin() as session:
        assert (
            McpTraceService(session).update(trace_ref, _update(turn_status="completed"))[
                "trace_version"
            ]
            == 3
        )
    with session_factory.begin() as session:
        replay = McpTraceService(session).update(trace_ref, _update(turn_status="completed"))
        assert replay["disposition"] == "replayed_request"

    with session_factory() as session:
        trace = session.scalar(
            select(ConversationalToolTrace).where(ConversationalToolTrace.ref_id == trace_ref)
        )
        assert trace is not None
        assert trace.status == "completed"
        assert trace.calls == [
            {
                "call_id": "call-1",
                "ordinal": 1,
                "tool_name": "docket_search_history",
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

    projected = _project_all(session_factory).mcp_traces[trace_ref]["render"]
    assert projected["status"] == "Completed"
    assert projected["calls"] == [
        {
            "ordinal": 1,
            "tool_name": "docket_search_history",
            "transport_state": "completed",
            "domain_state": "unknown",
            "elapsed_ms": 125,
            "outcome": "unknown",
            "tool_call_ref": "unreconciled",
            "transport_error_code": "none",
            "argument_preview": '{"fields":["query"]}',
        }
    ]
    assert "call-1" not in str(projected)
    assert get_settings().operator_discord_user_id not in str(projected)

    with pytest.raises(DocketError) as regression, session_factory.begin() as session:
        McpTraceService(session).update(trace_ref, _update(ordinal=1, transport_state="failed"))
    assert regression.value.code == "mcp_trace_state_regression"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("domain_state", "result_disposition", "error_code"),
    [
        ("rejected", "rejected_validation", "attention_case_items_unresolved"),
        ("failed", "failed", "internal_error"),
    ],
)
def test_mcp_trace_reconciles_qualified_tool_lifecycle(
    session_factory: sessionmaker[Session],
    domain_state: str,
    result_disposition: str,
    error_code: str,
) -> None:
    trace_ref = new_public_ref("trace")
    argument_hash = "a" * 64
    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="running",
                received_argument_hash=argument_hash,
            ),
        )
    with session_factory.begin() as session:
        invocation = ToolInvocation(
            tool_name="docket_search_history",
            tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"),
            caller_profile="interactive",
            received_argument_hash=argument_hash,
            result_refs=[],
            transport_state="completed",
            domain_state=domain_state,
            result_disposition=result_disposition,
            error_code=error_code,
            completed_at=datetime.now(UTC),
        )
        session.add(invocation)
        session.flush()
        invocation_ref = invocation.ref_id
    with session_factory.begin() as session:
        result = McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="completed",
                received_argument_hash=argument_hash,
            ),
        )
        assert result["tool_call_ref"] == invocation_ref

    with session_factory() as session:
        trace = session.scalar(
            select(ConversationalToolTrace).where(ConversationalToolTrace.ref_id == trace_ref)
        )
        assert trace is not None
        call = trace.calls[0]
        assert call["transport_state"] == "completed"
        assert call["domain_state"] == domain_state
        assert call["disposition"] == result_disposition
        assert call["tool_call_ref"] == invocation_ref
        assert call["domain_error_code"] == error_code
        stored = session.scalar(
            select(ToolInvocation).where(ToolInvocation.ref_id == invocation_ref)
        )
        assert stored is not None
        assert stored.trace_ref == trace_ref
        assert stored.trace_call_id == "call-1"
        assert not hasattr(stored, "status")

    projected = _project_all(session_factory).mcp_traces[trace_ref]["render"]["calls"][0]
    assert projected["transport_state"] == "completed"
    assert projected["domain_state"] == domain_state
    assert projected["outcome"] == result_disposition
    assert projected["tool_call_ref"] == invocation_ref


@pytest.mark.integration
def test_mcp_trace_projects_semantic_disposition_as_primary_outcome(
    session_factory: sessionmaker[Session],
) -> None:
    trace_ref = new_public_ref("trace")
    argument_hash = "b" * 64
    source_message_id = "777777777777777778"
    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="running",
                received_argument_hash=argument_hash,
                tool_name="docket_commit_changeset",
                source_message_id=source_message_id,
            ),
        )
        invocation = ToolInvocation(
            tool_name="docket_commit_changeset",
            tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"),
            caller_profile="interactive",
            received_argument_hash=argument_hash,
            result_refs=[],
            transport_state="completed",
            domain_state="succeeded",
            result_disposition="needs_clarification",
            completed_at=datetime.now(UTC),
        )
        session.add(invocation)
        session.flush()
        invocation_ref = invocation.ref_id

    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="completed",
                received_argument_hash=argument_hash,
                tool_name="docket_commit_changeset",
                source_message_id=source_message_id,
            ),
        )

    projected = _project_all(session_factory).mcp_traces[trace_ref]["render"]["calls"][0]
    assert projected == {
        "ordinal": 1,
        "tool_name": "docket_commit_changeset",
        "transport_state": "completed",
        "domain_state": "succeeded",
        "elapsed_ms": 125,
        "outcome": "needs_clarification",
        "tool_call_ref": invocation_ref,
        "transport_error_code": "none",
        "argument_preview": '{"fields":["query"]}',
    }


@pytest.mark.integration
def test_interrupted_trace_uses_unknown_domain_outcome(
    session_factory: sessionmaker[Session],
) -> None:
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        McpTraceService(session).update(trace_ref, _update(ordinal=1, transport_state="running"))
    with session_factory.begin() as session:
        McpTraceService(session).update(trace_ref, _update(turn_status="interrupted"))

    projected = _project_all(session_factory).mcp_traces[trace_ref]["render"]
    assert projected["status"] == "Interrupted"
    assert projected["calls"][0]["transport_state"] == "timed_out"
    assert projected["calls"][0]["domain_state"] == "unknown"
    assert projected["calls"][0]["outcome"] == "unknown"
