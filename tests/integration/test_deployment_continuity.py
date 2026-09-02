import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import AgentResponseCapture, OperatorUtteranceCapture
from docket.models import (
    ConversationalToolTrace,
    DeferredIngress,
    ExecutionLease,
    GatewayLifetime,
    IntentSession,
    OperatorUtterance,
    OutboxEvent,
    ToolInvocation,
)
from docket.providers.discord import FakeDiscordProjectionAdapter
from docket.services.continuity import ContinuityService
from docket.services.deferred_ingress import DeferredIngressRunner
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.ingress_ledger import IngressIdentity, IngressLedgerService
from docket.services.provenance import ProvenanceService
from docket.tool_contracts import CONTRACT_VERSION, contract_hash


@pytest.mark.integration
def test_drain_waits_only_for_prebarrier_execution_leases(session_factory) -> None:
    with session_factory.begin() as session:
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key="test:active-turn",
            lease_kind="interactive_turn",
            subject_ref=f"utt_{'1' * 26}",
        )
        completion_token = lease.completion_token

    with session_factory.begin() as session:
        barrier = ContinuityService(session).request_drain(
            requested_by="test",
            timeout_seconds=60,
        )
        drain_ref = str(barrier["ref"])

    with session_factory.begin() as session:
        status = ContinuityService(session).drain_status(drain_ref)
        assert status["drained"] is False
        assert status["active_lease_kinds"] == ["interactive_turn"]
        assert "active_lease_refs" not in status
        with pytest.raises(DocketError) as exc_info:
            ContinuityService(session).acquire_execution_lease(
                lease_key="test:new-turn",
                lease_kind="interactive_turn",
            )
        assert exc_info.value.code == "deployment_drain_active"

    with session_factory.begin() as session:
        service = ContinuityService(session)
        service.complete_execution_lease(completion_token)
        assert service.drain_status(drain_ref)["drained"] is True
        released = service.release_drain(drain_ref)
        assert released["state"] == "released"


@pytest.mark.integration
def test_drain_timeout_aborts_without_cancelling_active_work(session_factory) -> None:
    with session_factory.begin() as session:
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key="test:timeout-turn",
            lease_kind="interactive_turn",
        )
        drain = ContinuityService(session).request_drain(
            requested_by="test",
            timeout_seconds=30,
        )
        result = ContinuityService(session).release_drain(
            str(drain["ref"]),
            aborted=True,
        )
        assert result["state"] == "aborted"
        stored = session.scalar(
            select(ExecutionLease).where(ExecutionLease.completion_token == lease.completion_token)
        )
        assert stored is not None and stored.status == "active"


@pytest.mark.integration
def test_drained_deployment_fences_prior_gateway_during_replacement(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        prior = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )
        ContinuityService(session).request_drain(
            requested_by="test deployment",
            timeout_seconds=60,
        )
        replacement = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )

        assert replacement["replaced_gateway_ref"] == prior["ref"]
        assert replacement["lease_generation"] == 2
        lifetimes = list(
            session.scalars(select(GatewayLifetime).order_by(GatewayLifetime.lease_generation))
        )
        assert [lifetime.status for lifetime in lifetimes] == ["clean_shutdown", "active"]


@pytest.mark.integration
def test_drained_replacement_terminalizes_trace_without_replaying_finalized_utterance(
    session_factory,
) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    message_id = "1542799000000000421"
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        prior = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )
        capture = ProvenanceService(session).capture_operator_utterance(
            OperatorUtteranceCapture(
                request_id=uuid.uuid4(),
                guild_id=settings.discord_guild_id,
                channel_id=settings.chat_channel_id,
                message_id=message_id,
                actor_id=settings.operator_discord_user_id,
                verbatim_text="Track this deadline.",
                request_key=(
                    f"discord:{settings.discord_guild_id}:"
                    f"{settings.chat_channel_id}:{message_id}:0"
                ),
                gateway_instance_ref=str(prior["ref"]),
            )
        )
        ingress_binding = capture["deferred_ingress"]
        session.add(
            ConversationalToolTrace(
                ref_id=trace_ref,
                guild_id=settings.discord_guild_id,
                source_channel_id=settings.chat_channel_id,
                source_message_id=message_id,
                actor_id=settings.operator_discord_user_id,
                tool_contract_version=CONTRACT_VERSION,
                tool_contract_hash=contract_hash("interactive"),
                caller_profile="interactive",
                gateway_instance_ref=str(prior["ref"]),
                status="running",
                calls=[
                    {
                        "call_id": "locally-rejected-call",
                        "ordinal": 1,
                        "tool_name": "docket_commit_changeset",
                        "transport_state": "running",
                        "domain_state": "unknown",
                    }
                ],
                last_ordinal=1,
                version=1,
                started_at=now,
            )
        )
        session.flush()
        ProvenanceService(session).capture_agent_response(
            AgentResponseCapture(
                request_id=uuid.uuid4(),
                guild_id=settings.discord_guild_id,
                channel_id=settings.chat_channel_id,
                source_message_id=message_id,
                actor_id=settings.operator_discord_user_id,
                utterance_ref=str(capture["ref"]),
                turn_id="turn-finalized-before-drain",
                session_id="session-finalized-before-drain",
                model_identifier="test-model",
                verbatim_text="The request could not be committed.",
                generated_at=now,
                trace_ref=trace_ref,
                gateway_instance_ref=str(prior["ref"]),
            )
        )
        ContinuityService(session).complete_execution_lease(
            str(ingress_binding["execution_completion_token"])
        )
        ContinuityService(session).request_drain(
            requested_by="test deployment",
            timeout_seconds=60,
        )
        GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )

    with session_factory() as session:
        ingress = session.scalar(
            select(DeferredIngress).where(DeferredIngress.ref_id == ingress_binding["ref"])
        )
        trace = session.scalar(
            select(ConversationalToolTrace).where(
                ConversationalToolTrace.ref_id == trace_ref
            )
        )
        assert ingress is not None and ingress.status == "completed"
        assert ingress.claimed_by_gateway_ref is None
        assert ingress.claim_token is None
        assert trace is not None and trace.status == "interrupted"
        assert trace.calls[0]["transport_state"] == "timed_out"
        assert trace.calls[0]["disposition"] == "unknown"
        assert session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == "discord.mcp_trace.requested",
                OutboxEvent.aggregate_id == trace.id,
            )
        ) == 1

    assert DeferredIngressRunner(
        session_factory,
        FakeDiscordProjectionAdapter(),
    ).run_once() is False


@pytest.mark.integration
def test_startup_reconciles_trace_left_running_by_closed_gateway(
    session_factory,
) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        gateway = GatewayLifetime(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
            lease_generation=1,
            started_at=now - timedelta(minutes=5),
            heartbeat_at=now,
            lease_expires_at=now,
            clean_shutdown_at=now,
            status="clean_shutdown",
        )
        session.add(gateway)
        session.flush()
        session.add(
            ConversationalToolTrace(
                ref_id=trace_ref,
                guild_id=settings.discord_guild_id,
                source_channel_id=settings.chat_channel_id,
                source_message_id="1542799000000000422",
                actor_id=settings.operator_discord_user_id,
                tool_contract_version=CONTRACT_VERSION,
                tool_contract_hash=contract_hash("interactive"),
                caller_profile="interactive",
                gateway_instance_ref=gateway.ref_id,
                status="running",
                calls=[],
                last_ordinal=0,
                version=1,
                started_at=now - timedelta(minutes=4),
            )
        )

    with session_factory.begin() as session:
        assert GatewayLifetimeService(session).expire_and_reconcile() == []

    with session_factory() as session:
        trace = session.scalar(
            select(ConversationalToolTrace).where(
                ConversationalToolTrace.ref_id == trace_ref
            )
        )
        assert trace is not None and trace.status == "interrupted"


@pytest.mark.integration
def test_gateway_replacement_remains_fenced_while_prebarrier_work_is_active(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        prior = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )
        ContinuityService(session).acquire_execution_lease(
            lease_key="test:gateway-replacement-active",
            lease_kind="interactive_turn",
            gateway_instance_ref=str(prior["ref"]),
        )
        ContinuityService(session).request_drain(
            requested_by="test deployment",
            timeout_seconds=60,
        )

        with pytest.raises(DocketError) as exc_info:
            GatewayLifetimeService(session).register(
                registration_key=uuid.uuid4(),
                instance_kind="hermes_discord_gateway",
            )
        assert exc_info.value.code == "gateway_lifetime_already_active"


@pytest.mark.integration
def test_authenticated_message_is_captured_and_deferred_during_drain(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        gateway = GatewayLifetimeService(session).register(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
        )
        ContinuityService(session).request_drain(
            requested_by="test",
            timeout_seconds=60,
        )

    message_id = "1542799000000000999"
    capture = OperatorUtteranceCapture(
        request_id=uuid.uuid4(),
        guild_id=settings.discord_guild_id,
        channel_id=settings.chat_channel_id,
        message_id=message_id,
        actor_id=settings.operator_discord_user_id,
        verbatim_text="Record this during deploy.",
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
        gateway_instance_ref=str(gateway["ref"]),
    )
    with session_factory.begin() as session:
        result = ProvenanceService(session).capture_operator_utterance(capture)
        binding = result["deferred_ingress"]
        assert binding["state"] == "pending"
        assert binding["execution_completion_token"] is None
        row = session.scalar(
            select(DeferredIngress).where(DeferredIngress.ref_id == binding["ref"])
        )
        assert row is not None
        assert row.utterance_ref == result["ref"]
        assert row.status == "pending"
        assert row.drain_ref is not None


@pytest.mark.integration
def test_stable_ingress_captures_typed_message_without_domain_authority(
    session_factory,
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        captured = IngressLedgerService(
            session,
            identity=IngressIdentity(
                operator_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                chat_channel_id=settings.chat_channel_id,
                queue_channel_id=settings.queue_channel_id,
            ),
            signing_key=settings.read_secret(settings.interaction_signing_key_file).encode(),
            attachment_encryption_key=settings.attachment_encryption_key(),
            attachment_encryption_key_ref=settings.attachment_encryption_key_ref,
            attachment_max_bytes=settings.attachment_max_bytes,
            attachment_total_max_bytes=settings.attachment_total_max_bytes,
        ).capture_message(
            actor_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            parent_channel_id=None,
            message_id="1542799000000000410",
            reply_to_message_id=None,
            verbatim_text="Keep this message across deployment.",
            said_at=datetime.now(UTC),
        )
        assert captured["state"] == "pending"
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 1
        assert session.scalar(select(func.count(DeferredIngress.id))) == 1
        assert session.scalar(select(func.count(IntentSession.id))) == 0

    adapter = FakeDiscordProjectionAdapter()
    assert DeferredIngressRunner(session_factory, adapter).run_once() is True
    assert adapter.deferred_ingress[0]["utterance_ref"] == captured["utterance_ref"]


@pytest.mark.integration
def test_dead_gateway_preserves_terminal_domain_outcomes_and_marks_unknowns(
    session_factory,
) -> None:
    now = datetime.now(UTC)
    trace_ref = new_public_ref("trace")
    with session_factory.begin() as session:
        gateway = GatewayLifetime(
            registration_key=uuid.uuid4(),
            instance_kind="hermes_discord_gateway",
            lease_generation=1,
            started_at=now - timedelta(minutes=10),
            heartbeat_at=now - timedelta(minutes=5),
            lease_expires_at=now - timedelta(minutes=1),
            status="active",
        )
        session.add(gateway)
        session.flush()
        trace = ConversationalToolTrace(
            ref_id=trace_ref,
            guild_id=get_settings().discord_guild_id,
            source_channel_id=get_settings().chat_channel_id,
            source_message_id="1542799000000000411",
            actor_id=get_settings().operator_discord_user_id,
            tool_contract_version=CONTRACT_VERSION,
            tool_contract_hash=contract_hash("interactive"),
            caller_profile="interactive",
            gateway_instance_ref=gateway.ref_id,
            status="running",
            calls=[
                {
                    "call_id": "committed-call",
                    "ordinal": 1,
                    "tool_name": "docket_commit_changeset",
                    "transport_state": "running",
                    "domain_state": "unknown",
                },
                {
                    "call_id": "orphaned-call",
                    "ordinal": 2,
                    "tool_name": "docket_commit_changeset",
                    "transport_state": "running",
                    "domain_state": "unknown",
                },
            ],
            last_ordinal=2,
            version=1,
            started_at=now - timedelta(minutes=5),
        )
        session.add(trace)
        session.add_all(
            [
                ToolInvocation(
                    tool_name="docket_commit_changeset",
                    tool_contract_version=CONTRACT_VERSION,
                    tool_contract_hash=contract_hash("interactive"),
                    caller_profile="interactive",
                    received_argument_hash="a" * 64,
                    result_refs=[new_public_ref("chg")],
                    transport_state="completed",
                    domain_state="succeeded",
                    result_disposition="committed",
                    completed_at=now - timedelta(minutes=4),
                    trace_ref=trace_ref,
                    trace_call_id="committed-call",
                    trace_ordinal=1,
                    gateway_instance_ref=gateway.ref_id,
                ),
                ToolInvocation(
                    tool_name="docket_commit_changeset",
                    tool_contract_version=CONTRACT_VERSION,
                    tool_contract_hash=contract_hash("interactive"),
                    caller_profile="interactive",
                    received_argument_hash="b" * 64,
                    result_refs=[],
                    transport_state="running",
                    domain_state="unknown",
                    trace_ref=trace_ref,
                    trace_call_id="orphaned-call",
                    trace_ordinal=2,
                    gateway_instance_ref=gateway.ref_id,
                ),
            ]
        )

    with session_factory.begin() as session:
        assert GatewayLifetimeService(session).expire_and_reconcile()

    with session_factory() as session:
        trace = session.scalar(
            select(ConversationalToolTrace).where(ConversationalToolTrace.ref_id == trace_ref)
        )
        assert trace is not None and trace.status == "interrupted"
        assert trace.calls[0]["transport_state"] == "timed_out"
        assert trace.calls[0]["domain_state"] == "succeeded"
        assert trace.calls[0]["disposition"] == "committed"
        assert trace.calls[1]["transport_state"] == "timed_out"
        assert trace.calls[1]["domain_state"] == "unknown"
        assert trace.calls[1]["disposition"] == "unknown"
        orphaned = session.scalar(
            select(ToolInvocation).where(ToolInvocation.trace_call_id == "orphaned-call")
        )
        assert orphaned is not None
        assert orphaned.transport_state == "timed_out"
        assert orphaned.domain_state == "unknown"
        assert orphaned.result_disposition == "unknown"
