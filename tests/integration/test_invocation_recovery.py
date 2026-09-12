from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.mcp.instrumented import ProvenanceFastMCP, _result_envelope
from docket.models import (
    AssemblyOperation,
    ConversationalToolTrace,
    GatewayLifetime,
    Item,
    OperatorUtterance,
    OutboxEvent,
    SemanticRequest,
    SemanticRequestAttempt,
    ToolInvocation,
)
from docket.schemas.assembly import ReviewChangesInput, StageChangesInput
from docket.services.changeset_assembly import (
    ChangeSetAssemblyAdmissionService,
    ChangeSetAssemblyService,
)
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.invocation_outcomes import recover_assembly_outcome
from docket.services.trace_views import TraceViewService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


def _context(session):
    settings = get_settings()
    gateway = GatewayLifetimeService(session).register(
        registration_key=uuid.uuid4(), instance_kind="hermes_discord_gateway",
    )
    text = "Track this item."
    message_id = "1542799000000000782"
    utterance = OperatorUtterance(
        actor_ref=f"discord_user:{settings.operator_discord_user_id}", transport="discord",
        source_message_ref=(
            f"discord_message:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}"
        ),
        conversation_ref=f"discord_conversation:{settings.discord_guild_id}:{settings.chat_channel_id}",
        said_at=datetime.now(UTC), verbatim_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        request_key=f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0",
    )
    session.add(utterance)
    trace = ConversationalToolTrace(
        ref_id=new_public_ref("trace"), guild_id=settings.discord_guild_id,
        source_channel_id=settings.chat_channel_id, source_message_id=message_id,
        actor_id=settings.operator_discord_user_id, tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"), caller_profile="interactive",
        gateway_instance_ref=gateway["ref"], status="running", calls=[], last_ordinal=0,
        version=1, started_at=datetime.now(UTC),
    )
    session.add(trace)
    session.flush()
    return utterance, trace


def _admit(session, utterance, trace, kind, ordinal, digest=None):
    tool = f"docket_{kind}_changes" if kind == "stage" else f"docket_{kind}_changeset"
    digest = digest or hashlib.sha256(f"{kind}:{ordinal}".encode()).hexdigest()
    settings = get_settings()
    admitted = ChangeSetAssemblyAdmissionService(session).admit(
        utterance_ref=utterance.ref_id, trace_ref=trace.ref_id,
        upstream_tool_call_id=f"call-{ordinal}", trace_ordinal=ordinal,
        tool_name=tool, argument_hash=digest, guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id, source_message_id=trace.source_message_id,
        actor_id=settings.operator_discord_user_id,
    )
    invocation = ToolInvocation(
        tool_name=tool, tool_contract_version=CONTRACT_VERSION,
        tool_contract_hash=contract_hash("interactive"), caller_profile="interactive",
        actor_ref=utterance.actor_ref, utterance_refs=[utterance.ref_id],
        received_argument_hash=digest, trace_ref=trace.ref_id, trace_call_id=f"call-{ordinal}",
        trace_ordinal=ordinal, gateway_instance_ref=trace.gateway_instance_ref,
    )
    session.add(invocation)
    trace.calls = [*trace.calls, {
        "call_id": f"call-{ordinal}", "ordinal": ordinal, "tool_name": tool,
        "received_argument_hash": digest, "transport_state": "running",
        "domain_state": "unknown", "execution_boundary": "docket_dispatch",
    }]
    trace.last_ordinal = ordinal
    session.flush()
    return invocation, {
        "assembly_operation_token": admitted["assembly_operation_token"],
        "assembly_argument_hash": digest,
    }


def _stage(utterance):
    return StageChangesInput.model_validate({
        "utterance_ref": utterance.ref_id, "request_key": utterance.request_key,
        "assembly_scope": {
            "resolved_intent": {"intent": "track item"},
            "allowed_mutation_types": ["item_create"], "planned_create_types": ["item"],
        },
        "patch": {"operations": [{"operation": "action_upsert", "action": {
            "mutation_type": "item_create", "change_id": "tracked-item", "action": "create",
            "object_type": "item", "affected_fields": ["title"],
            "basis_refs": [utterance.ref_id], "create_spec": {"title": "Tracked item"},
        }}]},
    })


def _expire(session, trace):
    gateway = session.scalar(select(GatewayLifetime).where(
        GatewayLifetime.ref_id == trace.gateway_instance_ref,
    ))
    gateway.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)


@pytest.mark.integration
@pytest.mark.parametrize("missing_callback", [False, True])
def test_restart_recovers_exact_operation_not_later_request_outcome(
    session_factory, missing_callback,
):
    with session_factory.begin() as session:
        utterance, trace = _context(session)
        service = ChangeSetAssemblyService(session)
        rejected, rejected_binding = _admit(session, utterance, trace, "stage", 1)
        service.reject_admitted_operation(
            token=rejected_binding["assembly_operation_token"],
            argument_hash=rejected_binding["assembly_argument_hash"],
            operation_kind="stage", utterance_ref=utterance.ref_id,
            error=DocketError(code="invalid_patch", message="Synthetic structural error."),
        )
        staged, stage_binding = _admit(session, utterance, trace, "stage", 2)
        assert service.stage(_stage(utterance), **stage_binding)["disposition"] == "ready_to_commit"
        _reviewed, review_binding = _admit(session, utterance, trace, "review", 3)
        service.review(ReviewChangesInput(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key,
        ), **review_binding)
        committed, commit_binding = _admit(session, utterance, trace, "commit", 4)
        receipt = service.commit(
            utterance_ref=utterance.ref_id, request_key=utterance.request_key, **commit_binding,
        )
        # Old recovery incorrectly assigned this request's commit to earlier
        # calls. Normal finalization may not even have supplied the ref yet.
        staged.semantic_request_ref = receipt["semantic_request_ref"]
        rejected.semantic_request_ref = receipt["semantic_request_ref"]
        retry = ToolInvocation(**{field: getattr(committed, field) for field in (
            "tool_name", "tool_contract_version", "tool_contract_hash", "caller_profile",
            "actor_ref", "utterance_refs", "received_argument_hash", "trace_ref",
            "trace_ordinal", "gateway_instance_ref",
        )})
        session.add(retry)
        if missing_callback:
            trace.calls = []
            trace.last_ordinal = 0
        _expire(session, trace)
        trace_ref = trace.ref_id
    with session_factory.begin() as session:
        GatewayLifetimeService(session).expire_and_reconcile()
    with session_factory() as session:
        calls = list(session.scalars(select(ToolInvocation).order_by(ToolInvocation.started_at)))
        assert [row.result_disposition for row in calls] == [
            "rejected_validation", "ready_to_commit", "reviewed", "committed", "committed",
        ]
        assert [row.domain_state for row in calls] == ["rejected"] + ["succeeded"] * 4
        assert all(row.transport_state == "completed" for row in calls)
        assert all(row.completed_at >= row.started_at for row in calls)
        assert calls[0].error_code == "invalid_patch"
        assert receipt["changeset_ref"] in calls[3].result_refs
        assert session.scalar(select(func.count(Item.id))) == 1
        trace = session.scalar(select(ConversationalToolTrace).where(
            ConversationalToolTrace.ref_id == trace_ref,
        ))
        assert trace.status == "interrupted"
        view = TraceViewService(session).snapshot(trace)
        assert view["counts"]["attempts"] == 4
        assert view["counts"]["authenticated_invocations"] == 5
        if missing_callback:
            assert all(row["transport_layer"] == "docket" for row in view["rows"])
            assert all(row["elapsed_ms"] is None for row in view["rows"])
            assert view["timing"]["wrapper_elapsed_sum_ms"] == 0
        if not missing_callback:
            assert [row["disposition"] for row in trace.calls] == [
                "rejected_validation", "ready_to_commit", "reviewed", "committed",
            ]
        version = trace.version
        outbox_count = session.scalar(select(func.count(OutboxEvent.id)))
    with session_factory.begin() as session:
        GatewayLifetimeService(session).expire_and_reconcile()
        assert session.scalar(select(ConversationalToolTrace.version)) == version
        assert session.scalar(select(func.count(OutboxEvent.id))) == outbox_count


@pytest.mark.integration
@pytest.mark.parametrize("late_mcp_finish", [False, True])
def test_domain_outcome_arriving_after_gateway_expiry_replaces_only_unknown(
    session_factory, late_mcp_finish,
):
    with session_factory.begin() as session:
        utterance, trace = _context(session)
        invocation, binding = _admit(session, utterance, trace, "stage", 1)
        call_id, utterance_id, trace_id = invocation.id, utterance.id, trace.id
        _expire(session, trace)
    with session_factory.begin() as session:
        GatewayLifetimeService(session).expire_and_reconcile()
        assert session.get(ToolInvocation, call_id).result_disposition == "unknown"
        version = session.get(ConversationalToolTrace, trace_id).version
        completed_at = session.get(ConversationalToolTrace, trace_id).completed_at
    with session_factory.begin() as session:
        utterance = session.get(OperatorUtterance, utterance_id)
        result = ChangeSetAssemblyService(session).stage(_stage(utterance), **binding)
    with session_factory.begin() as session:
        if late_mcp_finish:
            ProvenanceFastMCP._finish_invocation(
                session, call_id, status="succeeded", normalized_argument_hash="b" * 64,
                result_refs=[result["draft_ref"]], result_disposition=result["disposition"],
                error_code=None, semantic_request_ref=result["semantic_request_ref"],
            )
        else:
            GatewayLifetimeService(session).expire_and_reconcile()
    with session_factory.begin() as session:
        invocation = session.get(ToolInvocation, call_id)
        assert invocation.result_disposition == "ready_to_commit"
        assert invocation.domain_state == "succeeded"
        assert invocation.error_code is None
        trace = session.get(ConversationalToolTrace, trace_id)
        assert trace.status == "interrupted" and trace.completed_at == completed_at
        assert trace.version == version + 1
        assert TraceViewService(session).snapshot(trace)["rows"][0]["outcome"] == "ready_to_commit"
        # A late/repeated generic transport failure cannot erase the durable
        # outcome we just learned. Neither path may execute the staged Item.
        ProvenanceFastMCP._finish_invocation(
            session, call_id, status="failed", normalized_argument_hash=None,
            result_refs=[], result_disposition="failed", error_code="service_exception",
        )
        assert invocation.domain_state == "succeeded"
        assert session.scalar(select(func.count(Item.id))) == 0
        assert session.scalar(select(SemanticRequest)).authority_availability == "available"


@pytest.mark.integration
@pytest.mark.parametrize("field", [
    "tool_name", "received_argument_hash", "actor_ref", "utterance_refs", "trace_ref",
    "trace_call_id", "retry_ordinal", "retry_gateway", "retry_contract",
])
def test_recovery_cannot_borrow_nearby_operation_outcomes(session, field):
    utterance, trace = _context(session)
    invocation, binding = _admit(session, utterance, trace, "stage", 1)
    ChangeSetAssemblyService(session).stage(_stage(utterance), **binding)
    if field.startswith("retry_"):
        original = invocation
        invocation = ToolInvocation(**{name: getattr(original, name) for name in (
            "tool_name", "tool_contract_version", "tool_contract_hash", "caller_profile",
            "actor_ref", "utterance_refs", "received_argument_hash", "trace_ref",
            "trace_ordinal", "gateway_instance_ref",
        )})
        session.add(invocation)
        if field == "retry_ordinal":
            invocation.trace_ordinal = 9
        elif field == "retry_gateway":
            invocation.gateway_instance_ref = new_public_ref("gwy")
        else:
            invocation.tool_contract_hash = "f" * 64
    else:
        value = {
            "tool_name": "docket_commit_changeset", "received_argument_hash": "f" * 64,
            "actor_ref": "discord_user:other", "utterance_refs": [new_public_ref("utt")],
            "trace_ref": new_public_ref("trace"), "trace_call_id": "different-call",
        }[field]
        setattr(invocation, field, value)
    session.flush()
    assert recover_assembly_outcome(session, invocation) is False
    assert invocation.transport_state == "running" and invocation.domain_state == "unknown"


@pytest.mark.integration
def test_late_finalization_binds_original_attempt_not_latest_request_attempt(session):
    utterance, trace = _context(session)
    invocation, binding = _admit(session, utterance, trace, "stage", 1)
    result = ChangeSetAssemblyService(session).stage(_stage(utterance), **binding)
    request = session.scalar(select(SemanticRequest))
    original_attempt = session.scalar(select(SemanticRequestAttempt))
    other_attempt = SemanticRequestAttempt(
        semantic_request_id=request.id, semantic_request_ref=request.ref_id, attempt_number=2,
        authority_scope_hash=request.authority_scope_hash,
        precondition_hash=request.current_precondition_hash,
        execution_trace_ref=new_public_ref("trace"), state="pending",
    )
    session.add(other_attempt)
    session.flush()
    ProvenanceFastMCP._finish_invocation(
        session, invocation.id, status="succeeded", normalized_argument_hash="b" * 64,
        result_refs=[], result_disposition=result["disposition"], error_code=None,
        semantic_request_ref=request.ref_id,
    )
    assert original_attempt.tool_call_ref == invocation.ref_id
    assert other_attempt.tool_call_ref is None
    assert session.scalar(select(AssemblyOperation)).state == "completed"


@pytest.mark.integration
def test_postcommit_runtime_error_cannot_erase_exact_durable_outcome(session):
    utterance, trace = _context(session)
    _staged, binding = _admit(session, utterance, trace, "stage", 1)
    service = ChangeSetAssemblyService(session)
    service.stage(_stage(utterance), **binding)
    invocation, commit_binding = _admit(session, utterance, trace, "commit", 2)
    receipt = service.commit(
        utterance_ref=utterance.ref_id, request_key=utterance.request_key, **commit_binding,
    )
    session.flush()
    ProvenanceFastMCP._finish_invocation(
        session, invocation.id, status="failed", normalized_argument_hash=None,
        result_refs=[], result_disposition="failed", error_code="internal_error",
    )
    assert invocation.result_disposition == "committed" and invocation.domain_state == "succeeded"
    assert receipt["changeset_ref"] in invocation.result_refs
    assert invocation.error_code is None
    assert session.scalar(select(func.count(Item.id))) == 1


@pytest.mark.integration
def test_mcp_recovers_commit_receipt_when_result_assembly_raises_after_commit(session_factory):
    with session_factory.begin() as session:
        utterance, trace = _context(session)
        _staged, stage_binding = _admit(session, utterance, trace, "stage", 1)
        ChangeSetAssemblyService(session).stage(_stage(utterance), **stage_binding)
        _original, binding = _admit(session, utterance, trace, "commit", 2, sha256_json({}))
        arguments = {
            "utterance_ref": utterance.ref_id, "request_key": utterance.request_key, **binding,
        }
        now = int(datetime.now(UTC).timestamp())
        payload = {
            "format": 1, "trace_ref": trace.ref_id, "call_id": "call-2", "ordinal": 2,
            "utterance_ref": utterance.ref_id, "gateway_instance_ref": trace.gateway_instance_ref,
            "tool_name": "docket_commit_changeset", "argument_hash": sha256_json({}),
            "contract_version": CONTRACT_VERSION, "contract_hash": contract_hash("interactive"),
            "issued_at": now, "expires_at": now + 900,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signature = hmac.new(
            get_settings().hermes_to_docket_token().encode(),
            b"docket-mcp-invocation-v1:" + encoded.encode(), hashlib.sha256,
        ).hexdigest()
        arguments["invocation_binding"] = f"{encoded}.{signature}"
    server = ProvenanceFastMCP("recovery-test", caller_profile="interactive")

    @server.tool(name="docket_commit_changeset")
    def commit(
        utterance_ref: str, request_key: str,
        assembly_operation_token: str, assembly_argument_hash: str,
    ) -> dict:
        with session_factory.begin() as session:
            ChangeSetAssemblyService(session).commit(
                utterance_ref=utterance_ref, request_key=request_key,
                assembly_operation_token=assembly_operation_token,
                assembly_argument_hash=assembly_argument_hash,
            )
        raise RuntimeError("Synthetic response assembly failure after durable commit.")

    for _ in range(2):
        result = _result_envelope(asyncio.run(server.call_tool(
            "docket_commit_changeset", arguments,
        )))
        assert result["ok"] is True
        assert result["disposition"] == "committed"
        assert result["reconciled"] is True
    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        calls = list(session.scalars(select(ToolInvocation).where(
            ToolInvocation.trace_ordinal == 2, ToolInvocation.trace_call_id.is_(None),
        )))
        assert len(calls) == 2
        assert all(row.result_disposition == "committed" for row in calls)
        # No post-tool trace callback ran, but finalization still queues a
        # refresh for each new authenticated invocation's known outcome.
        trace = session.scalar(select(ConversationalToolTrace))
        assert trace.version == 3
        assert session.scalar(select(func.count(OutboxEvent.id)).where(
            OutboxEvent.event_type == "discord.mcp_trace.requested",
            OutboxEvent.aggregate_id == trace.id,
        )) == 2


@pytest.mark.integration
def test_closed_gateway_without_new_evidence_does_not_relock_unknown_calls(
    session_factory, monkeypatch,
):
    with session_factory.begin() as session:
        utterance, trace = _context(session)
        _invocation, _binding = _admit(session, utterance, trace, "stage", 1)
        _expire(session, trace)
    with session_factory.begin() as session:
        GatewayLifetimeService(session).expire_and_reconcile()
        assert session.scalar(select(ToolInvocation)).result_disposition == "unknown"

    def never_reconcile(*args, **kwargs):
        raise AssertionError("No new durable operation evidence warrants another row lock")

    monkeypatch.setattr(GatewayLifetimeService, "_reconcile_closed_gateway", never_reconcile)
    with session_factory.begin() as session:
        GatewayLifetimeService(session).expire_and_reconcile()
        assert session.scalar(select(ToolInvocation)).domain_state == "unknown"
