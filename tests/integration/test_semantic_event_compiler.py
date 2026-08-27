import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.enums import IntentAuthority
from docket.internal_api.schemas import ApprovalResponse
from docket.models import (
    Account,
    ActionRevision,
    Approval,
    CalendarEventCache,
    CalendarLane,
    CalendarSyncState,
    CanonicalEvent,
    DiscordDailyThread,
    DiscordProjection,
    Entity,
    EntityResolution,
    EventObservation,
    Operation,
    ProviderEventBinding,
    QueueItem,
    SemanticCandidate,
    SourceItem,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.triage import SemanticCandidateInput, SubmitSemanticCandidatesInput
from docket.services.approvals import ApprovalService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.entities import EntityService
from docket.services.events import CanonicalEventService, SemanticCandidateCompiler
from docket.services.gmail_ingestion import GmailIngestionService
from docket.services.operations import OperationRunner
from docket.services.triage import TriageService


def _settings():
    return get_settings().model_copy(
        update={
            "gmail_ingestion_enabled": True,
            "gmail_claim_batch_size": 20,
            "gmail_triage_lease_seconds": 900,
        }
    )


@pytest.mark.integration
def test_same_title_different_time_create_does_not_rebind_existing_event(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="same-title-correlation-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        source = SourceItem(
            account_id=account.id,
            provider="gmail",
            external_object_id="general-meeting-new",
            source_version="1",
            source_fingerprint="a" * 64,
            status="classified",
        )
        existing = CanonicalEvent(
            canonical_key="existing-general-meeting",
            title="General meeting",
            status="active",
            event_spec={
                "title": "General meeting",
                "timing": {
                    "kind": "timed",
                    "start_local": "2026-09-10T10:00:00",
                    "end_local": "2026-09-10T11:00:00",
                },
                "recurrence_kind": "one_time",
                "system_tags": [],
                "operator_tags": [],
                "priority": "normal",
                "priority_basis": "default",
            },
            entity_refs=[],
            context_labels=[],
            authority="canonical",
        )
        session.add_all((source, existing))
        session.flush()
        candidate = SemanticCandidate(
            source_item_id=source.id,
            candidate_index=0,
            candidate_key="different-general-meeting",
            semantic_key="b" * 64,
            kind="event",
            mutation="create",
            title="General meeting",
            summary="A separate general meeting was scheduled.",
            fields={
                "event": {
                    "title": "General meeting",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-10T15:00:00",
                        "end_local": "2026-09-10T16:00:00",
                    },
                }
            },
            confidence=0.95,
            status="pending",
        )
        session.add(candidate)
        session.flush()

        assert CanonicalEventService(session).correlate(candidate) == []


@pytest.mark.integration
def test_complete_inferred_event_becomes_one_version_bound_proposal(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="calendar-notice",
        thread_id="calendar-thread",
        source_version="1",
        sender="Project Team <team@example.com>",
        subject="Project review confirmed",
        received_at=datetime(2026, 9, 10, 19, 0, tzinfo=UTC),
    )
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="semantic-calendar-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        EntityService(session).create(
            entity_class="organization",
            canonical_name="Design Club",
            attributes={"context": "club"},
            authority=IntentAuthority.EXPLICIT_USER,
        )
        organizations_calendar_id = "organizations@group.calendar.google.com"
        session.add(
            CalendarLane(
                account_id=account.id,
                lane="organizations",
                display_name="Docket · Organizations",
                color_hex="#0B8043",
                calendar_id=organizations_calendar_id,
                status="active",
            )
        )
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=organizations_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    triage = TriageService(session_factory, provider, _settings())
    claim = triage.claim_batch()
    source_id = str(claim["sources"][0]["source_id"])
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=source_id,
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="project-review",
                    kind="event",
                    calendar_relevance="recommended",
                    mutation="create",
                    title="Project review",
                    summary="A project review has been scheduled.",
                    event={
                        "title": "Project review",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-09-10T14:00:00",
                            "end_local": "2026-09-10T14:30:00",
                        },
                        "location": "Review Room",
                    },
                    entity_mentions=[
                        {
                            "entity_class": "organization",
                            "name": "Design Club",
                            "role": "organizer",
                        },
                        {
                            "entity_class": "person",
                            "name": "Project Team",
                            "role": "sender",
                        },
                        {
                            "entity_class": "location",
                            "name": "Review Room",
                            "role": "venue",
                        },
                    ],
                    context_labels=["club"],
                    confidence=0.96,
                )
            ],
        )
    )

    assert SemanticCandidateCompiler(session_factory, _settings()).run_due_once()
    with session_factory() as session:
        candidate = session.scalar(select(SemanticCandidate))
        canonical = session.scalar(select(CanonicalEvent))
        observation = session.scalar(select(EventObservation))
        queue_item = session.scalar(select(QueueItem))
        revision = session.scalar(select(ActionRevision))
        approval = session.scalar(select(Approval))
        source = session.get(SourceItem, candidate.source_item_id) if candidate else None
        assert candidate is not None and candidate.status == "proposed"
        assert canonical is not None and canonical.status == "proposed"
        assert canonical.calendar_lane == "organizations"
        assert observation is not None and observation.correlation_state == "new"
        assert observation.canonical_event_id == canonical.id
        assert source is not None
        assert queue_item is not None and queue_item.presentation == "proposal"
        assert queue_item.primary_source_item_id == source.id
        resolutions = candidate.fields["entity_resolutions"]
        optional = [
            resolution
            for resolution in resolutions
            if resolution["entity_class"] in {"person", "location"}
        ]
        assert len(optional) == 2
        assert all(resolution["required"] is False for resolution in optional)
        assert all(resolution["state"] == "unresolved" for resolution in optional)
        assert all(resolution["entity_id"] is None for resolution in optional)
        organization = next(
            resolution
            for resolution in resolutions
            if resolution["entity_class"] == "organization"
        )
        assert canonical.entity_refs == [
            {
                "entity_id": organization["entity_id"],
                "entity_class": "organization",
                "canonical_name": "Design Club",
                "role": "organizer",
                "resolution_id": organization["resolution_id"],
                "registration_disposition": "existing",
            }
        ]
        assert revision is not None and revision.authority == "inferred"
        assert revision.parameters["calendar_lane"] == "organizations"
        assert revision.parameters["canonical_event_id"] == str(canonical.id)
        assert revision.preview["source"] == {
            "relationship": "Inferred from email",
            "sender": "Project Team <team@example.com>",
            "subject": "Project review confirmed",
        }
        assert approval is not None and approval.status == "pending"

    settings = _settings()
    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and projection.message_id is not None
        assert thread is not None and thread.thread_id is not None
        rendered = repr(backend.messages[str(projection.id)]["embed"]).lower()
        assert "project review" in rendered
        assert "inferred from email" in rendered
        assert "confidence" not in rendered
        assert "candidate_type" not in rendered
        assert "correlation" not in rendered
        assert "gmail_message_id" not in rendered
        assert source_id not in rendered
        approval_control = next(
            control
            for control in backend.messages[str(projection.id)]["controls"]
            if control.get("decision") == "approve"
        )
        response = ApprovalResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id="inferred-event-approval",
            approval_id=uuid.UUID(approval_control["approval_id"]),
            approval_token=approval_control["token"],
            short_code=None,
            decision="approve",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=thread.thread_id,
            parent_channel_id=settings.queue_channel_id,
            projection_id=projection.id,
            message_id=projection.message_id,
            responded_at=datetime.now(UTC),
        )
    with session_factory.begin() as session:
        accepted = ApprovalService(session).respond(response)
        operation_id = uuid.UUID(accepted["operation_id"])
        assert accepted["approval_status"] == "consumed"
    assert OperationRunner(session_factory, FakeCalendarProvider()).run_due_once()
    with session_factory() as session:
        canonical = session.scalar(select(CanonicalEvent))
        binding = session.scalar(select(ProviderEventBinding))
        operation = session.get(Operation, operation_id)
        approvals = list(session.scalars(select(Approval)))
        assert canonical is not None and canonical.status == "active"
        assert binding is not None and binding.canonical_event_id == canonical.id
        assert operation is not None and operation.status == "succeeded"
        assert len(approvals) == 1 and approvals[0].status == "consumed"


@pytest.mark.integration
def test_inferred_event_integrates_calendar_conflicts_into_one_proposal(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="conflicting-invitation",
        thread_id="conflicting-invitation-thread",
        source_version="1",
        sender="Project Team <team@example.com>",
        subject="Project review invitation",
        received_at=datetime(2026, 9, 10, 19, 0, tzinfo=UTC),
    )
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="semantic-conflict-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        snapshot_generation = uuid.uuid4()
        sync = CalendarSyncState(
            account_id=account.id,
            calendar_id=get_settings().google_calendar_id,
            window_start=now - timedelta(days=30),
            window_end=now + timedelta(days=400),
            snapshot_generation=snapshot_generation,
            status="current",
            last_attempt_at=now,
            last_success_at=now,
        )
        session.add(sync)
        session.flush()
        session.add(
            CalendarEventCache(
                account_id=account.id,
                calendar_id=get_settings().google_calendar_id,
                provider_event_id="existing-workshop",
                snapshot_generation=snapshot_generation,
                status="confirmed",
                summary="Existing workshop",
                is_all_day=False,
                start_at=datetime(2026, 9, 10, 21, 15, tzinfo=UTC),
                end_at=datetime(2026, 9, 10, 21, 45, tzinfo=UTC),
                timezone="America/Los_Angeles",
                recurrence_kind="one_time",
                system_tags=["one_time", "timed", "external"],
                operator_tags=[],
                priority="normal",
                priority_basis="default",
                provider_reminders={},
                provider_etag='"workshop-v1"',
                synced_at=now,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    triage = TriageService(session_factory, provider, _settings())
    claim = triage.claim_batch()
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(claim["sources"][0]["source_id"]),
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="project-review-conflict",
                    kind="event",
                    calendar_relevance="recommended",
                    mutation="create",
                    title="Project review",
                    summary="A project review was scheduled.",
                    event={
                        "title": "Project review",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-09-10T14:00:00",
                            "end_local": "2026-09-10T14:30:00",
                        },
                    },
                    confidence=0.96,
                )
            ],
        )
    )

    assert SemanticCandidateCompiler(session_factory, _settings()).run_due_once()
    with session_factory() as session:
        queue_item = session.scalar(select(QueueItem))
        revision = session.scalar(select(ActionRevision))
        assert queue_item is not None and queue_item.presentation == "proposal"
        assert revision is not None and revision.authority == "inferred"
        assert [item["provider_event_id"] for item in revision.preview["conflicts"]] == [
            "existing-workshop"
        ]

    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        _settings(),
    )
    assert projection_runner.run_due_once()
    projected = next(iter(backend.messages.values()))
    fields = {field["name"]: field["value"] for field in projected["embed"]["fields"]}
    assert "Existing workshop" in fields["Conflicts"]
    assert any(control.get("field") == "conflict_resolution" for control in projected["controls"])


@pytest.mark.integration
def test_required_inferred_entity_registers_with_approved_event(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="unknown-organization",
        thread_id="unknown-organization-thread",
        source_version="1",
        subject="PolyUAS meeting",
    )
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="semantic-ascertainment-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        organizations_calendar_id = "organizations@group.calendar.google.com"
        session.add(
            CalendarLane(
                account_id=account.id,
                lane="organizations",
                display_name="Docket · Organizations",
                color_hex="#0B8043",
                calendar_id=organizations_calendar_id,
                status="active",
            )
        )
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=organizations_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    triage = TriageService(session_factory, provider, _settings())
    claim = triage.claim_batch()
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(claim["sources"][0]["source_id"]),
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="polyuas-meeting",
                    kind="event",
                    calendar_relevance="recommended",
                    mutation="create",
                    title="PolyUAS meeting",
                    summary="PolyUAS scheduled a meeting.",
                    event={
                        "title": "PolyUAS meeting",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-09-10T18:00:00",
                            "end_local": "2026-09-10T19:00:00",
                        },
                    },
                    entity_mentions=[
                        {
                            "entity_class": "organization",
                            "name": "PolyUAS",
                            "role": "organizer",
                            "required": True,
                        }
                    ],
                    confidence=0.95,
                )
            ],
        )
    )
    compiler = SemanticCandidateCompiler(session_factory, _settings())
    assert compiler.run_due_once()
    with session_factory() as session:
        candidate = session.scalar(select(SemanticCandidate))
        proposal = session.scalar(select(QueueItem))
        approval = session.scalar(select(Approval))
        entity = session.scalar(select(Entity))
        resolution = session.scalar(select(EntityResolution))
        revision = session.scalar(select(ActionRevision))
        assert candidate is not None and candidate.status == "proposed"
        assert proposal is not None and proposal.presentation == "proposal"
        assert proposal.status == "awaiting_approval"
        assert approval is not None and approval.status == "pending"
        assert entity is not None and entity.status == "provisional"
        assert resolution is not None and resolution.state == "provisional"
        assert revision is not None
        assert revision.preview["entity_refs"] == [
            {
                "entity_id": str(entity.id),
                "entity_class": "organization",
                "canonical_name": "PolyUAS",
                "role": "organizer",
                "resolution_id": str(resolution.id),
                "registration_disposition": "register_with_event",
            }
        ]

    settings = _settings()
    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and projection.message_id is not None
        assert thread is not None and thread.thread_id is not None
        card = backend.messages[str(projection.id)]
        fields = {field["name"]: field["value"] for field in card["embed"]["fields"]}
        assert fields["Also registers"] == (
            "Organizer: PolyUAS\nRegistered only if this event succeeds."
        )
        approval_control = next(
            control for control in card["controls"] if control.get("decision") == "approve"
        )
        response = ApprovalResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id="bundled-entity-approval",
            approval_id=uuid.UUID(approval_control["approval_id"]),
            approval_token=approval_control["token"],
            short_code=None,
            decision="approve",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=thread.thread_id,
            parent_channel_id=settings.queue_channel_id,
            projection_id=projection.id,
            message_id=projection.message_id,
            responded_at=datetime.now(UTC),
        )
    with session_factory.begin() as session:
        accepted = ApprovalService(session).respond(response)
        operation_id = uuid.UUID(accepted["operation_id"])
    assert OperationRunner(session_factory, FakeCalendarProvider()).run_due_once()
    with session_factory() as session:
        entity = session.scalar(select(Entity))
        resolution = session.scalar(select(EntityResolution))
        canonical = session.scalar(select(CanonicalEvent))
        operation = session.get(Operation, operation_id)
        assert entity is not None and entity.status == "active"
        assert entity.authority == "explicit_user"
        assert resolution is not None and resolution.state == "resolved"
        assert canonical is not None and canonical.status == "active"
        assert canonical.entity_refs[0]["registration_disposition"] == "existing"
        assert operation is not None and operation.status == "succeeded"


@pytest.mark.integration
def test_preference_excluded_event_stops_before_entity_registration(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="excluded-football-game",
        thread_id="excluded-football-thread",
        source_version="1",
        subject="Cal Poly vs. Idaho football game",
    )
    with session_factory.begin() as session:
        session.add(
            Account(
                provider="google",
                external_account_id="semantic-preference-exclusion-test",
                capabilities=["gmail"],
                enabled=True,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    triage = TriageService(session_factory, provider, _settings())
    claim = triage.claim_batch()
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(claim["sources"][0]["source_id"]),
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="football-game",
                    kind="event",
                    calendar_relevance="excluded",
                    relevance_basis="Operator does not want football games this semester.",
                    mutation="create",
                    title="Cal Poly vs. Idaho football game",
                    summary="A football game was advertised.",
                    event={
                        "title": "Cal Poly vs. Idaho football game",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-09-05T16:00:00",
                            "end_local": "2026-09-05T19:00:00",
                        },
                        "location": "Spanos Stadium",
                    },
                    entity_mentions=[
                        {
                            "entity_class": "organization",
                            "name": "Cal Poly Athletics",
                            "role": "organizer",
                            "required": True,
                        },
                        {
                            "entity_class": "location",
                            "name": "Spanos Stadium",
                            "role": "venue",
                            "required": True,
                        },
                    ],
                    confidence=0.98,
                )
            ],
        )
    )

    assert SemanticCandidateCompiler(session_factory, _settings()).run_due_once()
    with session_factory() as session:
        candidate = session.scalar(select(SemanticCandidate))
        resolutions = list(session.scalars(select(EntityResolution)))
        assert candidate is not None and candidate.status == "suppressed"
        assert candidate.resolution == {
            "disposition": "preference_excluded",
            "relevance_basis": "Operator does not want football games this semester.",
        }
        assert len(resolutions) == 2
        assert all(resolution.state == "unresolved" for resolution in resolutions)
        assert session.scalar(select(Entity)) is None
        assert session.scalar(select(QueueItem)) is None
        assert session.scalar(select(Approval)) is None


@pytest.mark.integration
def test_existing_provider_event_is_noop_then_cancellation_needs_no_replacement(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="provider-confirmation",
        thread_id="provider-event-thread",
        source_version="1",
        subject="Interview confirmed",
    )
    provider.add_message(
        message_id="provider-cancellation",
        thread_id="provider-event-thread",
        source_version="1",
        subject="Interview cancelled",
    )
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="provider-correlation-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=get_settings().google_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
        session.add(
            CalendarEventCache(
                account_id=account.id,
                calendar_id=get_settings().google_calendar_id,
                provider_event_id="google-interview-1",
                snapshot_generation=uuid.uuid4(),
                status="confirmed",
                summary="Acme interview",
                location="Video call",
                is_all_day=False,
                start_at=datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
                end_at=datetime(2026, 9, 10, 22, 0, tzinfo=UTC),
                timezone="America/Los_Angeles",
                recurrence_kind="one_time",
                provider_etag="etag-1",
                synced_at=now,
            )
        )
        account_id = account.id
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    triage = TriageService(session_factory, provider, _settings())
    claim = triage.claim_batch()
    sources = {source["external_object_id"]: source for source in claim["sources"]}
    token = str(claim["claim_token"])
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(sources["provider-confirmation"]["source_id"]),
            claim_token=token,
            candidates=[
                SemanticCandidateInput(
                    candidate_key="interview-confirmed",
                    kind="event",
                    calendar_relevance="required",
                    mutation="create",
                    title="Acme interview",
                    summary="The interview is confirmed.",
                    event={
                        "title": "Acme interview",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-09-10T14:00:00",
                            "end_local": "2026-09-10T15:00:00",
                        },
                        "location": "Video call",
                    },
                    correlation={"provider_event_id": "google-interview-1"},
                    confidence=0.99,
                )
            ],
        )
    )
    compiler = SemanticCandidateCompiler(session_factory, _settings())
    assert compiler.run_due_once()
    with session_factory() as session:
        confirmation = session.scalar(
            select(SemanticCandidate).where(
                SemanticCandidate.candidate_key == "interview-confirmed"
            )
        )
        assert confirmation is not None and confirmation.status == "resolved"
        assert confirmation.resolution["disposition"] == "calendar_already_matches"
        assert session.scalar(select(QueueItem)) is None

    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(sources["provider-cancellation"]["source_id"]),
            claim_token=token,
            candidates=[
                SemanticCandidateInput(
                    candidate_key="interview-cancelled",
                    kind="event",
                    calendar_relevance="required",
                    mutation="cancel",
                    title="Acme interview cancelled",
                    summary="The interview was cancelled.",
                    correlation={"provider_event_id": "google-interview-1"},
                    confidence=0.99,
                )
            ],
        )
    )
    assert compiler.run_due_once()
    with session_factory() as session:
        cancellation = session.scalar(
            select(SemanticCandidate).where(
                SemanticCandidate.candidate_key == "interview-cancelled"
            )
        )
        revision = session.scalar(select(ActionRevision))
        assert cancellation is not None and cancellation.status == "proposed"
        assert revision is not None and revision.action_type == "calendar_cancel_event"
        assert revision.account_id == account_id
        assert revision.parameters["external_event_id"] == "google-interview-1"
