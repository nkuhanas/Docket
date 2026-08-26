from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    Account,
    CalendarSyncState,
    DailyBrief,
    OutboxEvent,
    QueueItem,
    SemanticCandidate,
    TriageWindow,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.triage import SemanticCandidateInput, SubmitSemanticCandidatesInput
from docket.services.briefs import DailyBriefService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.events import SemanticCandidateCompiler
from docket.services.gmail_ingestion import GmailIngestionService
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
        assert len(outbox) == 3
        assert outbox[0].aggregate_id == brief.queue_item_id
        assert {event.aggregate_id for event in outbox[1:]} == {
            candidate.queue_item_id for candidate in candidates
        }

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
    assert brief_card["controls"] == []


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
