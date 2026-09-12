import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import McpTraceCheckpoint, McpTraceUpdate, TraceTimingInput
from docket.models import (
    ConversationalToolTrace,
    ExecutionLease,
    OperatorUtterance,
    TraceTimingObservation,
)
from docket.services.mcp_traces import McpTraceService
from docket.services.trace_views import TraceViewService, _partition_intervals
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _context(factory):
    settings = get_settings()
    start = datetime.now(UTC) - timedelta(seconds=20)
    with factory.begin() as session:
        utterance = OperatorUtterance(
            actor_ref=f"discord_user:{settings.operator_discord_user_id}", transport="discord",
            source_message_ref=(
                f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                "777777777777777765"
            ), conversation_ref=f"discord_conversation:{settings.chat_channel_id}",
            said_at=start, recorded_at=start - timedelta(seconds=10),
            verbatim_text="Timing fixture only.", content_hash="b" * 64,
            request_key="trace-timing-fixture",
        )
        session.add(utterance)
        session.flush()
        ref = utterance.ref_id
    return dict(
        request_id=uuid.uuid4(), guild_id=settings.discord_guild_id,
        source_channel_id=settings.chat_channel_id, source_message_id="777777777777777765",
        actor_id=settings.operator_discord_user_id, utterance_ref=ref,
        caller_profile="interactive", tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"), turn_started_at=start,
        updated_at=start + timedelta(seconds=10),
    )


def _span(context, phase="model_request", left=1, right=3):
    return TraceTimingInput(
        span_id=uuid.uuid4(), phase=phase,
        started_at=context["turn_started_at"] + timedelta(seconds=left),
        ended_at=context["turn_started_at"] + timedelta(seconds=right),
    )


def test_partition_attributes_nested_and_parallel_intervals_once():
    assert _partition_intervals({
        "docket_execution_ms": [(30, 45), (40, 50)],
        "context_schema_ms": [(0, 70)],
        "model_ms": [(10, 60), (25, 65)],
        "local_validation_ms": [(20, 25)],
    }) == {"docket_execution_ms": 20, "model_ms": 30,
           "context_schema_ms": 15, "local_validation_ms": 5, "queue_ms": 0}


def _claim(session, context, gateway, *, seconds=-3, kind="interactive_turn"):
    claimed = context["turn_started_at"] + timedelta(seconds=seconds)
    session.add(ExecutionLease(
        lease_key=f"fixture:{uuid.uuid4()}", lease_kind=kind,
        subject_ref=context["utterance_ref"], gateway_instance_ref=gateway,
        claimed_at=claimed, heartbeat_at=claimed,
        lease_expires_at=claimed + timedelta(minutes=10), status="completed",
        completed_at=claimed + timedelta(seconds=1),
    ))


def test_ingress_queue_uses_original_receipt_and_first_claim_after_restart(session_factory):
    context = _context(session_factory)
    trace_ref, gateway = new_public_ref("trace"), new_public_ref("gwy")
    with session_factory.begin() as session:
        McpTraceService(session).checkpoint(trace_ref, McpTraceCheckpoint(
            **context, timings=[_span(context)], turn_status="completed",
        ))
        trace = session.scalar(select(ConversationalToolTrace))
        trace.gateway_instance_ref = gateway
        _claim(session, context, gateway)
        # An unrelated lease and a later recovery must not move the boundary.
        _claim(session, context, gateway, seconds=-5, kind="outbox_delivery")
        _claim(session, context, gateway, seconds=12)
    with session_factory.begin() as session:
        trace = session.scalar(select(ConversationalToolTrace))
        view = TraceViewService(session).snapshot(trace)
        timing = view["timing"]
        assert timing["queue_ms"] == 7000
        trace_elapsed = int((trace.completed_at.replace(tzinfo=UTC)
                             - context["turn_started_at"]).total_seconds() * 1000)
        assert timing["total_elapsed_ms"] == pytest.approx(trace_elapsed + 10000, abs=1)
        assert timing["model_ms"] == 2000
        assert timing["unattributed_ms"] == timing["total_elapsed_ms"] - 9000
        assert timing["provider_wait_ms"] is None
        assert view["timing_scope"].startswith("durable_receipt_to_trace_end")
        assert view["rows"] == []
        assert session.scalar(select(func.count(TraceTimingObservation.id))) == 1
        assert session.scalar(select(func.count(ExecutionLease.id))) == 3


@pytest.mark.parametrize("failure", [
    "other_gateway", "missing_gateway", "other_actor", "other_source",
    "claim_before_receipt", "claim_after_trace_start", "trace_end_before_claim",
])
def test_queue_measurement_does_not_guess_missing_or_inconsistent_evidence(
    session_factory, failure,
):
    context = _context(session_factory)
    gateway = new_public_ref("gwy")
    with session_factory.begin() as session:
        McpTraceService(session).checkpoint(new_public_ref("trace"), McpTraceCheckpoint(
            **context, timings=[_span(context)], turn_status="completed",
        ))
        trace = session.scalar(select(ConversationalToolTrace))
        trace.gateway_instance_ref = None if failure == "missing_gateway" else gateway
        if failure == "other_actor":
            trace.actor_id = "777777777777777799"
        if failure == "other_source":
            trace.source_message_id = "777777777777777799"
        if failure == "trace_end_before_claim":
            trace.completed_at = context["turn_started_at"] - timedelta(seconds=4)
        _claim(session, context, new_public_ref("gwy") if failure == "other_gateway" else gateway,
               seconds=-11 if failure == "claim_before_receipt" else (
                   1 if failure == "claim_after_trace_start" else -3
               ))
        if failure == "other_gateway":
            # Selecting a later matching claim would invent an initial queue.
            _claim(session, context, gateway, seconds=-1)
    with session_factory.begin() as session:
        trace = session.scalar(select(ConversationalToolTrace))
        view = TraceViewService(session).snapshot(trace)
        assert view["timing"]["queue_ms"] is None
        assert view["timing"]["total_elapsed_ms"] == max(0, int((
            trace.completed_at.replace(tzinfo=UTC) - context["turn_started_at"]
        ).total_seconds() * 1000))
        assert view["timing_scope"] == "trace_window_closed_intervals_exclusive_attribution"


def test_timing_checkpoint_recovery_is_durable_replayable_and_payload_free(session_factory):
    context = _context(session_factory)
    trace_ref = new_public_ref("trace")
    spans = [_span(context), _span(context, "context_schema", 0, 5),
             _span(context, "local_validation", 6, 7)]
    with session_factory.begin() as session:
        result = McpTraceService(session).checkpoint(trace_ref, McpTraceCheckpoint(
            **context, timings=spans, turn_status="completed",
        ))
        assert result["disposition"] == "updated"
    with session_factory.begin() as session:
        result = McpTraceService(session).checkpoint(trace_ref, McpTraceCheckpoint(
            **context, timings=spans, turn_status="completed",
        ))
        assert result["disposition"] == "replayed_request"
        # A delayed async observation cannot change the recorded interval or final state.
        ordinary = {key: value for key, value in context.items() if key != "utterance_ref"}
        assert McpTraceService(session).update(trace_ref, McpTraceUpdate(
            **ordinary, timing=spans[0],
        ))["disposition"] == "replayed_request"
        assert session.scalar(select(func.count(TraceTimingObservation.id))) == 3
        trace = session.scalar(select(ConversationalToolTrace))
        view = TraceViewService(session).snapshot(trace)
        timing = view["timing"]
        assert timing["model_ms"] == 2000 and timing["context_schema_ms"] == 3000
        assert timing["local_validation_ms"] == 1000 and timing["docket_execution_ms"] == 0
        assert timing["unattributed_ms"] == timing["total_elapsed_ms"] - 6000
        assert timing["queue_ms"] is None and timing["provider_wait_ms"] is None
        assert view["rows"] == [] and view["counts"]["authenticated_invocations"] == 0
        assert set(TraceTimingObservation.__table__.columns.keys()) == {
            "id", "trace_ref", "phase", "started_at", "ended_at",
        }


def test_failed_timing_page_rolls_back_all_new_observations(session_factory):
    context = _context(session_factory)
    ref = new_public_ref("trace")
    original = _span(context)
    with session_factory.begin() as session:
        McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(**context, timings=[original]))
    with session_factory.begin() as session:
        with pytest.raises(DocketError) as error:
            McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(**context, timings=[
                _span(context), original.model_copy(update={"phase": "local_validation"}),
            ]))
        assert error.value.code == "trace_timing_conflict"
        assert session.scalar(select(func.count(TraceTimingObservation.id))) == 1


@pytest.mark.parametrize("change", [
    {"phase": "docket_execution"}, {"ended_at": None}, {"prompt": "not permitted"},
    {"started_at": "2026-01-01T00:00:00"},
    {"ended_at": "2026-01-02T00:00:00Z"},
    {"ended_at": "2026-09-01T00:00:00Z"},
])
def test_timing_schema_rejects_unmeasured_or_payload_bearing_inputs(change):
    with pytest.raises(ValidationError):
        TraceTimingInput.model_validate({
            "span_id": str(uuid.uuid4()), "phase": "model_request",
            "started_at": "2026-01-03T00:00:00Z", "ended_at": "2026-01-03T00:00:01Z",
            **change,
        })


@pytest.mark.parametrize("mode", ["before_turn", "future", "terminal", "other_source"])
def test_timing_is_bound_to_captured_running_turn(session_factory, mode):
    context = _context(session_factory)
    ref = new_public_ref("trace")
    with session_factory.begin() as session:
        McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(
            **context, timings=[_span(context)],
            turn_status="completed" if mode == "terminal" else "running",
        ))
    span = _span(context, left=-1 if mode == "before_turn" else 3, right=4)
    if mode == "future":
        span = _span(context, right=11)
    if mode == "other_source":
        context["utterance_ref"] = new_public_ref("utt")
    with session_factory.begin() as session:
        with pytest.raises((ValidationError, DocketError)):
            McpTraceService(session).checkpoint(ref, McpTraceCheckpoint(**context, timings=[span]))
        assert session.scalar(select(func.count(TraceTimingObservation.id))) == 1


@pytest.mark.parametrize("mode", ["update", "delete"])
def test_timing_rows_are_immutable(session_factory, mode):
    context = _context(session_factory)
    with session_factory.begin() as session:
        McpTraceService(session).checkpoint(new_public_ref("trace"), McpTraceCheckpoint(
            **context, timings=[_span(context)],
        ))
    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        row = session.scalar(select(TraceTimingObservation))
        if mode == "update":
            row.phase = "context_schema"
        else:
            session.delete(row)
