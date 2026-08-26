import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from docket.config import get_settings
from docket.models import Account, Action, QueueItem, SemanticCandidate, SourceItem
from docket.providers.google.fake_gmail import FakeGmailProvider
from docket.schemas.triage import SemanticCandidateInput, SubmitSemanticCandidatesInput
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
def test_triage_persists_typed_event_candidate_without_housekeeping_or_card(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="event-notice",
        thread_id="event-thread",
        source_version="1",
        sender="Robotics Club <robotics@example.edu>",
        subject="Design review confirmed",
        body_text="Do not persist this body or archive the message.",
    )
    with session_factory.begin() as session:
        session.add(
            Account(
                provider="google",
                external_account_id="semantic-candidate-test",
                capabilities=["gmail"],
                enabled=True,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    service = TriageService(session_factory, provider, _settings())
    claim = service.claim_batch()
    source_id = str(claim["sources"][0]["source_id"])

    result = service.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=source_id,
            claim_token=str(claim["claim_token"]),
            candidates=[
                SemanticCandidateInput(
                    candidate_key="design-review",
                    kind="event",
                    mutation="create",
                    title="Robotics Club design review",
                    summary="A design review was confirmed for the club project.",
                    event={
                        "title": "Robotics Club design review",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-09-03T15:00:00",
                            "end_local": "2026-09-03T16:00:00",
                        },
                    },
                    entity_mentions=[
                        {
                            "entity_class": "organization",
                            "name": "Robotics Club",
                            "role": "organizer",
                        }
                    ],
                    context_labels=["club"],
                    confidence=0.94,
                )
            ],
        )
    )

    assert result["disposition"] == "candidates_persisted"
    with session_factory() as session:
        source = session.get(SourceItem, uuid.UUID(source_id))
        candidate = session.scalar(select(SemanticCandidate))
        assert source is not None and source.classification is not None
        assert source.classification["schema_version"] == 2
        assert candidate is not None
        assert candidate.kind == "event"
        assert candidate.mutation == "create"
        assert candidate.status == "pending"
        assert candidate.fields["event"]["timing"]["timezone"] == ("America/Los_Angeles")
        assert "Do not persist" not in repr(source.classification)
        assert session.scalar(select(Action)) is None
        assert session.scalar(select(QueueItem)) is None


def test_update_candidate_requires_correlation() -> None:
    with pytest.raises(ValidationError, match="correlation hints"):
        SemanticCandidateInput(
            candidate_key="changed-meeting",
            kind="event",
            mutation="update",
            title="Meeting moved",
            summary="The meeting time changed.",
            confidence=0.8,
        )


@pytest.mark.integration
def test_empty_extraction_and_duplicate_thread_candidate_are_idempotent(
    session_factory,
) -> None:
    provider = FakeGmailProvider()
    provider.add_message(
        message_id="empty-source",
        thread_id="empty-thread",
        source_version="1",
        subject="No semantic content",
    )
    provider.add_message(
        message_id="duplicate-one",
        thread_id="shared-thread",
        source_version="1",
        subject="Interview confirmed",
    )
    provider.add_message(
        message_id="duplicate-two",
        thread_id="shared-thread",
        source_version="1",
        subject="Interview confirmed again",
    )
    with session_factory.begin() as session:
        session.add(
            Account(
                provider="google",
                external_account_id="semantic-dedupe-test",
                capabilities=["gmail"],
                enabled=True,
            )
        )
    assert (
        GmailIngestionService(session_factory, provider, _settings())
        .run_due_once(force=True)
        .completed
    )
    service = TriageService(session_factory, provider, _settings())
    claim = service.claim_batch()
    sources = {source["external_object_id"]: source for source in claim["sources"]}
    token = str(claim["claim_token"])

    empty = service.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(sources["empty-source"]["source_id"]),
            claim_token=token,
            candidates=[],
        )
    )
    assert empty["candidates"] == []

    candidate = SemanticCandidateInput(
        candidate_key="interview-first-wording",
        kind="event",
        mutation="create",
        title="Acme interview",
        summary="The interview is confirmed.",
        event={
            "title": "Acme interview",
            "timing": {
                "kind": "timed",
                "start_local": "2026-09-03T15:00:00",
                "end_local": "2026-09-03T16:00:00",
            },
        },
        confidence=0.95,
    )
    created = service.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(sources["duplicate-one"]["source_id"]),
            claim_token=token,
            candidates=[candidate],
        )
    )
    matched = service.submit_candidates(
        SubmitSemanticCandidatesInput(
            source_id=str(sources["duplicate-two"]["source_id"]),
            claim_token=token,
            candidates=[
                candidate.model_copy(
                    update={
                        "candidate_key": "interview-second-wording",
                        "summary": "A second message confirms the same interview.",
                        "confidence": 0.91,
                    }
                )
            ],
        )
    )

    assert created["candidates"][0]["disposition"] == "created"
    assert matched["candidates"][0]["disposition"] == "matched_existing"
    assert matched["candidates"][0]["candidate_id"] == created["candidates"][0]["candidate_id"]
    with session_factory() as session:
        assert len(session.scalars(select(SemanticCandidate)).all()) == 1
