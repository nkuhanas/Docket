import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from trace_support import bind_execution, callback_binding, segment_for

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import McpTraceCallUpdate, McpTraceCheckpoint, McpTraceUpdate
from docket.models import ConversationalToolTrace, OperatorUtterance, OutboxEvent, ToolInvocation
from docket.services.mcp_traces import McpTraceService
from docket.services.trace_views import TraceViewService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _context(factory, ref=None):
    settings = get_settings()
    now = datetime.now(UTC)
    message = "777777777777777779"
    with factory.begin() as session:
        utterance = OperatorUtterance(
            actor_ref=f"discord_user:{settings.operator_discord_user_id}", transport="discord",
            source_message_ref=(
                f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message}"
            ), conversation_ref=f"discord_conversation:{settings.chat_channel_id}",
            said_at=now, verbatim_text="Read my bounded test context.", content_hash="b" * 64,
            request_key=f"checkpoint:{message}",
        )
        session.add(utterance)
        session.flush()
        utterance_ref = utterance.ref_id
        binding = bind_execution(session, utterance, label=ref, started_at=now)
    return dict(
        request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
        source_channel_id=settings.chat_channel_id, source_message_id=message,
        actor_id=settings.operator_discord_user_id, utterance_ref=utterance_ref,
        caller_profile="interactive", tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"), updated_at=now,
        **callback_binding(binding),
    )


def _call(ordinal, **changes):
    return dict(
        call_id=f"checkpoint-call-{ordinal}", ordinal=ordinal,
        tool_name="docket_stage_changes", execution_boundary="local_rejection",
        transport_state="completed", elapsed_ms=13, disposition="rejected_validation",
        received_argument_hash="a" * 64, **changes,
    )


def _row(session, ref):
    return session.scalar(select(ConversationalToolTrace).where(
        ConversationalToolTrace.ref_id == ref
    ))


def test_checkpoint_recovers_lost_queue_and_replays_after_reconciliation(session_factory):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    local = _call(1)
    remote = {**_call(2), "execution_boundary": "mcp_attempted", "disposition": "succeeded"}
    calls = [local, remote]
    checkpoint = McpTraceCheckpoint(**context, calls=calls, turn_status="completed")
    with session_factory.begin() as session:
        result = McpTraceService(session).checkpoint(ref, checkpoint)
        assert result["trace_status"] == "completed"
        assert result["trace_version"] == 2
    with session_factory.begin() as session:
        trace = _row(session, ref)
        assert segment_for(session, ref).last_ordinal == 2
        assert segment_for(session, ref).calls[1]["disposition"] is None
        assert segment_for(session, ref).calls[1]["reported_disposition"] == "succeeded"
        replay = McpTraceService(session).checkpoint(ref, checkpoint)
        assert replay["disposition"] == "replayed_request"
        # Its delayed asynchronous start cannot erase completion or add an outbox row.
        start = {**remote, "transport_state": "running", "elapsed_ms": 0, "disposition": None}
        update = McpTraceUpdate(**{k: v for k, v in context.items() if k != "utterance_ref"},
                                call=start)
        assert McpTraceService(session).update(ref, update)["disposition"] == "replayed_request"
        assert session.scalar(select(func.count(OutboxEvent.id))) == 2
        assert session.scalar(select(func.count(ToolInvocation.id))) == 0
        view = TraceViewService(session).snapshot(trace)
        assert view["counts"]["local_rejections"] == 1
        assert view["counts"]["unreconciled_attempts"] == 1
        assert view["rows"][1]["domain_state"] == "unknown"


def test_checkpoint_cannot_override_authenticated_domain_outcome(session_factory):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    remote = {**_call(1), "execution_boundary": "mcp_attempted", "disposition": "succeeded"}
    with session_factory.begin() as session:
        invocation = ToolInvocation(
            tool_name=remote["tool_name"], caller_profile="interactive",
            tool_contract_version=CONTRACT_VERSION, received_argument_hash="a" * 64,
            actor_ref=f"discord_user:{context['actor_id']}",
            utterance_refs=[context["utterance_ref"]],
            trace_ref=ref, trace_execution_id=segment_for(session, ref).id,
            gateway_instance_ref=context["gateway_instance_ref"],
            trace_call_id=remote["call_id"], trace_ordinal=1,
            transport_state="completed", domain_state="rejected",
            result_disposition="rejected_validation", completed_at=datetime.now(UTC),
        )
        session.add(invocation)
    checkpoint = McpTraceCheckpoint(**context, calls=[remote])
    with session_factory.begin() as session:
        service = McpTraceService(session)
        service.checkpoint(ref, checkpoint)
        assert segment_for(session, ref).calls[0]["disposition"] == "rejected_validation"
        assert service.checkpoint(ref, checkpoint)["disposition"] == "replayed_request"
        assert segment_for(session, ref).calls[0]["reported_disposition"] == "succeeded"
    with session_factory.begin() as session:
        with pytest.raises(DocketError) as conflict:
            McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(
                **context, calls=[_call(1)],
            ))
        assert conflict.value.code == "mcp_trace_call_conflict"
        assert segment_for(session, ref).calls[0]["disposition"] == "rejected_validation"


@pytest.mark.parametrize("field,value", [
    ("call_id", "other-call"), ("tool_name", "docket_review_changeset"),
    ("received_argument_hash", "b" * 64), ("argument_preview", '{"fields":["other"]}'),
    ("disposition", "rejected_authority"), ("elapsed_ms", 99),
])
def test_checkpoint_conflicts_roll_back_complete_page(session_factory, field, value):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    original = _call(1)
    with session_factory.begin() as session:
        McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(**context, calls=[original]))
    with session_factory.begin() as session:
        service = McpTraceService(session)
        with pytest.raises(DocketError):
            service.checkpoint(ref, McpTraceCheckpoint(**context, calls=[
                {**original, field: value}, _call(2),
            ]))
        assert segment_for(session, ref).last_ordinal == 1
        assert _row(session, ref).version == 2
        assert session.scalar(select(func.count(OutboxEvent.id))) == 2


def test_checkpoint_ordinal_gap_does_not_partially_persist_new_trace(session_factory):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    with session_factory.begin() as session:
        with pytest.raises(DocketError) as conflict:
            McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(
                **context, calls=[_call(1), _call(3)],
            ))
        assert conflict.value.code == "nonmonotonic_mcp_trace"
        assert segment_for(session, ref).last_ordinal == 0
        assert segment_for(session, ref).calls == []
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1


def test_checkpoint_pages_resume_and_terminal_trace_does_not_admit_new_calls(session_factory):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    for offset in (0, 25, 50):
        page = [_call(ordinal) for ordinal in range(offset + 1, min(offset + 26, 54))]
        with session_factory.begin() as session:
            McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(
                **context, calls=page, turn_status="completed" if offset == 50 else "running",
            ))
    with session_factory.begin() as session:
        trace = _row(session, ref)
        assert segment_for(session, ref).last_ordinal == 53 and trace.status == "completed"
        assert trace.version == 4
        with pytest.raises(DocketError) as conflict:
            McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(
                **context, calls=[_call(54)],
            ))
        assert conflict.value.code == "mcp_trace_terminal"
        assert segment_for(session, ref).last_ordinal == 53


@pytest.mark.parametrize("disposition", ["failed", "rejected_authority", "rejected_conflict"])
def test_local_rejection_reason_survives_reconciliation_without_domain_authority(
    session_factory, disposition,
):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    checkpoint = McpTraceCheckpoint(**context, calls=[{**_call(1), "disposition": disposition}])
    with session_factory.begin() as session:
        service = McpTraceService(session)
        service.checkpoint(ref, checkpoint)
        assert service.checkpoint(ref, checkpoint)["disposition"] == "replayed_request"
        row = TraceViewService(session).snapshot(_row(session, ref))["rows"][0]
        assert row["origin"] == "local_rejection"
        assert row["outcome"] == disposition and row["domain_state"] == "unknown"


@pytest.mark.parametrize("problem", ["utterance", "source", "actor", "contract", "local_success"])
def test_checkpoint_rejects_wrong_evidence_or_success_claim(session_factory, problem):
    context = _context(session_factory)
    call = _call(1)
    if problem == "utterance":
        context["utterance_ref"] = new_public_ref("utt")
    elif problem == "source":
        context["source_message_id"] = "777777777777777778"
    elif problem == "actor":
        context["actor_id"] = "777777777777777778"
    elif problem == "contract":
        context["tool_contract_hash"] = "f" * 64
    else:
        call["disposition"] = "committed"
    with session_factory.begin() as session:
        with pytest.raises(DocketError):
            McpTraceService(session).checkpoint(new_public_ref("trace"), McpTraceCheckpoint(
                **context, calls=[call],
            ))
        assert session.scalar(select(func.count(ConversationalToolTrace.id))) == 1


def test_checkpoint_schema_caps_bytes_and_entries_and_rejects_duplicate_order(session_factory):
    context = _context(session_factory)
    for calls in ([_call(1), _call(1)], [_call(2), _call(1)], [_call(i) for i in range(1, 27)]):
        with pytest.raises(ValidationError):
            McpTraceCheckpoint(**context, calls=calls)
    with pytest.raises(ValidationError, match="byte bound"):
        McpTraceCheckpoint(**context, calls=[
            {**_call(i), "argument_preview": '"' + "界" * 700 + '"'} for i in range(1, 10)
        ])
    with pytest.raises(ValidationError):
        McpTraceCheckpoint(**context, calls=[])
    assert McpTraceCallUpdate(**_call(1)).ordinal == 1


def test_reconciliation_preserves_uncopied_historical_local_observation(session_factory):
    ref = new_public_ref("trace")
    context = _context(session_factory, ref)
    with session_factory.begin() as session:
        service = McpTraceService(session)
        service.checkpoint(ref, McpTraceCheckpoint(**context, calls=[_call(1)]))
        # This older evidence was never captured as an independent report.
        historical_call = {
            k: v
            for k, v in segment_for(session, ref).calls[0].items()
            if k != "reported_disposition"
        }
        segment_for(session, ref).calls = [historical_call]
        session.flush()
        assert service._reconcile_calls(segment_for(session, ref)) is False
        assert segment_for(session, ref).calls == [historical_call]
        assert segment_for(session, ref).calls[0]["disposition"] == "rejected_validation"
        assert "reported_disposition" not in segment_for(session, ref).calls[0]
