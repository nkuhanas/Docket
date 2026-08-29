import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import is_public_ref, new_public_ref, parse_public_ref
from docket.internal_api.schemas import (
    AgentResponseCapture,
    AgentResponseDeliveryUpdate,
    AgentTurnNoResponse,
    McpTraceUpdate,
    OperatorUtteranceCapture,
    SpecificationSignoffCapture,
)
from docket.mcp.instrumented import ProvenanceFastMCP
from docket.models import (
    AgentResponse,
    AgentResponseProjection,
    AuditEvent,
    Decision,
    IntentTurn,
    OperatorUtterance,
    Record,
    RecordSource,
    ToolInvocation,
)
from docket.schemas.authority import IntentSessionOpen, IntentTurnAppend
from docket.services.intent_sessions import IntentSessionService
from docket.services.mcp_traces import McpTraceService, trace_id_for_source
from docket.services.provenance import (
    BOOTSTRAP_AUTHORIZATION_TEXT,
    BOOTSTRAP_CANONICAL_KEY,
    FINAL_ARCHITECTURE_SIGNOFF_TEXT,
    FROZEN_ARTIFACT_HASH,
    FROZEN_DOCUMENT_REF,
    ProvenanceService,
)
from docket.specification_artifacts import specification_artifact
from docket.tool_contracts import CONTRACT_VERSION, contract_hash

MESSAGE_ID = "1542778234028953620"


def _utterance_request(
    *,
    text: str = "lol",
    message_id: str = MESSAGE_ID,
) -> OperatorUtteranceCapture:
    settings = get_settings()
    return OperatorUtteranceCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "actor_id": settings.operator_discord_user_id,
            "verbatim_text": text,
            "request_key": (
                f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                f"{message_id}:0"
            ),
        }
    )


@pytest.mark.integration
def test_public_refs_are_typed_ulids() -> None:
    refs = {new_public_ref("utt") for _ in range(100)}
    assert len(refs) == 100
    assert all(is_public_ref(ref) for ref in refs)
    assert all(parse_public_ref(ref)[0] == "utt" for ref in refs)
    assert not is_public_ref("utt_not-a-ulid")


@pytest.mark.integration
def test_operator_utterance_is_verbatim_idempotent_and_immutable(session_factory) -> None:
    request = _utterance_request(text="lol\nexactly as typed")
    with session_factory.begin() as session:
        created = ProvenanceService(session).capture_operator_utterance(request)
    with session_factory.begin() as session:
        replay = ProvenanceService(session).capture_operator_utterance(request)

    assert created["ref"].startswith("utt_")
    assert replay["ref"] == created["ref"]
    assert replay["disposition"] == "replayed_request"
    with session_factory() as session:
        utterance = session.scalar(select(OperatorUtterance))
        assert utterance is not None
        assert utterance.verbatim_text == "lol\nexactly as typed"
        assert utterance.said_at.replace(tzinfo=UTC) == datetime(
            2026, 8, 28, 6, 9, 54, 426000, tzinfo=UTC
        )
        assert session.scalar(select(func.count()).select_from(OperatorUtterance)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1

    with pytest.raises(ValueError, match="immutable"), session_factory.begin() as session:
        utterance = session.scalar(select(OperatorUtterance))
        assert utterance is not None
        utterance.verbatim_text = "rewritten"


@pytest.mark.integration
def test_agent_response_persists_before_delivery_and_advances_only_delivery_state(
    session_factory,
) -> None:
    settings = get_settings()
    request = _utterance_request(text="answer me")
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        settings.chat_channel_id,
        MESSAGE_ID,
    )
    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(request)["ref"]
        invocation = ToolInvocation(
            tool_name="docket_search_records",
            tool_contract_version="test",
            caller_profile="interactive",
            status="succeeded",
            received_argument_hash="a" * 64,
            normalized_argument_hash="a" * 64,
            completed_at=datetime.now(UTC),
            trace_id=trace_id,
            trace_call_id="call-1",
            trace_ordinal=1,
            utterance_refs=[utterance_ref],
        )
        session.add(invocation)
        session.flush()
        call_ref = invocation.ref_id

    capture = AgentResponseCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "source_message_id": MESSAGE_ID,
            "actor_id": settings.operator_discord_user_id,
            "utterance_ref": utterance_ref,
            "turn_id": "turn-1",
            "session_id": "session-1",
            "model_identifier": "test-model",
            "verbatim_text": "Final assembled answer.",
            "generated_at": datetime.now(UTC).isoformat(),
            "trace_id": str(trace_id),
        }
    )
    with session_factory.begin() as session:
        created = ProvenanceService(session).capture_agent_response(capture)

    completed_at = datetime.now(UTC)
    delivery = AgentResponseDeliveryUpdate.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "response_ref": created["ref"],
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "source_message_id": MESSAGE_ID,
            "actor_id": settings.operator_discord_user_id,
            "outcome": "delivered",
            "completed_at": completed_at.isoformat(),
        }
    )
    with session_factory.begin() as session:
        updated = ProvenanceService(session).update_agent_response_delivery(delivery)
    assert updated["state"] == "delivered"

    with session_factory() as session:
        response = session.scalar(select(AgentResponse))
        projection = session.scalar(select(AgentResponseProjection))
        assert response is not None and projection is not None
        assert response.verbatim_text == "Final assembled answer."
        assert response.responds_to_utterance_refs == [utterance_ref]
        assert response.tool_call_refs == [call_ref]
        assert response.basis_refs == [utterance_ref, call_ref]
        assert response.delivery_state == "delivered"
        assert response.delivered_at is not None
        assert response.delivered_at.replace(tzinfo=UTC) == completed_at
        assert projection.status == "delivered"
        assert projection.attempt_count == 1
        assert projection.operator_ref == f"discord_user:{settings.operator_discord_user_id}"
        assert projection.primary_public_ref == response.ref_id
        assert projection.projection_version == 1
        assert projection.case_revision_refs == []
        assert projection.brief_ref is None

    with pytest.raises(ValueError, match="semantic fields"), session_factory.begin() as session:
        response = session.scalar(select(AgentResponse))
        assert response is not None
        response.verbatim_text = "different answer"


@pytest.mark.integration
def test_agent_turn_is_finalized_by_response_or_explicit_no_response(session_factory) -> None:
    settings = get_settings()
    first_message_id = MESSAGE_ID
    second_message_id = str(int(MESSAGE_ID) + 1)

    def open_pending_turn(session, message_id: str, text: str) -> tuple[str, str]:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(text=text, message_id=message_id)
        )["ref"]
        intent_service = IntentSessionService(session)
        intent_session, _ = intent_service.open(
            IntentSessionOpen(source_utterance_ref=utterance_ref)
        )
        _, turn = intent_service.append_turn(
            IntentTurnAppend(
                intent_session_ref=intent_session.ref_id,
                utterance_ref=utterance_ref,
                blocking_clarifications=[
                    {"blocking": True, "question": "Which one?"}
                ],
                response_disposition="pending",
            )
        )
        return utterance_ref, turn.ref_id

    with session_factory.begin() as session:
        first_utterance_ref, first_turn_ref = open_pending_turn(
            session, first_message_id, "Clarify this one"
        )
        trace_id = trace_id_for_source(
            settings.discord_guild_id,
            settings.chat_channel_id,
            first_message_id,
        )
        invocation = ToolInvocation(
            tool_name="docket_get_intent_session",
            tool_contract_version="test",
            caller_profile="interactive",
            status="succeeded",
            received_argument_hash="b" * 64,
            normalized_argument_hash="b" * 64,
            completed_at=datetime.now(UTC),
            trace_id=trace_id,
            trace_call_id="call-1",
            trace_ordinal=1,
            utterance_refs=[first_utterance_ref],
        )
        session.add(invocation)
        session.flush()
        call_ref = invocation.ref_id

    response_capture = AgentResponseCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "source_message_id": first_message_id,
            "actor_id": settings.operator_discord_user_id,
            "utterance_ref": first_utterance_ref,
            "turn_id": "intent-response-turn",
            "session_id": "intent-response-session",
            "model_identifier": "test-model",
            "verbatim_text": "Which one did you mean?",
            "generated_at": datetime.now(UTC).isoformat(),
            "trace_id": str(trace_id),
        }
    )
    with session_factory.begin() as session:
        response_result = ProvenanceService(session).capture_agent_response(response_capture)

    with session_factory() as session:
        first_turn = session.scalar(
            select(IntentTurn).where(IntentTurn.ref_id == first_turn_ref)
        )
        response = session.scalar(
            select(AgentResponse).where(AgentResponse.ref_id == response_result["ref"])
        )
        invocation = session.scalar(
            select(ToolInvocation).where(ToolInvocation.ref_id == call_ref)
        )
        assert first_turn is not None and response is not None and invocation is not None
        assert first_turn.response_disposition == "final_response"
        assert first_turn.agent_response_ref == response.ref_id
        assert first_turn.tool_call_refs == [call_ref]
        assert response.intent_session_ref == first_turn.intent_session_ref
        assert invocation.intent_session_ref == first_turn.intent_session_ref

    with session_factory.begin() as session:
        second_utterance_ref, second_turn_ref = open_pending_turn(
            session, second_message_id, "Do this silently"
        )
    no_response_capture = AgentTurnNoResponse.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "source_message_id": second_message_id,
            "actor_id": settings.operator_discord_user_id,
            "utterance_ref": second_utterance_ref,
            "turn_id": "intent-no-response-turn",
            "session_id": "intent-no-response-session",
            "trace_id": str(
                trace_id_for_source(
                    settings.discord_guild_id,
                    settings.chat_channel_id,
                    second_message_id,
                )
            ),
        }
    )
    with session_factory.begin() as session:
        no_response = ProvenanceService(session).finalize_agent_turn_without_response(
            no_response_capture
        )
        replay = ProvenanceService(session).finalize_agent_turn_without_response(
            no_response_capture
        )
    assert no_response["ref"] == second_turn_ref
    assert replay["ref"] == second_turn_ref
    with session_factory() as session:
        second_turn = session.scalar(
            select(IntentTurn).where(IntentTurn.ref_id == second_turn_ref)
        )
        assert second_turn is not None
        assert second_turn.response_disposition == "no_response"
        assert second_turn.agent_response_ref is None


@pytest.mark.integration
def test_failed_mutation_refs_do_not_block_final_response(session_factory) -> None:
    settings = get_settings()
    request = _utterance_request(text="apply this exact correction")
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        settings.chat_channel_id,
        MESSAGE_ID,
    )
    phantom_ref = new_public_ref("idn")
    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(request)["ref"]
        intent_service = IntentSessionService(session)
        intent_session, _ = intent_service.open(
            IntentSessionOpen(source_utterance_ref=utterance_ref)
        )
        _, turn = intent_service.append_turn(
            IntentTurnAppend(
                intent_session_ref=intent_session.ref_id,
                utterance_ref=utterance_ref,
                blocking_clarifications=[],
                response_disposition="pending",
            )
        )
        failed = ToolInvocation(
            tool_name="docket_commit_changeset",
            tool_contract_version="test",
            caller_profile="interactive",
            status="failed",
            received_argument_hash="c" * 64,
            normalized_argument_hash="c" * 64,
            result_refs=[phantom_ref],
            error_code="active_email_identity_required",
            completed_at=datetime.now(UTC),
            trace_id=trace_id,
            trace_call_id="call-failed",
            trace_ordinal=1,
            utterance_refs=[utterance_ref],
        )
        succeeded = ToolInvocation(
            tool_name="docket_commit_changeset",
            tool_contract_version="test",
            caller_profile="interactive",
            status="succeeded",
            received_argument_hash="d" * 64,
            normalized_argument_hash="d" * 64,
            result_refs=[intent_session.ref_id],
            completed_at=datetime.now(UTC),
            trace_id=trace_id,
            trace_call_id="call-succeeded",
            trace_ordinal=2,
            utterance_refs=[utterance_ref],
        )
        session.add_all([failed, succeeded])
        session.flush()
        failed_ref = failed.ref_id
        succeeded_ref = succeeded.ref_id
        turn_ref = turn.ref_id
        intent_session_ref = intent_session.ref_id

    capture = AgentResponseCapture.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "source_message_id": MESSAGE_ID,
            "actor_id": settings.operator_discord_user_id,
            "utterance_ref": utterance_ref,
            "turn_id": "response-after-retry",
            "session_id": "session-after-retry",
            "model_identifier": "test-model",
            "verbatim_text": "The correction is committed.",
            "generated_at": datetime.now(UTC).isoformat(),
            "trace_id": str(trace_id),
        }
    )
    with session_factory.begin() as session:
        response_result = ProvenanceService(session).capture_agent_response(capture)

    with session_factory() as session:
        response = session.scalar(
            select(AgentResponse).where(AgentResponse.ref_id == response_result["ref"])
        )
        turn = session.scalar(select(IntentTurn).where(IntentTurn.ref_id == turn_ref))
        assert response is not None and turn is not None
        assert response.tool_call_refs == [failed_ref, succeeded_ref]
        assert turn.tool_call_refs == [failed_ref, succeeded_ref]
        assert turn.resulting_semantic_refs == [intent_session_ref]
        assert phantom_ref not in turn.resulting_semantic_refs
        assert turn.response_disposition == "final_response"


@pytest.mark.integration
def test_tool_invocation_is_created_before_validation(session_factory) -> None:
    server = ProvenanceFastMCP("instrumented-test", caller_profile="interactive")

    @server.tool()
    def bounded_tool(value: int) -> dict[str, object]:
        return {"ok": True, "value": value}

    with pytest.raises(ToolError):
        import asyncio

        asyncio.run(server.call_tool("bounded_tool", {"value": "not-an-int"}))

    with session_factory() as session:
        invocation = session.scalar(select(ToolInvocation))
        assert invocation is not None
        assert invocation.ref_id.startswith("call_")
        assert invocation.status == "rejected_validation"
        assert invocation.normalized_argument_hash is None
        assert invocation.completed_at is not None


@pytest.mark.integration
def test_failed_tool_invocation_omits_non_durable_result_refs(session_factory) -> None:
    import asyncio

    phantom_ref = new_public_ref("idn")
    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(text="inspect this")
        )["ref"]

    server = ProvenanceFastMCP("durable-result-ref-test", caller_profile="interactive")

    @server.tool()
    def failed_read() -> dict[str, object]:
        return {
            "ok": False,
            "error": {"code": "internal_error", "message": "Lookup failed."},
            "affected_refs": [utterance_ref, phantom_ref],
        }

    result = asyncio.run(server.call_tool("failed_read", {}))
    assert result
    with session_factory() as session:
        invocation = session.scalar(select(ToolInvocation))
        assert invocation is not None
        assert invocation.status == "failed"
        assert invocation.result_refs == []
        assert phantom_ref not in invocation.result_refs


@pytest.mark.integration
def test_mutating_tool_requires_and_binds_operator_utterance_authority(
    session_factory,
) -> None:
    import asyncio

    settings = get_settings()
    calls: list[str] = []
    server = ProvenanceFastMCP("mutation-authority-test", caller_profile="interactive")

    @server.tool(name="docket_commit_changeset")
    def commit_changeset(
        utterance_ref: str, request_key: str, actor_id: str
    ) -> dict[str, object]:
        del utterance_ref
        calls.append(request_key)
        return {"ok": True, "state": "committed"}

    request_key = (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{MESSAGE_ID}:1"
    )
    arguments = {
        "utterance_ref": new_public_ref("utt"),
        "request_key": request_key,
        "actor_id": settings.operator_discord_user_id,
    }
    with pytest.raises(ToolError, match="operator_utterance_authority_required"):
        asyncio.run(server.call_tool("docket_commit_changeset", arguments))
    assert calls == []

    with session_factory() as session:
        rejected = session.scalar(select(ToolInvocation))
        assert rejected is not None
        assert rejected.status == "rejected_authority"
        assert rejected.error_code == "operator_utterance_authority_required"
        assert rejected.normalized_argument_hash == sha256_json(arguments)
        assert rejected.actor_ref is None
        assert rejected.utterance_refs == []

    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(text="update that")
        )["ref"]

    arguments["utterance_ref"] = utterance_ref
    result = asyncio.run(server.call_tool("docket_commit_changeset", arguments))
    assert result
    assert calls == [request_key]
    with session_factory() as session:
        succeeded = session.scalar(
            select(ToolInvocation)
            .where(ToolInvocation.status == "succeeded")
            .order_by(ToolInvocation.started_at.desc())
        )
        assert succeeded is not None
        assert succeeded.actor_ref == f"discord_user:{settings.operator_discord_user_id}"
        assert succeeded.utterance_refs == [utterance_ref]


@pytest.mark.integration
def test_read_only_tool_does_not_require_operator_utterance(session_factory) -> None:
    import asyncio

    server = ProvenanceFastMCP("read-authority-test", caller_profile="interactive")

    @server.tool(name="docket_search_records")
    def search_records(query: str) -> dict[str, object]:
        return {"ok": True, "query": query, "disposition": "matched_existing"}

    result = asyncio.run(server.call_tool("docket_search_records", {"query": "term"}))
    assert result
    with session_factory() as session:
        invocation = session.scalar(select(ToolInvocation))
        assert invocation is not None
        assert invocation.status == "succeeded"
        assert invocation.result_disposition == "matched_existing"
        assert invocation.utterance_refs == []


@pytest.mark.integration
def test_trace_enriches_boundary_invocation_with_operator_utterance(session_factory) -> None:
    settings = get_settings()
    arguments = {"query": "term"}
    argument_hash = sha256_json(arguments)
    trace_id = trace_id_for_source(
        settings.discord_guild_id,
        settings.chat_channel_id,
        MESSAGE_ID,
    )
    with session_factory.begin() as session:
        utterance_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(text="find my term")
        )["ref"]
        invocation = ToolInvocation(
            tool_name="docket_search_records",
            tool_contract_version="test",
            caller_profile="interactive",
            status="succeeded",
            received_argument_hash=argument_hash,
            normalized_argument_hash=argument_hash,
            completed_at=datetime.now(UTC),
        )
        session.add(invocation)
        session.flush()
        call_ref = invocation.ref_id

    update = McpTraceUpdate.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "guild_id": settings.discord_guild_id,
            "source_channel_id": settings.chat_channel_id,
            "source_message_id": MESSAGE_ID,
            "actor_id": settings.operator_discord_user_id,
            "tool_contract_version": CONTRACT_VERSION,
            "tool_contract_hash": contract_hash("interactive"),
            "caller_profile": "interactive",
            "updated_at": datetime.now(UTC).isoformat(),
            "call": {
                "call_id": "call-1",
                "ordinal": 1,
                "tool_name": "docket_search_records",
                "transport_state": "running",
                "received_argument_hash": argument_hash,
            },
        }
    )
    with session_factory.begin() as session:
        result = McpTraceService(session).update(trace_id, update)
        assert result["tool_call_ref"] == call_ref

    with session_factory() as session:
        invocation = session.scalar(
            select(ToolInvocation).where(ToolInvocation.ref_id == call_ref)
        )
        assert invocation is not None
        assert invocation.trace_id == trace_id
        assert invocation.trace_call_id == "call-1"
        assert invocation.trace_ordinal == 1
        assert invocation.actor_ref == f"discord_user:{settings.operator_discord_user_id}"
        assert invocation.utterance_refs == [utterance_ref]


@pytest.mark.integration
def test_bootstrap_authorization_backfills_exact_utterance_and_decision(session_factory) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        record = Record(
            record_type="generic",
            canonical_key=BOOTSTRAP_CANONICAL_KEY,
            title="Authorization: Docket provenance bootstrap phase 1",
            status="active",
            data={
                "utterance": BOOTSTRAP_AUTHORIZATION_TEXT,
                "architecture_sha256": FROZEN_ARTIFACT_HASH,
                "authorized_phase": "provenance-bootstrap phase 1",
                "authorization_status": "authorized",
            },
        )
        session.add(record)
        session.flush()
        session.add(
            RecordSource(
                record_id=record.id,
                source_type="discord_message",
                source_object_id=MESSAGE_ID,
                source_request_key=(
                    f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                    f"{MESSAGE_ID}:0"
                ),
                source_metadata={
                    "guild_id": settings.discord_guild_id,
                    "channel_id": settings.chat_channel_id,
                    "message_id": MESSAGE_ID,
                    "user_id": settings.operator_discord_user_id,
                    "intent_index": 0,
                },
            )
        )

    with session_factory.begin() as session:
        refs = ProvenanceService(session).backfill_bootstrap_authorization()
    assert refs is not None
    with session_factory.begin() as session:
        assert ProvenanceService(session).backfill_bootstrap_authorization() == refs

    with session_factory() as session:
        utterance = session.scalar(select(OperatorUtterance))
        decision = session.scalar(select(Decision))
        assert utterance is not None and decision is not None
        assert utterance.verbatim_text == BOOTSTRAP_AUTHORIZATION_TEXT
        assert decision.ref_id == refs["decision_ref"]
        assert decision.decision_kind == "provenance_bootstrap_signoff"
        assert decision.document_ref == FROZEN_DOCUMENT_REF
        assert decision.frozen_artifact_hash == FROZEN_ARTIFACT_HASH
        assert decision.authorized_scope == "provenance_bootstrap_only"
        assert decision.architecture_authority is False
        assert decision.basis_refs == [utterance.ref_id]
        assert session.scalar(select(func.count()).select_from(Decision)) == 1


@pytest.mark.integration
def test_final_architecture_signoff_requires_exact_ledger_utterance(session_factory) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        record = Record(
            record_type="generic",
            canonical_key=BOOTSTRAP_CANONICAL_KEY,
            title="Authorization: Docket provenance bootstrap phase 1",
            status="active",
            data={
                "utterance": BOOTSTRAP_AUTHORIZATION_TEXT,
                "architecture_sha256": FROZEN_ARTIFACT_HASH,
                "authorized_phase": "provenance-bootstrap phase 1",
                "authorization_status": "authorized",
            },
        )
        session.add(record)
        session.flush()
        session.add(
            RecordSource(
                record_id=record.id,
                source_type="discord_message",
                source_object_id=MESSAGE_ID,
                source_request_key=(
                    f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                    f"{MESSAGE_ID}:0"
                ),
                source_metadata={
                    "guild_id": settings.discord_guild_id,
                    "channel_id": settings.chat_channel_id,
                    "message_id": MESSAGE_ID,
                    "user_id": settings.operator_discord_user_id,
                    "intent_index": 0,
                },
            )
        )
    with session_factory.begin() as session:
        ProvenanceService(session).backfill_bootstrap_authorization()

    signoff_message_id = "1542779000000000000"
    with session_factory.begin() as session:
        signoff_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(
                text=FINAL_ARCHITECTURE_SIGNOFF_TEXT,
                message_id=signoff_message_id,
            )
        )["ref"]
        request = SpecificationSignoffCapture.model_validate(
            {
                "request_id": str(uuid.uuid4()),
                "utterance_ref": signoff_ref,
                "document_ref": FROZEN_DOCUMENT_REF,
                "frozen_artifact_hash": FROZEN_ARTIFACT_HASH,
            }
        )
        result = ProvenanceService(session).record_final_architecture_signoff(request)
        replay = ProvenanceService(session).record_final_architecture_signoff(request)

    assert result["ref"].startswith("dec_")
    assert replay["ref"] == result["ref"]
    assert replay["disposition"] == "replayed_request"
    with session_factory() as session:
        decision = session.scalar(
            select(Decision).where(Decision.decision_kind == "specification_signoff")
        )
        assert decision is not None
        assert decision.basis_refs == [signoff_ref]
        assert decision.architecture_authority is True
        assert decision.implementation_authority == "gated_by_ONT-INV-0011"

    non_exact_message_id = "1542779000000000001"
    with session_factory.begin() as session:
        non_exact_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(
                text=f"{FINAL_ARCHITECTURE_SIGNOFF_TEXT} please",
                message_id=non_exact_message_id,
            )
        )["ref"]
        non_exact_request = SpecificationSignoffCapture.model_validate(
            {
                "request_id": str(uuid.uuid4()),
                "utterance_ref": non_exact_ref,
                "document_ref": FROZEN_DOCUMENT_REF,
                "frozen_artifact_hash": FROZEN_ARTIFACT_HASH,
            }
        )
        with pytest.raises(DocketError) as exc_info:
            ProvenanceService(session).record_final_architecture_signoff(non_exact_request)
        assert exc_info.value.code == "specification_signoff_not_explicit"


@pytest.mark.integration
def test_manifest_bound_amendment_signoff_requires_bootstrap_and_base_signoff(
    session_factory,
) -> None:
    settings = get_settings()
    document_ref = "ONT-DELTA-2026-08-28-CASE-RESOLUTION"
    frozen_hash = "058788ec6728565b51bbce3e80d51146c52fec0c0364f7599e3877f97d964a05"
    artifact = specification_artifact(document_ref, frozen_hash)
    assert artifact is not None and artifact.bootstrap_authority is not None
    bootstrap_text = (
        "I authorize only the amendment-signoff bootstrap needed to make\n"
        "  ONT-DELTA-2026-08-28-CASE-RESOLUTION frozen at SHA-256 "
        "058788ec6728565b51bbce3e80d51146c52fec0c0364f7599e3877f97d964a05\n"
        "  signable through the Docket ledger. This does not authorize the amendment's\n"
        "  case-resolution, triage, schema, migration, or tool-outcome behavior."
    )
    assert hashlib.sha256(bootstrap_text.encode()).hexdigest() == (
        artifact.bootstrap_authority.content_hash
    )
    with session_factory.begin() as session:
        session.add(
            OperatorUtterance(
                ref_id=artifact.bootstrap_authority.utterance_ref,
                actor_ref=f"discord_user:{settings.operator_discord_user_id}",
                transport="discord",
                source_message_ref=(
                    f"discord_message:{settings.discord_guild_id}:"
                    f"{settings.chat_channel_id}:1543023122465423372"
                ),
                conversation_ref=(
                    f"discord_conversation:{settings.discord_guild_id}:"
                    f"{settings.chat_channel_id}"
                ),
                said_at=datetime.now(UTC),
                verbatim_text=bootstrap_text,
                content_hash=artifact.bootstrap_authority.content_hash,
                request_key=(
                    f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
                    "1543023122465423372:0"
                ),
            )
        )
        session.add(
            Decision(
                decision_kind=artifact.prerequisite.decision_kind,
                actor_ref=f"discord_user:{settings.operator_discord_user_id}",
                basis_refs=[artifact.bootstrap_authority.utterance_ref],
                document_ref=artifact.prerequisite.document_ref,
                frozen_artifact_hash=artifact.prerequisite.frozen_artifact_hash,
                architecture_authority=True,
                implementation_authority="gated_by_ONT-INV-0011",
            )
        )

    signoff_message_id = "1543024000000000000"
    with session_factory.begin() as session:
        signoff_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(
                text=artifact.signoff_text,
                message_id=signoff_message_id,
            )
        )["ref"]
        request = SpecificationSignoffCapture(
            request_id=uuid.uuid4(),
            utterance_ref=signoff_ref,
            document_ref=document_ref,
            frozen_artifact_hash=frozen_hash,
        )
        result = ProvenanceService(session).record_final_architecture_signoff(request)
        replay = ProvenanceService(session).record_final_architecture_signoff(request)

    assert result["ref"].startswith("dec_")
    assert replay == {**result, "disposition": "replayed_request"}
    with session_factory() as session:
        decision = session.scalar(
            select(Decision).where(
                Decision.document_ref == document_ref,
                Decision.frozen_artifact_hash == frozen_hash,
            )
        )
        assert decision is not None
        assert decision.basis_refs == [signoff_ref]
        assert decision.authorized_scope == (
            "attention_case_resolution_and_tool_outcome_amendment"
        )
        assert decision.architecture_authority is True
        assert decision.implementation_authority == "amendment_scope"
        assert decision.payload_json["bootstrap_utterance_ref"] == (
            artifact.bootstrap_authority.utterance_ref
        )

    with session_factory.begin() as session:
        wrong_hash_request = request.model_copy(
            update={"request_id": uuid.uuid4(), "frozen_artifact_hash": "0" * 64}
        )
        with pytest.raises(DocketError) as exc_info:
            ProvenanceService(session).record_final_architecture_signoff(
                wrong_hash_request
            )
        assert exc_info.value.code == "specification_signoff_artifact_mismatch"


@pytest.mark.integration
def test_later_manifest_bound_amendment_uses_existing_base_signoff(
    session_factory,
) -> None:
    settings = get_settings()
    document_ref = "ONT-DELTA-2026-08-28-INTERACTIVE-CONTINUITY"
    frozen_hash = "972784149dd2a219d027684a76f04fac37d8147e9656a3ff06326d883fd06579"
    artifact = specification_artifact(document_ref, frozen_hash)
    assert artifact is not None
    assert artifact.bootstrap_authority is None

    with session_factory.begin() as session:
        prerequisite = Decision(
            decision_kind=artifact.prerequisite.decision_kind,
            actor_ref=f"discord_user:{settings.operator_discord_user_id}",
            basis_refs=[f"utt_{'1' * 26}"],
            document_ref=artifact.prerequisite.document_ref,
            frozen_artifact_hash=artifact.prerequisite.frozen_artifact_hash,
            architecture_authority=True,
            implementation_authority="gated_by_ONT-INV-0011",
        )
        session.add(prerequisite)
        session.flush()
        prerequisite_ref = prerequisite.ref_id

    signoff_message_id = "1543050207879495760"
    with session_factory.begin() as session:
        signoff_ref = ProvenanceService(session).capture_operator_utterance(
            _utterance_request(
                text=artifact.signoff_text,
                message_id=signoff_message_id,
            )
        )["ref"]
        request = SpecificationSignoffCapture(
            request_id=uuid.uuid4(),
            utterance_ref=signoff_ref,
            document_ref=document_ref,
            frozen_artifact_hash=frozen_hash,
        )
        result = ProvenanceService(session).record_final_architecture_signoff(request)
        replay = ProvenanceService(session).record_final_architecture_signoff(request)

    assert result["ref"].startswith("dec_")
    assert replay == {**result, "disposition": "replayed_request"}
    with session_factory() as session:
        decision = session.scalar(
            select(Decision).where(
                Decision.document_ref == document_ref,
                Decision.frozen_artifact_hash == frozen_hash,
            )
        )
        assert decision is not None
        assert decision.basis_refs == [signoff_ref]
        assert decision.authorized_scope == (
            "interactive_authority_continuity_and_deployment_drain_amendment"
        )
        assert decision.architecture_authority is True
        assert decision.implementation_authority == "amendment_scope"
        assert decision.payload_json["prerequisite_decision_ref"] == prerequisite_ref
        assert decision.payload_json["bootstrap_utterance_ref"] is None
