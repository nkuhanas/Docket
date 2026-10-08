from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import AuditEvent, AuthenticatedRequest, ConversationRecord, OperatorUtterance
from docket.services.agent_requests import AgentRequestService


def _principal(role="interactive"):
    return AgentPrincipal(
        principal_ref=f"agent:{role}",
        operator_ref=f"operator:{get_settings().operator_discord_user_id}",
        role=role,
        permissions=frozenset({"read", "stage", "commit", "resolve_conflict"}),
    )


@pytest.fixture
def interactive_context():
    token = agent_call_context.set(AgentCallContext(_principal()))
    try:
        yield
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
def test_request_is_durable_without_a_transcript(session_factory, interactive_context):
    with session_factory.begin() as session:
        ref = AgentRequestService(session).admit(request_key="synthetic-request-1").ref_id
    with session_factory.begin() as session:
        repeated = AgentRequestService(session).admit(request_key="synthetic-request-1")
        assert repeated.ref_id == ref
        assert repeated.state == "active"
        assert repeated.role == "interactive"
        assert session.scalar(select(func.count(AuthenticatedRequest.id))) == 1
        assert session.scalar(select(func.count(AuditEvent.id))) == 1
        assert session.scalar(select(func.count(OperatorUtterance.id))) == 0
        assert session.scalar(select(func.count(ConversationRecord.id))) == 0


@pytest.mark.integration
@pytest.mark.parametrize("role", ["triage", "read_only"])
def test_background_cannot_admit_even_with_claimed_permissions(session, role):
    token = agent_call_context.set(AgentCallContext(_principal(role)))
    try:
        with pytest.raises(DocketError) as error:
            AgentRequestService(session).admit(request_key="copied-interactive-request")
        assert error.value.code == "agent_authority_denied"
        assert session.scalar(select(func.count(AuthenticatedRequest.id))) == 0
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
def test_request_requires_server_authentication(session):
    token = agent_call_context.set(None)
    try:
        with pytest.raises(DocketError) as error:
            AgentRequestService(session).admit(request_key="untrusted")
        assert error.value.code == "agent_authentication_required"
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        {"principal_ref": "agent:another"},
        {"operator_ref": "operator:another"},
        {"enabled": False},
        {"audience": "another-service"},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
def test_request_cannot_be_used_by_wrong_or_revoked_principal(session, interactive_context, change):
    row = AgentRequestService(session).admit(request_key="owned-request")
    token = agent_call_context.set(AgentCallContext(replace(_principal(), **change)))
    try:
        with pytest.raises(DocketError):
            AgentRequestService(session).require(row.ref_id, permission="commit")
    finally:
        agent_call_context.reset(token)


@pytest.mark.integration
def test_optional_capture_can_arrive_after_commit_and_remains_reported(
    session_factory, interactive_context
):
    with session_factory.begin() as session:
        row = AgentRequestService(session).admit(request_key="finished-request")
        row.state = "committed"
        ref = row.ref_id
    with session_factory.begin() as session:
        service = AgentRequestService(session)
        capture = service.record(
            request_ref=ref,
            record_key="transcript-1",
            record_kind="operator_transcript",
            text="Synthetic reported wording",
        )
        replay = service.record(
            request_ref=ref,
            record_key="transcript-1",
            record_kind="operator_transcript",
            text="Synthetic reported wording",
        )
        assert capture["ref"] == replay["ref"]
        record = session.scalar(select(ConversationRecord))
        assert record.capture_method == "agent_reported"
        assert b"Synthetic reported wording" not in record.ciphertext
        assert service.require(ref, permission="stage", allow_committed=True).state == "committed"
        with pytest.raises(DocketError) as error:
            service.record(
                request_ref=ref,
                record_key="transcript-1",
                record_kind="operator_transcript",
                text="A correction",
            )
        assert error.value.code == "record_idempotency_mismatch"


@pytest.mark.integration
def test_optional_capture_failure_does_not_erase_request(session_factory, interactive_context):
    with session_factory.begin() as session:
        ref = AgentRequestService(session).admit(request_key="capture-failure").ref_id
    with pytest.raises(DocketError), session_factory.begin() as session:
        AgentRequestService(session).record(
            request_ref=ref,
            record_key="oversize",
            record_kind="operator_transcript",
            text="x" * 65537,
        )
    with session_factory.begin() as session:
        assert AgentRequestService(session).require(ref, permission="commit").state == "active"
        assert session.scalar(select(func.count(ConversationRecord.id))) == 0
