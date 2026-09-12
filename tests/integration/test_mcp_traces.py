import base64
import json
from datetime import UTC, date, datetime, timedelta

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
from docket.services.history import HistoryService
from docket.services.mcp_traces import McpTraceService
from docket.services.trace_views import TraceViewService
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
    turn_started_at: datetime | None = None,
    disposition: str | None = "succeeded",
    execution_boundary: str = "mcp_attempted",
) -> McpTraceUpdate:
    settings = get_settings()
    updated_at = datetime.now(UTC)
    call = None
    if ordinal is not None and transport_state is not None:
        call = {
            "call_id": f"call-{ordinal}",
            "ordinal": ordinal,
            "tool_name": tool_name,
            "execution_boundary": execution_boundary,
            "transport_state": transport_state,
            "elapsed_ms": 0 if transport_state == "running" else 125,
            "disposition": disposition if transport_state == "completed" else None,
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
            "turn_started_at": (turn_started_at or updated_at).isoformat(),
            "updated_at": updated_at.isoformat(),
            "turn_status": turn_status,
            "call": call,
        }
    )


@pytest.mark.integration
def test_mcp_trace_accepts_completed_domain_rejection(
    session_factory: sessionmaker[Session],
) -> None:
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_ref,
            _update(ordinal=1, transport_state="running"),
        )
    with session_factory.begin() as session:
        result = McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="completed",
                disposition="rejected_validation",
            ),
        )

    assert result["disposition"] == "updated"
    with session_factory() as session:
        trace = session.scalar(
            select(ConversationalToolTrace).where(ConversationalToolTrace.ref_id == trace_ref)
        )
        assert trace is not None
        assert trace.calls[0]["transport_state"] == "completed"
        assert trace.calls[0]["disposition"] == "rejected_validation"


@pytest.mark.integration
def test_mcp_trace_rejects_duplicate_source_with_domain_error(
    session_factory: sessionmaker[Session],
) -> None:
    source_message_id = "777777777777777777"
    with session_factory.begin() as session:
        McpTraceService(session).update(
            new_public_ref("trace"),
            _update(
                ordinal=1,
                transport_state="running",
                source_message_id=source_message_id,
            ),
        )

    with pytest.raises(DocketError) as duplicate, session_factory.begin() as session:
        McpTraceService(session).update(
            new_public_ref("trace"),
            _update(
                ordinal=1,
                transport_state="running",
                source_message_id=source_message_id,
            ),
        )

    assert duplicate.value.code == "mcp_trace_source_conflict"


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
                "execution_boundary": "mcp_attempted",
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
    assert projected["timing"]["total_elapsed_ms"] >= 0
    assert projected["timing"]["wrapper_elapsed_sum_ms"] == 125
    assert projected["timing"]["docket_execution_ms"] == 0
    assert projected["timing"]["unattributed_ms"] == projected["timing"]["total_elapsed_ms"]
    assert projected["calls"] == [
        {
            "ordinal": 1,
            "tool_name": "docket_search_history",
            "origin": "unreconciled",
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
        "origin": "authenticated_docket",
        "transport_state": "completed",
        "domain_state": "succeeded",
        "elapsed_ms": 125,
        "outcome": "needs_clarification",
        "tool_call_ref": invocation_ref,
        "transport_error_code": "none",
        "argument_preview": '{"fields":["query"]}',
    }


@pytest.mark.integration
def test_mcp_trace_timing_includes_gateway_to_first_tool_delay(
    session_factory: sessionmaker[Session],
) -> None:
    trace_ref = new_public_ref("trace")
    argument_hash = "c" * 64
    turn_started_at = datetime.now(UTC) - timedelta(minutes=4)
    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="running",
                received_argument_hash=argument_hash,
                turn_started_at=turn_started_at,
            ),
        )
        session.add(
            ToolInvocation(
                tool_name="docket_search_history",
                tool_contract_version=CONTRACT_VERSION,
                tool_contract_hash=contract_hash("interactive"),
                caller_profile="interactive",
                received_argument_hash=argument_hash,
                result_refs=[],
                transport_state="completed",
                domain_state="succeeded",
                result_disposition="succeeded",
                completed_at=datetime.now(UTC),
            )
        )

    with session_factory.begin() as session:
        McpTraceService(session).update(
            trace_ref,
            _update(
                ordinal=1,
                transport_state="completed",
                received_argument_hash=argument_hash,
                turn_started_at=turn_started_at,
            ),
        )
        McpTraceService(session).update(
            trace_ref,
            _update(turn_status="completed", turn_started_at=turn_started_at),
        )

    timing = _project_all(session_factory).mcp_traces[trace_ref]["render"]["timing"]
    assert timing["before_first_docket_call_ms"] >= 239_000
    assert timing["total_elapsed_ms"] >= timing["before_first_docket_call_ms"]
    assert timing["wrapper_elapsed_sum_ms"] == 125
    assert timing["unattributed_ms"] >= 239_000


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


@pytest.mark.integration
def test_local_rejection_never_claims_an_unrelated_matching_invocation(session_factory):
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        unrelated = ToolInvocation(
            tool_name="docket_commit_changeset", caller_profile="interactive",
            tool_contract_version=CONTRACT_VERSION,
            received_argument_hash="d" * 64, domain_state="succeeded",
            transport_state="completed", result_disposition="committed",
            completed_at=datetime.now(UTC),
        )
        session.add(unrelated)
        session.flush()
        unrelated_ref = unrelated.ref_id
        for state in ("running", "completed"):
            McpTraceService(session).update(trace_ref, _update(
                ordinal=1, transport_state=state, execution_boundary="local_rejection",
                received_argument_hash="d" * 64, tool_name="docket_commit_changeset",
                disposition="rejected_validation",
            ))
        McpTraceService(session).update(trace_ref, _update(turn_status="completed"))
    with session_factory() as session:
        page = HistoryService(session).get_entry(trace_ref, view="calls")
        assert page["counts"]["authenticated_invocations"] == 0
        assert page["counts"]["local_rejections"] == 1
        assert page["items"][0]["origin"] == "local_rejection"
        assert page["items"][0]["domain_state"] == "unknown"
        assert page["items"][0]["outcome"] == "rejected_validation"
        assert session.scalar(select(ToolInvocation).where(
            ToolInvocation.ref_id == unrelated_ref
        )).trace_ref is None
        assert page["timing"]["before_first_docket_call_ms"] is None


def _long_trace(session):
    settings = get_settings()
    start = datetime.now(UTC) - timedelta(seconds=30)
    calls = []
    for ordinal in range(1, 101):
        name = (
            "docket_stage_changes" if ordinal == 98 else
            "docket_review_changeset" if ordinal == 99 else
            "docket_commit_changeset" if ordinal == 100 else "docket_search_history"
        )
        calls.append({
            "call_id": f"call-{ordinal}", "ordinal": ordinal, "tool_name": name,
            "execution_boundary": "mcp_attempted", "transport_state": "completed",
            "elapsed_ms": 125, "disposition": None, "domain_state": "unknown",
            "argument_preview": json.dumps({"fields": ["界" * 180]}, ensure_ascii=False),
        })
    trace = ConversationalToolTrace(
        guild_id=settings.discord_guild_id, source_channel_id=settings.chat_channel_id,
        source_message_id="777777777777777777", actor_id=settings.operator_discord_user_id,
        tool_contract_version=CONTRACT_VERSION, tool_contract_hash=contract_hash("interactive"),
        caller_profile="interactive", started_at=start, calls=calls, last_ordinal=100,
    )
    session.add(trace)
    session.flush()
    session.add(ToolInvocation(
        tool_name="docket_commit_changeset", caller_profile="interactive",
        tool_contract_version=CONTRACT_VERSION, received_argument_hash="f" * 64,
        trace_ref=trace.ref_id, trace_call_id="call-100", trace_ordinal=100,
        started_at=start + timedelta(seconds=20), completed_at=start + timedelta(seconds=21),
        transport_state="completed", domain_state="succeeded", result_disposition="committed",
    ))
    session.flush()
    return trace


@pytest.mark.integration
def test_whole_trace_counts_recent_commit_and_bounded_complete_pages(session_factory):
    with session_factory.begin() as session:
        trace = _long_trace(session)
        trace_ref = trace.ref_id
        McpTraceService(session).update(trace_ref, _update(
            turn_status="completed", turn_started_at=trace.started_at,
        ))
    projected = _project_all(session_factory).mcp_traces[trace_ref]["render"]
    assert projected["counts"]["attempts"] == 100
    assert projected["counts"]["authenticated_invocations"] == 1
    assert projected["counts"]["unreconciled_attempts"] == 99
    assert projected["timing"]["wrapper_elapsed_sum_ms"] == 12_500
    assert projected["timing"]["docket_execution_ms"] == 1000
    assert projected["calls"][-1]["ordinal"] == 100
    assert projected["calls"][-1]["outcome"] == "committed"
    assert projected["overflow_count"] == 100 - len(projected["calls"])
    totals = {row["tool_name"]: row["attempts"] for row in projected["tool_counts"]}
    assert totals == {"docket_search_history": 97, "docket_stage_changes": 1,
                      "docket_review_changeset": 1, "docket_commit_changeset": 1}
    ordinals = []
    cursor = None
    while True:
        # Fresh sessions prove no process-local page state is required.
        with session_factory() as session:
            page = HistoryService(session).get_entry(
                trace_ref, view="calls", cursor=cursor, limit=100
            )
            assert len(json.dumps(page, ensure_ascii=False).encode()) < 16_384
            assert page["total_if_known"] == 100
            ordinals.extend(row["ordinal"] for row in page["items"])
            cursor = page.get("cursor")
        if cursor is None:
            break
    assert ordinals == list(range(1, 101))


@pytest.mark.integration
def test_trace_pagination_rejects_mixed_revision_and_foreign_or_invalid_cursors(session_factory):
    with session_factory.begin() as session:
        trace = _long_trace(session)
        history = HistoryService(session)
        first = history.get_entry(trace.ref_id, view="calls", limit=1)
        cursor = first["cursor"]
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for field, value in (("trace_ref", new_public_ref("trace")), ("position", True),
                             ("format", 2), ("position", 101)):
            bad = base64.urlsafe_b64encode(json.dumps({**decoded, field: value}).encode()).decode()
            with pytest.raises(DocketError) as invalid:
                history.get_entry(trace.ref_id, view="calls", cursor=bad)
            assert invalid.value.code == "invalid_trace_cursor"
        with pytest.raises(DocketError) as malformed:
            history.get_entry(trace.ref_id, view="calls", cursor="?")
        assert malformed.value.code == "invalid_trace_cursor"
        trace.version += 1
        session.flush()
        with pytest.raises(DocketError) as stale:
            history.get_entry(trace.ref_id, view="calls", cursor=cursor)
        assert stale.value.code == "trace_revision_changed"


@pytest.mark.integration
def test_trace_timing_unions_closed_intervals_without_invented_phases(session_factory):
    with session_factory.begin() as session:
        trace = _long_trace(session)
        trace.completed_at = trace.started_at + timedelta(seconds=30)
        # [20,21] from the fixture and [20.5,22] overlap. [5,7] is separate.
        for ordinal, left, right in ((1, 20.5, 22), (2, 5, 7), (3, 8, None)):
            session.add(ToolInvocation(
                tool_name="docket_search_history", caller_profile="interactive",
                tool_contract_version=CONTRACT_VERSION, received_argument_hash="f" * 64,
                trace_ref=trace.ref_id, trace_call_id=f"call-{ordinal}", trace_ordinal=ordinal,
                started_at=trace.started_at + timedelta(seconds=left),
                completed_at=trace.started_at + timedelta(seconds=right) if right else None,
            ))
        # An unrelated trace must not enter counts or the time union.
        session.add(ToolInvocation(
            tool_name="docket_commit_changeset", caller_profile="interactive",
            tool_contract_version=CONTRACT_VERSION, received_argument_hash="f" * 64,
            trace_ref=new_public_ref("trace"), started_at=trace.started_at,
            completed_at=trace.completed_at,
        ))
        session.flush()
        view = TraceViewService(session).snapshot(trace)
        assert view["counts"]["authenticated_invocations"] == 4
        assert view["counts"]["unfinished_invocations"] == 1
        assert view["timing"] == {
            "total_elapsed_ms": 30_000, "before_first_docket_call_ms": 5_000,
            "docket_execution_ms": 4_000, "wrapper_elapsed_sum_ms": 12_500,
            "unattributed_ms": 26_000, "queue_ms": None, "context_schema_ms": None,
            "model_ms": None, "local_validation_ms": None, "provider_wait_ms": None,
        }
