import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.internal_api.schemas import ApprovalResponse, LocalActionResponse
from docket.models import (
    Account,
    CalendarSyncState,
    DailyBrief,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
    SemanticCandidate,
    TriageWindow,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.triage import SemanticCandidateInput, SubmitSemanticCandidatesInput
from docket.services.approvals import ApprovalService
from docket.services.briefs import DailyBriefService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.events import SemanticCandidateCompiler
from docket.services.gmail_ingestion import GmailIngestionService
from docket.services.local_actions import LocalActionService
from docket.services.proposal_controls import ProposalControlService
from docket.services.triage import TriageService


def _settings():
    return get_settings().model_copy(
        update={
            "gmail_ingestion_enabled": True,
            "gmail_claim_batch_size": 20,
            "gmail_triage_lease_seconds": 900,
            "waking_window_start_hour": 7,
            "waking_window_end_hour": 22,
        }
    )


@pytest.mark.integration
def test_overnight_event_is_silent_until_idempotent_morning_brief(
    session_factory,
) -> None:
    settings = _settings()
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="overnight-event",
        thread_id="overnight-thread",
        source_version="1",
        subject="Morning review",
        received_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
    )
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="daily-brief-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=settings.google_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, settings)
        .run_due_once(force=True)
        .completed
    )
    briefs = DailyBriefService(session_factory, settings)
    morning = datetime(2026, 8, 26, 14, 1, tzinfo=UTC)
    assert not briefs.run_due_once(morning)
    triage = TriageService(session_factory, provider, settings)
    claim = triage.claim_batch()
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(claim["sources"][0]["source_id"]),
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="morning-review",
                    kind="event",
                    mutation="create",
                    title="Morning review",
                    summary="A review is scheduled for this morning.",
                    event={
                        "title": "Morning review",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-08-27T09:00:00",
                            "end_local": "2026-08-27T09:30:00",
                        },
                    },
                    confidence=0.95,
                ),
                SemanticCandidateInput(
                    candidate_key="morning-clarification",
                    kind="event",
                    mutation="create",
                    title="Incomplete overnight event",
                    summary="The source does not establish when this occurs.",
                    missing_fields=["start", "end"],
                    confidence=0.7,
                ),
            ],
        )
    )
    compiler = SemanticCandidateCompiler(session_factory, settings)
    assert compiler.run_due_once()
    assert compiler.run_due_once()
    with session_factory() as session:
        window = session.scalar(select(TriageWindow))
        candidates = list(
            session.scalars(select(SemanticCandidate).order_by(SemanticCandidate.candidate_index))
        )
        assert window is not None and window.window_kind == "overnight"
        assert [candidate.status for candidate in candidates] == [
            "proposed",
            "needs_clarification",
        ]
        assert session.scalar(select(OutboxEvent)) is None

    assert briefs.run_due_once(morning)
    assert not briefs.run_due_once(morning)
    with session_factory() as session:
        brief = session.scalar(select(DailyBrief))
        queue_items = list(session.scalars(select(QueueItem).order_by(QueueItem.created_at)))
        outbox = list(session.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at)))
        assert brief is not None and brief.status == "published"
        assert len(queue_items) == 3
        assert queue_items[2].title == "Morning brief"
        assert "Calendar (2)" in queue_items[2].summary
        assert len(outbox) == 1
        assert outbox[0].aggregate_id == brief.queue_item_id

    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    brief_card = next(iter(backend.messages.values()))
    assert brief_card["embed"]["title"] == "Morning brief"
    assert brief_card["embed"]["fields"] == []
    assert [control["label"] for control in brief_card["controls"]] == ["Review decisions"]
    assert len(backend.messages) == 1

    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and projection.message_id is not None
        assert thread is not None and thread.thread_id is not None
        projection_id = projection.id
        message_id = projection.message_id
        thread_id = thread.thread_id
        review = brief_card["controls"][0]
    with session_factory.begin() as session:
        ProposalControlService(session).respond(
            LocalActionResponse(
                request_id=uuid.uuid4(),
                discord_interaction_id="morning-brief-review",
                discord_user_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                channel_id=thread_id,
                parent_channel_id=settings.queue_channel_id,
                projection_id=projection_id,
                message_id=message_id,
                responded_at=datetime.now(UTC),
                action_revision_id=uuid.UUID(review["action_revision_id"]),
                action_token=review["token"],
                transition="proposal_review_navigate",
                source_view="summary",
                target_view="brief_review",
                target_page=1,
            )
        )
    while projection_runner.run_due_once():
        pass
    decision_card = backend.messages[str(projection_id)]
    assert decision_card["embed"]["title"] == "Review new event"
    assert next(
        field["value"]
        for field in decision_card["embed"]["fields"]
        if field["name"] == "Morning brief review"
    ) == "Decision 1 of 2"
    assert {control["label"] for control in decision_card["controls"]} >= {
        "Approve",
        "Reject",
        "Edit details",
        "Snooze until tomorrow",
        "Back to brief",
        "Next",
    }
    assert {control.get("field") for control in decision_card["controls"]} >= {
        "priority",
        "reminder_preset",
    }
    priority_control = next(
        control for control in decision_card["controls"] if control.get("field") == "priority"
    )
    proposal_revision_id = next(
        control["action_revision_id"]
        for control in decision_card["controls"]
        if control.get("kind") == "proposal_action"
    )
    with session_factory.begin() as session:
        ProposalControlService(session).respond(
            LocalActionResponse(
                request_id=uuid.uuid4(),
                discord_interaction_id="morning-brief-priority",
                discord_user_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                channel_id=thread_id,
                parent_channel_id=settings.queue_channel_id,
                projection_id=projection_id,
                message_id=message_id,
                responded_at=datetime.now(UTC),
                action_revision_id=uuid.UUID(proposal_revision_id),
                action_token=priority_control["token"],
                transition="proposal_field_change",
                field="priority",
                value="high",
            )
        )
    while projection_runner.run_due_once():
        pass
    decision_card = backend.messages[str(projection_id)]
    assert "High priority" in next(
        field["value"]
        for field in decision_card["embed"]["fields"]
        if field["name"] == "Details"
    )
    approval_control = next(
        control for control in decision_card["controls"] if control.get("decision") == "approve"
    )
    with session_factory.begin() as session:
        ApprovalService(session).respond(
            ApprovalResponse(
                request_id=uuid.uuid4(),
                discord_interaction_id="morning-brief-approve",
                approval_id=uuid.UUID(approval_control["approval_id"]),
                approval_token=approval_control["token"],
                short_code=None,
                decision="approve",
                discord_user_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                channel_id=thread_id,
                parent_channel_id=settings.queue_channel_id,
                projection_id=projection_id,
                message_id=message_id,
                responded_at=datetime.now(UTC),
            )
        )
    while projection_runner.run_due_once():
        pass
    in_progress = backend.messages[str(projection_id)]
    next_control = next(
        control for control in in_progress["controls"] if control["label"] == "Next"
    )
    with session_factory.begin() as session:
        ProposalControlService(session).respond(
            LocalActionResponse(
                request_id=uuid.uuid4(),
                discord_interaction_id="morning-brief-next",
                discord_user_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                channel_id=thread_id,
                parent_channel_id=settings.queue_channel_id,
                projection_id=projection_id,
                message_id=message_id,
                responded_at=datetime.now(UTC),
                action_revision_id=uuid.UUID(next_control["action_revision_id"]),
                action_token=next_control["token"],
                transition="proposal_review_navigate",
                source_view="brief_review",
                source_page=1,
                target_view="brief_review",
                target_page=2,
            )
        )
    while projection_runner.run_due_once():
        pass
    clarification = backend.messages[str(projection_id)]
    assert clarification["embed"]["title"].startswith("Clarification needed")
    assert next(
        field["value"]
        for field in clarification["embed"]["fields"]
        if field["name"] == "Morning brief review"
    ) == "Decision 2 of 2"
    assert {control["label"] for control in clarification["controls"]} == {
        "Snooze until tomorrow",
        "Ignore",
        "Back to brief",
        "Previous",
    }
    ignore_control = next(
        control for control in clarification["controls"] if control["label"] == "Ignore"
    )
    with session_factory.begin() as session:
        LocalActionService(session).respond(
            LocalActionResponse(
                request_id=uuid.uuid4(),
                discord_interaction_id="morning-brief-ignore",
                discord_user_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                channel_id=thread_id,
                parent_channel_id=settings.queue_channel_id,
                projection_id=projection_id,
                message_id=message_id,
                responded_at=datetime.now(UTC),
                action_revision_id=uuid.UUID(ignore_control["action_revision_id"]),
                action_token=ignore_control["token"],
            )
        )
    while projection_runner.run_due_once():
        pass
    ignored = backend.messages[str(projection_id)]
    assert {control["label"] for control in ignored["controls"]} == {
        "Back to brief",
        "Previous",
    }
    assert len(backend.messages) == 1


@pytest.mark.integration
def test_night_brief_consolidates_daytime_action_and_awareness(
    session_factory,
) -> None:
    settings = _settings()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="night-brief-test",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=settings.google_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
    briefs = DailyBriefService(session_factory, settings)
    assert briefs.run_due_once(datetime(2026, 8, 26, 14, 1, tzinfo=UTC))

    provider = FakeGmailProvider()
    provider.add_message(
        message_id="daytime-summary",
        thread_id="daytime-thread",
        source_version="1",
        subject="Daytime items",
        received_at=datetime(2026, 8, 26, 19, 0, tzinfo=UTC),
    )
    assert (
        GmailIngestionService(session_factory, provider, settings)
        .run_due_once(force=True)
        .completed
    )
    triage = TriageService(session_factory, provider, settings)
    claim = triage.claim_batch()
    triage.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(claim["sources"][0]["source_id"]),
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="submit-form",
                    kind="task",
                    title="Submit department form",
                    summary="A department form needs a response.",
                    confidence=0.9,
                ),
                SemanticCandidateInput(
                    candidate_key="application-received",
                    kind="information",
                    title="Application received",
                    summary="The employer confirmed receipt.",
                    confidence=0.99,
                ),
            ],
        )
    )
    compiler = SemanticCandidateCompiler(session_factory, settings)
    assert compiler.run_due_once()
    assert compiler.run_due_once()

    # Simulate a restart after the 22:00 boundary. The missed closeout is still
    # due at 03:01 local time and must not be lost merely because the hour wrapped.
    night = datetime(2026, 8, 27, 10, 1, tzinfo=UTC)
    assert briefs.run_due_once(night)
    assert not briefs.run_due_once(night)
    with session_factory() as session:
        night_brief = session.scalar(select(DailyBrief).where(DailyBrief.brief_kind == "night"))
        assert night_brief is not None
        queue_item = session.get(QueueItem, night_brief.queue_item_id)
        assert queue_item is not None
        assert "Still needs you (1)" in queue_item.summary
        assert "Submit department form" in queue_item.summary
        assert "Awareness (1)" in queue_item.summary
        assert "Application received" in queue_item.summary

    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    while projection_runner.run_due_once():
        pass
    action_card = next(
        message
        for message in backend.messages.values()
        if message["embed"]["title"] == "Submit department form"
    )
    assert action_card["embed"]["fields"] == []
    assert {control["label"] for control in action_card["controls"]} == {
        "Snooze until tomorrow",
        "Acknowledge",
    }
    assert not any(
        message["embed"]["title"] == "Application received" for message in backend.messages.values()
    )
