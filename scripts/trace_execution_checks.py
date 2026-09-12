"""Populated PostgreSQL trace-cutover proofs, using the isolated smoke database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.models import IntentSession, OperatorUtterance, SemanticRequest, TraceExecutionSegment


def test_retained_trace_migration_round_trip(factory: sessionmaker[Session]) -> None:
    """No guessed leases; original rows/outcomes survive upgrade/down/upgrade."""
    engine = factory.kw["bind"]
    assert engine.dialect.name == "postgresql"
    with engine.connect() as connection:
        assert not connection.scalar(text("SELECT count(*) FROM conversational_tool_traces"))
    command.downgrade(Config("alembic.ini"), "20260912a7e6")
    settings = get_settings()
    start = datetime(2026, 9, 1, 12, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    trace_id, timing_id = uuid.uuid4(), uuid.uuid4()
    ref, orphan_ref = new_public_ref("trace"), new_public_ref("trace")
    gateway_ref = new_public_ref("gwy")
    with factory.begin() as session:
        utterance = OperatorUtterance(
            actor_ref=f"discord_user:{settings.operator_discord_user_id}",
            transport="discord",
            source_message_ref=(
                f"discord_message:{settings.discord_guild_id}:"
                f"{settings.chat_channel_id}:1542799000000000799"
            ),
            conversation_ref=f"discord_conversation:{settings.chat_channel_id}",
            said_at=start,
            verbatim_text="Synthetic migration evidence; not an executable request.",
            content_hash="a" * 64,
            request_key="synthetic:trace-migration-evidence",
        )
        session.add(utterance)
        session.flush()
        intent = IntentSession(
            source_utterance_ref=utterance.ref_id,
            conversation_ref=utterance.conversation_ref,
        )
        session.add(intent)
        session.flush()
        request = SemanticRequest(
            intent_session_id=intent.id,
            intent_session_ref=intent.ref_id,
            authority_scope_hash="b" * 64,
            current_precondition_hash="c" * 64,
            origin_utterance_refs=[utterance.ref_id],
        )
        session.add(request)
        session.flush()
        utterance_ref, request_id, request_ref = utterance.ref_id, request.id, request.ref_id
    metadata = MetaData()
    names = (
        "conversational_tool_traces",
        "tool_invocations",
        "assembly_executions",
        "semantic_request_attempts",
        "trace_timing_observations",
    )
    tables = {name: Table(name, metadata, autoload_with=engine) for name in names}
    calls = [
        {
            "call_id": "captured-call",
            "ordinal": 1,
            "tool_name": "docket_commit_changeset",
            "transport_state": "completed",
            "disposition": "committed",
            "elapsed_ms": 17,
        }
    ]
    with engine.begin() as connection:
        connection.execute(
            tables["conversational_tool_traces"]
            .insert()
            .values(
                id=trace_id,
                ref_id=ref,
                guild_id=settings.discord_guild_id,
                source_channel_id=settings.chat_channel_id,
                source_message_id="1542799000000000799",
                actor_id=settings.operator_discord_user_id,
                gateway_instance_ref=gateway_ref,
                tool_contract_version="captured-test-contract",
                tool_contract_hash="d" * 64,
                caller_profile="interactive",
                started_at=start,
                completed_at=end,
                status="completed",
                created_at=start,
                updated_at=end,
                calls=calls,
                last_ordinal=1,
                version=7,
            )
        )
        for index, trace_ref in enumerate((ref, orphan_ref), start=1):
            call_ref, attempt_ref = new_public_ref("call"), new_public_ref("sattempt")
            connection.execute(
                tables["tool_invocations"]
                .insert()
                .values(
                    id=uuid.uuid4(),
                    ref_id=call_ref,
                    tool_name="docket_commit_changeset",
                    tool_contract_version="captured-test-contract",
                    tool_contract_hash="d" * 64,
                    caller_profile="interactive",
                    actor_ref=f"discord_user:{settings.operator_discord_user_id}",
                    utterance_refs=[utterance_ref],
                    started_at=start,
                    completed_at=end,
                    received_argument_hash="e" * 64,
                    result_refs=[],
                    result_disposition="committed",
                    transport_state="completed",
                    domain_state="succeeded",
                    trace_ref=trace_ref,
                    trace_call_id="captured-call",
                    trace_ordinal=1,
                    gateway_instance_ref=gateway_ref,
                )
            )
            connection.execute(
                tables["semantic_request_attempts"]
                .insert()
                .values(
                    id=uuid.uuid4(),
                    ref_id=attempt_ref,
                    semantic_request_id=request_id,
                    semantic_request_ref=request_ref,
                    attempt_number=index,
                    authority_scope_hash="b" * 64,
                    precondition_hash="c" * 64,
                    tool_call_ref=call_ref,
                    execution_trace_ref=trace_ref,
                    gateway_instance_ref=gateway_ref,
                    state="committed",
                    error_details_json={},
                    started_at=start,
                    completed_at=end,
                )
            )
            connection.execute(
                tables["assembly_executions"]
                .insert()
                .values(
                    id=uuid.uuid4(),
                    source_utterance_ref=utterance_ref,
                    trace_ref=trace_ref,
                    semantic_request_ref=request_ref,
                    semantic_request_attempt_ref=attempt_ref,
                    next_sequence=2,
                    created_at=start,
                    updated_at=end,
                )
            )
        connection.execute(
            tables["trace_timing_observations"]
            .insert()
            .values(
                id=timing_id,
                trace_ref=ref,
                phase="model_request",
                started_at=start,
                ended_at=end,
            )
        )
        before = {
            name: [
                dict(row)
                for row in connection.execute(select(table).order_by(table.c.id)).mappings()
            ]
            for name, table in tables.items()
        }
    command.upgrade(Config("alembic.ini"), "head")
    with factory() as session:
        segment = session.get(TraceExecutionSegment, trace_id)
        assert segment is not None and segment.trace_ref == ref
        assert segment.binding_basis == "retained_trace" and segment.execution_lease_id is None
        assert segment.execution_index == 1 and segment.calls == calls
        assert segment.gateway_instance_ref == gateway_ref and segment.started_at == start
        assert segment.completed_at == end and segment.status == "completed"
        assert segment.tool_contract_version == "captured-test-contract"
        assert session.scalar(text("SELECT count(*) FROM execution_leases")) == 0
        for name in names[1:]:
            rows = (
                session.execute(
                    text(f"SELECT trace_ref, trace_execution_id FROM {name} ORDER BY trace_ref")
                )
                .mappings()
                .all()
            )
            assert all(
                row["trace_execution_id"] == (trace_id if row["trace_ref"] == ref else None)
                for row in rows
            )
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM tool_invocations WHERE domain_state = 'succeeded' "
                    "AND result_disposition = 'committed' AND transport_state = 'completed'"
                )
            )
            == 2
        )
    for sql in (
        "UPDATE trace_execution_segments SET gateway_instance_ref = 'gwy_changed' WHERE id = :id",
        "DELETE FROM trace_execution_segments WHERE id = :id",
    ):
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), {"id": trace_id})
        except DBAPIError:
            pass
        else:
            raise AssertionError("PostgreSQL allowed rewriting retained trace bindings")
    command.downgrade(Config("alembic.ini"), "20260912a7e6")
    with engine.connect() as connection:
        after = {
            name: [
                dict(row)
                for row in connection.execute(select(table).order_by(table.c.id)).mappings()
            ]
            for name, table in tables.items()
        }
        assert after == before
    command.upgrade(Config("alembic.ini"), "head")
