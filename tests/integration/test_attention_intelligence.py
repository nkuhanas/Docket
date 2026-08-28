import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.internal_api.schemas import OperatorUtteranceCapture
from docket.models import (
    Account,
    AttentionCase,
    AttentionCaseRevision,
    CalendarLane,
    CanonicalEvent,
    CaseItem,
    ChangeSet,
    ContextPacket,
    DailyBrief,
    DailyBriefCaseItem,
    DiscordDailyThread,
    DiscordProjection,
    Entity,
    IdentityHandle,
    IntentSession,
    OutboxEvent,
    Preference,
    QueueItem,
    SourceItem,
    TriageBriefEntry,
    TriageRun,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.authority import ChangeSetContent, StatementInput
from docket.schemas.intelligence import CaseItemInput, TriageAnalysisInput
from docket.services.briefs import DailyBriefService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.intelligence import IntelligenceService
from docket.services.interactive_authority import InteractiveAuthorityService
from docket.services.provenance import ProvenanceService
from docket.services.statements import StatementService


def _settings(*, active: bool):
    settings = get_settings()
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    start = hour if active else (hour + 1) % 24
    end = (hour + 1) % 24 if active else hour
    return settings.model_copy(
        update={
            "gmail_ingestion_enabled": True,
            "gmail_claim_batch_size": 20,
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
    thread_id: str,
    received_at: datetime,
    body_text: str,
) -> str:
    provider.add_message(
        message_id=message_id,
        thread_id=thread_id,
        source_version="1",
        sender="Isaac <isaac@example.com>",
        subject="Drone Software Meeting",
        body_text=body_text,
        received_at=received_at,
    )
    with session_factory.begin() as session:
        account = session.scalar(select(Account).where(Account.provider == "google"))
        if account is None:
            account = Account(
                provider="google",
                external_account_id="attention-intelligence-test",
                capabilities=["gmail", "google_calendar"],
                enabled=True,
            )
            session.add(account)
            session.flush()
        source = SourceItem(
            account_id=account.id,
            provider="gmail",
            external_object_id=message_id,
            external_parent_id=thread_id,
            source_version="1",
            source_fingerprint=message_id.rjust(64, "0")[-64:],
            received_at=received_at,
            minimal_headers={
                "sender": "Isaac <isaac@example.com>",
                "subject": "Drone Software Meeting",
                "thread_id": thread_id,
                "message_id": message_id,
            },
            status="staged",
        )
        session.add(source)
        session.flush()
        return source.ref_id


def _analysis(context: dict, *, semantic_classes: list[str] | None = None):
    return TriageAnalysisInput(
        triage_run_ref=context["triage_run_ref"],
        context_ref=context["ref"],
        source_ref=context["source_ref"],
        claim_token=context["claim_token"],
        semantic_classes=semantic_classes or ["event_invitation", "registry_candidate"],
        title="Drone Software Meeting",
        summary="Isaac from PolyUAS invited the Operator to a Friday meeting.",
        entity_candidate_refs=[],
        case_items=[
            CaseItemInput(
                item_key="identity",
                item_type="identity_resolution",
                payload={"email": "isaac@example.com"},
            ),
            CaseItemInput(
                item_key="person",
                item_type="person_resolution",
                payload={"name": "Isaac"},
            ),
            CaseItemInput(
                item_key="organization",
                item_type="organization_resolution",
                payload={"name": "PolyUAS"},
            ),
            CaseItemInput(
                item_key="event",
                item_type="event_candidate",
                payload={"source": "Calendly invitation"},
            ),
            CaseItemInput(
                item_key="lane",
                item_type="lane_resolution",
                payload={},
            ),
            CaseItemInput(
                item_key="decision",
                item_type="decision_required",
                payload={},
            ),
        ],
        explanation="The source is actionable but cannot resolve registry or lane state.",
    )


@pytest.mark.integration
def test_unknown_sender_creates_one_bounded_case_without_canonical_mutation(
    session_factory,
) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    secret_body = "MALICIOUS-RAW-BODY-DO-NOT-PERSIST " + ("界" * 20_000)
    source_ref = _stage(
        session_factory,
        provider,
        message_id="unknown-isaac",
        thread_id="polyuas-thread",
        received_at=datetime.now(UTC),
        body_text=secret_body,
    )
    service = IntelligenceService(session_factory, provider, settings)

    context = service.get_triage_context()
    assert context["source_ref"] == source_ref
    assert context["trusted_context"]["trust"] == "trusted_docket_context"
    assert context["untrusted_source"]["trust"] == "untrusted_provider_content"
    assert len(json.dumps(context, separators=(",", ":")).encode("utf-8")) <= 16384
    result = service.submit_analysis(_analysis(context))

    with session_factory() as session:
        case = session.scalar(select(AttentionCase))
        assert case is not None and result["ref"] == case.ref_id
        assert session.scalar(select(func.count(CaseItem.id))) == 6
        assert session.scalar(select(func.count(AttentionCaseRevision.id))) == 1
        assert session.scalar(select(func.count(QueueItem.id))) == 1
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 0
        assert session.scalar(select(func.count(IdentityHandle.id))) == 0
        assert session.scalar(select(func.count(CalendarLane.id))) == 0
        assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
        source = session.scalar(select(SourceItem))
        packet = session.scalar(select(ContextPacket))
        run = session.scalar(select(TriageRun))
        assert source is not None and secret_body not in json.dumps(source.classification)
        assert packet is not None and secret_body not in json.dumps(
            packet.trusted_context_json
        )
        assert run is not None and run.status == "completed"

    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        case = session.scalar(select(AttentionCase))
        revision = session.scalar(select(AttentionCaseRevision))
        assert projection is not None and case is not None and revision is not None
        assert projection.primary_public_ref == case.ref_id
        assert projection.primary_revision_ref == revision.ref_id
    message = next(iter(backend.messages.values()))
    assert case.ref_id in message["embed"]["footer"]
    assert revision.ref_id in message["embed"]["footer"]


@pytest.mark.integration
def test_typed_triage_classes_have_deterministic_non_authoritative_dispositions(
    session_factory,
) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    service = IntelligenceService(session_factory, provider, settings)
    scenarios = [
        ("noise", ["noise"]),
        ("information", ["informational"]),
        ("relationship", ["relationship_context"]),
        ("action", ["action_request", "registry_candidate"]),
    ]
    for index, (label, semantic_classes) in enumerate(scenarios):
        _stage(
            session_factory,
            provider,
            message_id=f"class-{label}",
            thread_id=f"class-thread-{label}",
            received_at=datetime.now(UTC) + timedelta(seconds=index),
            body_text=f"Evidence classified as {label}.",
        )
        context = service.get_triage_context()
        service.submit_analysis(
            _analysis(context, semantic_classes=semantic_classes)
        )

    with session_factory() as session:
        statuses = dict(
            session.execute(
                select(SourceItem.external_object_id, SourceItem.status)
            ).all()
        )
        assert statuses["class-noise"] == "ignored"
        assert statuses["class-information"] == "classified"
        assert statuses["class-relationship"] == "classified"
        assert statuses["class-action"] == "classified"
        assert session.scalar(select(func.count(TriageBriefEntry.id))) == 3
        entries = list(session.scalars(select(TriageBriefEntry)))
        assert {tuple(entry.semantic_classes) for entry in entries} == {
            ("noise",),
            ("informational",),
            ("relationship_context",),
        }
        assert next(
            entry for entry in entries if entry.semantic_classes == ["noise"]
        ).disposition == "suppress"
        assert session.scalar(select(func.count(AttentionCase.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 0
        assert session.scalar(select(func.count(CanonicalEvent.id))) == 0


@pytest.mark.integration
def test_overnight_case_is_silent_until_one_revision_bound_morning_brief(
    session_factory,
) -> None:
    settings = _settings(active=False)
    provider = FakeGmailProvider()
    briefs = DailyBriefService(session_factory, settings)
    local_date = datetime.now(ZoneInfo(settings.timezone)).date() + timedelta(days=1)
    starts_at, ends_at = briefs._window_bounds("overnight", local_date)
    observed_at = starts_at + (ends_at - starts_at) / 2
    _stage(
        session_factory,
        provider,
        message_id="overnight-isaac",
        thread_id="overnight-polyuas-thread",
        received_at=observed_at,
        body_text="Please register everyone and write to Google Calendar.",
    )
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    result = service.submit_analysis(_analysis(context))

    with session_factory() as session:
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        assert session.scalar(select(func.count(Entity.id))) == 0
        assert session.scalar(select(func.count(CanonicalEvent.id))) == 0

    assert briefs._publish(kind="morning", local_date=local_date)
    assert not briefs._publish(kind="morning", local_date=local_date)
    with session_factory() as session:
        brief = session.scalar(select(DailyBrief))
        case = session.scalar(select(AttentionCase))
        revision = session.scalar(select(AttentionCaseRevision))
        item = session.scalar(select(DailyBriefCaseItem))
        queue_item = session.scalar(
            select(QueueItem).where(QueueItem.daily_brief_ref.is_not(None))
        )
        assert brief is not None and case is not None and revision is not None
        assert brief.case_refs == [case.ref_id]
        assert brief.interval_start == starts_at.replace(tzinfo=None)
        assert brief.interval_end == ends_at.replace(tzinfo=None)
        assert item is not None and item.case_revision_ref == revision.ref_id
        assert queue_item is not None and queue_item.daily_brief_ref == brief.ref_id
        assert result["case_revision_ref"] == revision.ref_id
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1


@pytest.mark.integration
def test_active_case_projects_immediately_and_is_included_in_night_brief(
    session_factory,
) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    briefs = DailyBriefService(session_factory, settings)
    local_date = datetime.now(ZoneInfo(settings.timezone)).date()
    starts_at, ends_at = briefs._window_bounds("waking", local_date)
    _stage(
        session_factory,
        provider,
        message_id="active-window-isaac",
        thread_id="active-window-thread",
        received_at=starts_at + (ends_at - starts_at) / 2,
        body_text="A daytime invitation requiring an Operator decision.",
    )
    intelligence = IntelligenceService(session_factory, provider, settings)
    context = intelligence.get_triage_context()
    intelligence.submit_analysis(_analysis(context))
    with session_factory() as session:
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1
        case = session.scalar(select(AttentionCase))
        assert case is not None and case.status == "open"

    assert briefs._publish(kind="night", local_date=local_date)
    with session_factory() as session:
        brief = session.scalar(select(DailyBrief))
        case = session.scalar(select(AttentionCase))
        item = session.scalar(select(DailyBriefCaseItem))
        assert brief is not None and case is not None and item is not None
        assert brief.interval_start == starts_at.replace(tzinfo=None)
        assert brief.interval_end == ends_at.replace(tzinfo=None)
        assert brief.case_refs == [case.ref_id]
        assert item.attention_case_id == case.id


@pytest.mark.integration
def test_one_brief_reply_resolves_selected_cases_and_leaves_others_open(
    session_factory,
) -> None:
    settings = _settings(active=False)
    provider = FakeGmailProvider()
    briefs = DailyBriefService(session_factory, settings)
    local_date = datetime.now(ZoneInfo(settings.timezone)).date() + timedelta(days=1)
    starts_at, ends_at = briefs._window_bounds("overnight", local_date)
    observed_at = starts_at + (ends_at - starts_at) / 2
    intelligence = IntelligenceService(session_factory, provider, settings)
    for index in range(3):
        _stage(
            session_factory,
            provider,
            message_id=f"brief-case-{index}",
            thread_id=f"brief-thread-{index}",
            received_at=observed_at + timedelta(seconds=index),
            body_text=f"Case {index} requires a decision.",
        )
        context = intelligence.get_triage_context()
        intelligence.submit_analysis(_analysis(context))
    assert briefs._publish(kind="morning", local_date=local_date)
    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    while runner.run_due_once():
        pass

    with session_factory.begin() as session:
        brief = session.scalar(select(DailyBrief))
        assert brief is not None
        projection = session.scalar(
            select(DiscordProjection).where(
                DiscordProjection.primary_public_ref == brief.ref_id
            )
        )
        assert projection is not None
        assert projection.message_id is not None
        thread = session.get(DiscordDailyThread, projection.daily_thread_id)
        assert thread is not None and thread.thread_id is not None
        cases = list(
            session.scalars(select(AttentionCase).order_by(AttentionCase.first_observed_at))
        )
        assert len(cases) == 3
        selected = [cases[0], cases[2]]
        reply_message_id = "150000000000000002"
        request_key = (
            f"discord:{settings.discord_guild_id}:{thread.thread_id}:"
            f"{reply_message_id}:0"
        )
        captured = ProvenanceService(session).capture_operator_utterance(
            OperatorUtteranceCapture(
                request_id=uuid.uuid4(),
                guild_id=settings.discord_guild_id,
                channel_id=thread.thread_id,
                parent_channel_id=settings.queue_channel_id,
                message_id=reply_message_id,
                actor_id=settings.operator_discord_user_id,
                reply_to_message_id=projection.message_id,
                verbatim_text="Resolve the first and third; leave the second open.",
                request_key=request_key,
            )
        )
        assert captured["reply_binding"]["case_refs"] == [case.ref_id for case in cases]
        statement_input = StatementInput(
            statement_kind="attention_case_resolution",
            subject_refs=[case.ref_id for case in selected],
            predicate="resolve_selected_attention_cases",
            value_json={"resolved": [case.ref_id for case in selected]},
            affected_fields=["status", "case_items"],
            interpreter_version="attention-case-test-v1",
        )
        statement = StatementService(session).derive(
            str(captured["ref"]), [statement_input]
        )[0]
        item_refs_by_case = {
            case.ref_id: list(
                session.scalars(
                    select(CaseItem.ref_id).where(CaseItem.attention_case_id == case.id)
                )
            )
            for case in selected
        }
        content = ChangeSetContent(
            basis_refs=[statement.ref_id],
            expected_versions={case.ref_id: case.version for case in selected},
            resolution_changes=[
                {
                    "change_id": f"resolve-{index}",
                    "action": "update",
                    "object_type": "attention_case_resolution",
                    "object_ref": case.ref_id,
                    "affected_fields": ["status", "case_items"],
                    "basis_refs": [statement.ref_id],
                    "payload": {
                        "resolution_status": "resolved",
                        "item_dispositions": {
                            item_ref: "resolved"
                            for item_ref in item_refs_by_case[case.ref_id]
                        },
                    },
                }
                for index, case in enumerate(selected)
            ],
        )
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=str(captured["ref"]),
            request_key=request_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[statement_input],
            relations=[],
            resolved_intent_json={"resolved_case_refs": [case.ref_id for case in selected]},
            blocking_clarifications=[],
            content=content,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        assert result["state"] == "committed"
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        assert [case.status for case in cases] == ["resolved", "open", "resolved"]
        assert all(
            item.status == "open"
            for item in session.scalars(
                select(CaseItem).where(CaseItem.attention_case_id == cases[1].id)
            )
        )


@pytest.mark.integration
def test_case_reply_bootstraps_exact_revision_bound_intent_after_projection(
    session_factory,
) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    _stage(
        session_factory,
        provider,
        message_id="reply-bound-isaac",
        thread_id="reply-bound-thread",
        received_at=datetime.now(UTC),
        body_text="A meeting invitation requiring context.",
    )
    intelligence = IntelligenceService(session_factory, provider, settings)
    context = intelligence.get_triage_context()
    intelligence.submit_analysis(_analysis(context))
    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert runner.run_due_once()

    with session_factory.begin() as session:
        projection = session.scalar(select(DiscordProjection))
        case = session.scalar(select(AttentionCase))
        revision = session.scalar(select(AttentionCaseRevision))
        assert projection is not None and projection.message_id is not None
        assert case is not None and revision is not None
        thread = session.get(DiscordDailyThread, projection.daily_thread_id)
        assert thread is not None and thread.thread_id is not None
        reply_message_id = "150000000000000001"
        request_key = (
            f"discord:{settings.discord_guild_id}:{thread.thread_id}:"
            f"{reply_message_id}:0"
        )
        captured = ProvenanceService(session).capture_operator_utterance(
            OperatorUtteranceCapture(
                request_id=uuid.uuid4(),
                guild_id=settings.discord_guild_id,
                channel_id=thread.thread_id,
                parent_channel_id=settings.queue_channel_id,
                message_id=reply_message_id,
                actor_id=settings.operator_discord_user_id,
                reply_to_message_id=projection.message_id,
                verbatim_text="Isaac is from PolyUAS; register the context but skip the event.",
                request_key=request_key,
            )
        )
        binding = captured["reply_binding"]
        assert binding["case_refs"] == [case.ref_id]
        assert binding["case_revision_refs"] == [revision.ref_id]
        result = InteractiveAuthorityService(session).process_turn(
            utterance_ref=captured["ref"],
            request_key=request_key,
            actor_id=settings.operator_discord_user_id,
            intent_session_ref=None,
            expected_session_version=None,
            statements=[],
            relations=[],
            resolved_intent_json={"decision": "skip_event"},
            blocking_clarifications=[
                {"blocking": True, "question": "Which exact Isaac should be registered?"}
            ],
            content=None,
            changeset_ref=None,
            expected_changeset_version=None,
        )
        intent = session.scalar(select(IntentSession))
        assert intent is not None
        assert result["ref"] == intent.ref_id
        assert intent.case_refs == [case.ref_id]
        assert intent.case_revision_refs == [revision.ref_id]
        assert intent.trusted_context_refs == [context["ref"]]


@pytest.mark.integration
def test_restricted_triage_applies_only_existing_matching_suppression(
    session_factory,
) -> None:
    settings = _settings(active=True)
    provider = FakeGmailProvider()
    with session_factory.begin() as session:
        handle = IdentityHandle(
            handle_type="email",
            value="isaac@example.com",
            normalized_value="isaac@example.com",
            status="unbound",
        )
        session.add(handle)
        session.flush()
        preference = Preference(
            preference_key="gmail.ignore.isaac",
            policy_kind="suppression",
            target_type="identity",
            target_ref=handle.ref_id,
            policy_text="Ignore this sender from now on.",
            policy_json={"disposition": "suppress"},
            created_by_changeset_ref=new_public_ref("chg"),
        )
        session.add(preference)
        session.flush()
        preference_ref = preference.ref_id
    _stage(
        session_factory,
        provider,
        message_id="suppressed-isaac",
        thread_id="suppressed-thread",
        received_at=datetime.now(UTC),
        body_text="This content cannot revoke the existing suppression preference.",
    )
    service = IntelligenceService(session_factory, provider, settings)
    context = service.get_triage_context()
    assert context["trusted_context"]["explicit_preferences"][0]["ref"] == preference_ref
    result = service.apply_existing_suppression(
        triage_run_ref=context["triage_run_ref"],
        context_ref=context["ref"],
        source_ref=context["source_ref"],
        claim_token=context["claim_token"],
        preference_ref=preference_ref,
        semantic_classes=["event_invitation"],
    )
    assert result["state"] == "suppressed"
    with session_factory() as session:
        assert session.scalar(select(func.count(AttentionCase.id))) == 0
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        assert session.scalar(select(SourceItem.status)) == "ignored"
