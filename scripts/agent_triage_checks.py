"""Provider-neutral triage claims and inbox fanout against isolated PostgreSQL."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import func, select

from docket.agent_auth import AgentCallContext, AgentPrincipal, agent_call_context
from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import (
    BriefEntry,
    GmailSource,
    Operation,
    PluginNotification,
    ProviderAccount,
    Source,
)
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.intelligence import TriageAnalysisInput
from docket.services.intelligence import IntelligenceService
from docket.services.oauth import OAuthPrincipalService
from docket.services.triage_inbox import TriageInboxService


def test_agent_triage_callbacks_serialize_and_inbox_survives_connections(factory) -> None:
    settings = get_settings().model_copy(
        update={
            "plugin_enabled": True,
            "discord_adapter_enabled": False,
            "plugin_oauth_issuer": "https://identity.test/",
        }
    )
    provider = FakeGmailProvider()
    instant = datetime(1900, 1, 1, tzinfo=UTC)
    provider.add_message(
        message_id="neutral-triage-pg",
        thread_id="neutral-thread",
        source_version="1",
        sender="synthetic@example.com",
        subject="Synthetic triage source",
        body_text="Bounded untrusted data.",
        received_at=instant,
    )
    with factory.begin() as session:
        owner = OAuthPrincipalService(session, settings).approve("synthetic-triage", triage=True)
        account = ProviderAccount(
            provider="google",
            external_account_id="neutral-triage-pg",
            capabilities=["gmail"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        principal = AgentPrincipal(
            owner.ref_id,
            owner.operator_ref,
            "triage",
            frozenset({"read", "triage"}),
            integration="oauth",
            grant_epoch=owner.valid_after,
        )
        fingerprint = hashlib.sha256(b"neutral-triage-pg").hexdigest()
        evidence = Source(
            source_kind="gmail",
            external_ref="gmail:neutral-triage-pg:1",
            observed_at=instant,
            content_hash=fingerprint,
            metadata_json={"account_ref": account.ref_id},
        )
        session.add(evidence)
        session.flush()
        source_ref = evidence.ref_id
        session.add(
            GmailSource(
                ref_id=source_ref,
                account_id=account.id,
                provider="gmail",
                external_object_id="neutral-triage-pg",
                external_parent_id="neutral-thread",
                source_version="1",
                source_fingerprint=fingerprint,
                received_at=instant,
                minimal_headers={"sender": "synthetic@example.com", "subject": "Synthetic triage"},
                status="staged",
            )
        )
        before_operations = session.scalar(select(func.count()).select_from(Operation))
    inbox = TriageInboxService(factory, settings)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(inbox.run_due_once) for _ in range(2)]
        assert sum(job.result(timeout=15) for job in jobs) >= 1
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(PluginNotification)
                .where(
                    PluginNotification.subject_ref == source_ref,
                    PluginNotification.event_name == "docket.triage.ready",
                )
            )
            == 1
        )
    service = IntelligenceService(factory, provider, settings)
    token = agent_call_context.set(AgentCallContext(principal))
    try:
        context = service.get_triage_context()
    finally:
        agent_call_context.reset(token)
    assert context["source_ref"] == source_ref
    request = TriageAnalysisInput(
        triage_run_ref=context["triage_run_ref"],
        context_ref=context["ref"],
        source_ref=source_ref,
        claim_token=context["claim_token"],
        semantic_classes=["noise"],
        title="Synthetic noise",
        summary="Synthetic classification only.",
        explanation="No canonical consequence.",
    )
    barrier = threading.Barrier(2)

    def submit():
        binding = agent_call_context.set(AgentCallContext(principal))
        try:
            barrier.wait(timeout=10)
            try:
                assert service.submit_analysis(request)["ok"]
                return "compiled"
            except DocketError as exc:
                return exc.code
        finally:
            agent_call_context.reset(binding)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(submit) for _ in range(2)]
        outcomes = [job.result(timeout=15) for job in jobs]
    assert sorted(outcomes) == ["compiled", "triage_context_binding_invalid"]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(BriefEntry)
                .where(
                    BriefEntry.source_ref == source_ref,
                )
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(Operation)) == before_operations
