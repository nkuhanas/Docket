from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.models import (
    Account,
    ActionRevision,
    Approval,
    CalendarSyncState,
    CanonicalEvent,
    EventObservation,
    QueueItem,
    SemanticCandidate,
    SourceItem,
)
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.triage import SemanticCandidateInput, SubmitSemanticCandidatesInput
from docket.services.events import SemanticCandidateCompiler
from docket.services.gmail_ingestion import GmailIngestionService
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
def test_complete_inferred_event_becomes_one_version_bound_proposal(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="calendar-notice",
        thread_id="calendar-thread",
        source_version="1",
        subject="Project review confirmed",
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
                    },
                    entity_mentions=[
                        {
                            "entity_class": "organization",
                            "name": "Design Club",
                            "role": "organizer",
                        }
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
        assert observation is not None and observation.correlation_state == "new"
        assert observation.canonical_event_id == canonical.id
        assert source is not None
        assert queue_item is not None and queue_item.presentation == "proposal"
        assert queue_item.primary_source_item_id == source.id
        assert revision is not None and revision.authority == "inferred"
        assert revision.parameters["canonical_event_id"] == str(canonical.id)
        assert approval is not None and approval.status == "pending"
