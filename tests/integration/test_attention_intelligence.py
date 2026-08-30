from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import (
    AttentionCase,
    AttentionCaseRevision,
    BriefEntry,
    CanonicalEvent,
    CaseItem,
    Entity,
    GmailSource,
    IdentityHandle,
    Item,
    Operation,
    OperatorProjection,
    OutboxEvent,
    ProjectionDelivery,
    ProviderAccount,
    Source,
    Task,
    TemporalBinding,
    TriageRun,
)
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.intelligence import CaseItemInput, TriageAnalysisInput
from docket.services.intelligence import IntelligenceService


def _settings(*, active: bool):
    settings = get_settings()
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    start = hour if active else (hour + 1) % 24
    end = (hour + 1) % 24 if active else hour
    return settings.model_copy(
        update={
            "gmail_ingestion_enabled": True,
            "gmail_claim_batch_size": 1,
            "gmail_triage_lease_seconds": 900,
            "waking_window_start_hour": start,
            "waking_window_end_hour": end,
        }
    )


def _stage(
    session_factory,
    provider: FakeGmailProvider,
    *,
    message_id: str,
    sender: str = "Isaac <isaac@example.com>",
    subject: str = "Source message",
    body_text: str = "Bounded untrusted evidence.",
) -> str:
    received_at = datetime.now(UTC)
    provider.add_message(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        source_version="1",
        sender=sender,
        subject=subject,
        body_text=body_text,
        received_at=received_at,
    )
    with session_factory.begin() as session:
        account = session.scalar(
            select(ProviderAccount).where(ProviderAccount.provider == "google")
        )
        if account is None:
            account = ProviderAccount(
                provider="google",
                external_account_id="attention-intelligence-test",
                capabilities=["gmail", "google_calendar"],
                enabled=True,
            )
            session.add(account)
            session.flush()
        provenance_source = Source(
            source_kind="gmail",
            external_ref=f"gmail:{message_id}:1",
            observed_at=received_at,
            content_hash=message_id.encode("utf-8").hex().ljust(64, "0")[:64],
            metadata_json={"account_ref": account.ref_id},
        )
        session.add(provenance_source)
        session.flush()
        source = GmailSource(
            ref_id=provenance_source.ref_id,
            account_id=account.id,
            provider="gmail",
            external_object_id=message_id,
            external_parent_id=f"thread-{message_id}",
            source_version="1",
            source_fingerprint=message_id.rjust(64, "0")[-64:],
            received_at=received_at,
            minimal_headers={
                "sender": sender,
                "subject": subject,
                "thread_id": f"thread-{message_id}",
                "message_id": message_id,
            },
            status="staged",
        )
        session.add(source)
        session.flush()
        return source.ref_id


def _request(
    context: dict,
    *,
    semantic_classes: list[str],
    case_items: list[CaseItemInput] | None = None,
    title: str = "Triage result",
    summary: str = "Bounded semantic summary.",
) -> TriageAnalysisInput:
    return TriageAnalysisInput(
        triage_run_ref=context["triage_run_ref"],
        context_ref=context["ref"],
        source_ref=context["source_ref"],
        claim_token=context["claim_token"],
        semantic_classes=semantic_classes,
        title=title,
        summary=summary,
        case_items=case_items or [],
        explanation="Deterministic test disposition.",
    )


def _assert_no_canonical_mutation(session) -> None:
    assert session.scalar(select(func.count(Entity.id))) == 0
    assert session.scalar(select(func.count(Item.id))) == 0
    assert session.scalar(select(func.count(Task.id))) == 0
    assert session.scalar(select(func.count(TemporalBinding.id))) == 0
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0


@pytest.mark.integration
def test_unknown_sender_meeting_admits_case_for_event_not_identity(session_factory) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    source_ref = _stage(
        session_factory,
        provider,
        message_id="unknown-meeting",
        subject="Meeting tomorrow",
    )
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    sender = context["trusted_context"]["sender_resolution"]
    assert context["source_ref"] == source_ref
    assert sender["state"] == "unbound"
    assert sender["identity_ref"].startswith("idn_")
    assert len(json.dumps(context, separators=(",", ":")).encode("utf-8")) <= 16384

    result = service.submit_analysis(
        _request(
            context,
            semantic_classes=["event_invitation", "registry_candidate"],
            title="Specific meeting tomorrow",
            case_items=[
                CaseItemInput(
                    item_key="event",
                    item_type="event_candidate",
                    resolution_role="required",
                    canonical_consequence_class="event_disposition",
                    payload={"when": "tomorrow"},
                ),
                CaseItemInput(
                    item_key="identity",
                    item_type="identity_resolution",
                    resolution_role="supporting",
                    payload={"identity_ref": sender["identity_ref"]},
                ),
            ],
        )
    )

    with session_factory() as session:
        case = session.scalar(select(AttentionCase))
        revision = session.scalar(select(AttentionCaseRevision))
        items = list(session.scalars(select(CaseItem).order_by(CaseItem.item_key)))
        assert case is not None and revision is not None
        assert result["ref"] == case.ref_id
        assert revision.admission_rule_ref == "triage.canonical_consequence.v1"
        assert revision.canonical_consequence_classes == ["event_disposition"]
        assert revision.required_case_item_refs == [
            next(item.ref_id for item in items if item.item_key == "event")
        ]
        identity = next(item for item in items if item.item_key == "identity")
        assert identity.resolution_role == "supporting"
        assert identity.canonical_consequence_class is None
        projection = session.scalar(select(OperatorProjection))
        delivery = session.scalar(select(ProjectionDelivery))
        assert projection is not None and projection.case_ref == case.ref_id
        assert delivery is not None and delivery.projection_ref == projection.ref_id
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1
        handle = session.scalar(select(IdentityHandle))
        assert handle is not None and handle.status == "unbound"
        _assert_no_canonical_mutation(session)


@pytest.mark.integration
def test_optional_outreach_is_brief_without_attention_demand(session_factory) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    _stage(session_factory, provider, message_id="optional-outreach")
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    result = service.submit_analysis(
        _request(
            context,
            semantic_classes=["relationship_context", "registry_candidate"],
            title="Optional Devpost outreach",
            summary="An unknown participant asked an optional question.",
            case_items=[
                CaseItemInput(
                    item_key="identity",
                    item_type="identity_resolution",
                    resolution_role="supporting",
                )
            ],
        )
    )
    with session_factory() as session:
        entry = session.scalar(select(BriefEntry))
        assert entry is not None and result["ref"] == entry.ref_id
        assert entry.disposition == "include"
        assert session.scalar(select(func.count(AttentionCase.id))) == 0
        assert session.scalar(select(func.count(CaseItem.id))) == 0
        assert session.scalar(select(func.count(OperatorProjection.id))) == 0
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        _assert_no_canonical_mutation(session)


@pytest.mark.integration
def test_identity_only_attention_submission_is_rejected_before_persistence(
    session_factory,
) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    _stage(session_factory, provider, message_id="identity-only")
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    request = _request(
        context,
        semantic_classes=["action_request", "registry_candidate"],
        case_items=[
            CaseItemInput(
                item_key="identity",
                item_type="identity_resolution",
                resolution_role="required",
            )
        ],
    )
    with pytest.raises(DocketError) as exc_info:
        service.submit_analysis(request)
    assert exc_info.value.code == "attention_case_canonical_consequence_required"

    with session_factory() as session:
        assert session.scalar(select(func.count(AttentionCase.id))) == 0
        assert session.scalar(select(func.count(CaseItem.id))) == 0
        run = session.scalar(select(TriageRun))
        source = session.scalar(select(GmailSource))
        assert run is not None and run.status == "running"
        assert source is not None and source.status == "claimed"
        _assert_no_canonical_mutation(session)


@pytest.mark.integration
def test_required_action_and_time_record_exact_consequence_classes(session_factory) -> None:
    settings = _settings(active=False)
    provider = FakeGmailProvider()
    _stage(session_factory, provider, message_id="financial-aid-response")
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    service.submit_analysis(
        _request(
            context,
            semantic_classes=["deadline_or_required_response", "registry_candidate"],
            case_items=[
                CaseItemInput(
                    item_key="response",
                    item_type="task_candidate",
                    resolution_role="required",
                    canonical_consequence_class="task_disposition",
                ),
                CaseItemInput(
                    item_key="deadline",
                    item_type="temporal_binding_candidate",
                    resolution_role="required",
                    canonical_consequence_class="temporal_disposition",
                ),
                CaseItemInput(
                    item_key="identity",
                    item_type="identity_resolution",
                    resolution_role="supporting",
                ),
            ],
        )
    )
    with session_factory() as session:
        revision = session.scalar(select(AttentionCaseRevision))
        assert revision is not None
        assert revision.canonical_consequence_classes == [
            "task_disposition",
            "temporal_disposition",
        ]
        assert len(revision.required_case_item_refs) == 2
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        _assert_no_canonical_mutation(session)


@pytest.mark.integration
def test_noise_is_suppressed_without_case_or_projection(session_factory) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    _stage(session_factory, provider, message_id="promotional-noise")
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    result = service.submit_analysis(_request(context, semantic_classes=["noise"]))
    with session_factory() as session:
        entry = session.scalar(select(BriefEntry))
        source = session.scalar(select(GmailSource))
        assert result["state"] == "suppress"
        assert entry is not None and entry.disposition == "suppress"
        assert source is not None and source.status == "ignored"
        assert session.scalar(select(func.count(AttentionCase.id))) == 0
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        _assert_no_canonical_mutation(session)
