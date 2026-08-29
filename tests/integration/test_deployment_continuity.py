import uuid

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import DeferredIngress, ExecutionLease
from docket.services.continuity import ContinuityService
from docket.services.gateway_lifetimes import GatewayLifetimeService
from docket.services.provenance import ProvenanceService


@pytest.mark.integration
def test_drain_waits_only_for_prebarrier_execution_leases(session_factory) -> None:
    with session_factory.begin() as session:
        lease = ContinuityService(session).acquire_execution_lease(
            lease_key="test:active-turn",
            lease_kind="interactive_turn",
            subject_ref=f"utt_{'1' * 26}",
        )
        lease_ref = lease.ref_id

    with session_factory.begin() as session:
        barrier = ContinuityService(session).request_drain(
            requested_by="test",
            timeout_seconds=60,
        )
        drain_ref = str(barrier["ref"])

    with session_factory.begin() as session:
        status = ContinuityService(session).drain_status(drain_ref)
        assert status["drained"] is False
        assert status["active_lease_refs"] == [lease_ref]
        with pytest.raises(DocketError) as exc_info:
            ContinuityService(session).acquire_execution_lease(
                lease_key="test:new-turn",
                lease_kind="interactive_turn",
            )
        assert exc_info.value.code == "deployment_drain_active"

    with session_factory.begin() as session:
        service = ContinuityService(session)
        service.complete_execution_lease(lease_ref)
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
        stored = session.scalar(select(ExecutionLease).where(ExecutionLease.ref_id == lease.ref_id))
        assert stored is not None and stored.status == "active"


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
        assert binding["lease_ref"] is None
        row = session.scalar(
            select(DeferredIngress).where(DeferredIngress.ref_id == binding["ref"])
        )
        assert row is not None
        assert row.utterance_ref == result["ref"]
        assert row.status == "pending"
        assert row.drain_ref is not None
