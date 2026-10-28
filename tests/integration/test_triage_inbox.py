import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_attention_intelligence import _assert_no_canonical_mutation, _request, _stage

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.domain.public_refs import new_public_ref
from docket.models import GmailSource, PluginNotification, TriageRun
from docket.models.base import utc_now
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.services.intelligence import IntelligenceService
from docket.services.triage_inbox import TriageInboxService


@contextmanager
def caller(principal):
    context = agent_call_context.set(AgentCallContext(principal))
    try:
        yield
    finally:
        agent_call_context.reset(context)


def principal(integration="oauth"):
    return AgentPrincipal(
        new_public_ref("prn"),
        get_settings().canonical_operator_ref,
        "triage",
        frozenset({"read", "triage"}),
        integration=integration,
    )


def test_ready_inbox_catches_backlog_in_bounded_pages_and_reannounces_expired_claim(
    session_factory,
):
    settings = get_settings().model_copy(update={"plugin_enabled": True})
    provider = FakeGmailProvider()
    refs = [
        _stage(session_factory, provider, message_id=f"backlog-{index}") for index in range(107)
    ]
    inbox = TriageInboxService(session_factory, settings)
    assert inbox.run_due_once() == 100
    assert inbox.run_due_once() == 7
    assert inbox.run_due_once() == 0
    with session_factory.begin() as session:
        source = session.scalar(select(GmailSource).where(GmailSource.ref_id == refs[0]))
        source.status, source.claim_token = "claimed", uuid.uuid4()
        source.claimed_until = utc_now() + timedelta(minutes=1)
    assert inbox.run_due_once() == 0
    with session_factory.begin() as session:
        source = session.scalar(select(GmailSource).where(GmailSource.ref_id == refs[0]))
        source.claimed_until = utc_now() - timedelta(seconds=1)
    assert inbox.run_due_once() == 1
    assert inbox.run_due_once() == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PluginNotification)) == 108
        _assert_no_canonical_mutation(session)


@pytest.mark.parametrize("integration", ["service", "oauth"])
def test_agent_triage_rejects_other_principals_and_old_context_with_new_claim(
    session_factory, integration,
):
    settings = get_settings().model_copy(update={"plugin_enabled": True})
    provider = FakeGmailProvider()
    ref = _stage(session_factory, provider, message_id="bound-claims")
    service = IntelligenceService(session_factory, provider, settings)
    first, other = principal(integration), principal(integration)
    with caller(first):
        old = service.get_triage_context()
    request = _request(old, semantic_classes=["noise"])
    with caller(other), pytest.raises(DocketError) as denied:
        service.submit_analysis(request)
    assert denied.value.code == "triage_claim_invalid"
    with session_factory.begin() as session:
        source = session.scalar(select(GmailSource).where(GmailSource.ref_id == ref))
        source.claimed_until = utc_now() - timedelta(seconds=1)
    with caller(first):
        current = service.get_triage_context()
        stale = request.model_copy(update={"claim_token": current["claim_token"]})
        with pytest.raises(DocketError) as denied:
            service.submit_analysis(stale)
        assert denied.value.code == "triage_claim_invalid"
        result = service.submit_analysis(_request(current, semantic_classes=["noise"]))
        assert result["ok"] is True
    with session_factory() as session:
        assert len(session.scalars(select(TriageRun)).all()) == 2
        assert all(
            run.stats_json.get("claim_token_sha256")
            for run in session.scalars(
                select(TriageRun),
            )
        )
        _assert_no_canonical_mutation(session)
