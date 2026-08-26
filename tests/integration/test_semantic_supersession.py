from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    CalendarSyncState,
    CanonicalEvent,
    DailyBrief,
    DailyBriefItem,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
    SemanticCandidate,
    SourceItem,
    TriageWindow,
)
from docket.services.events import SemanticCandidateCompiler


def _source(
    session,
    *,
    account_id,
    object_id: str,
    fingerprint: str,
) -> SourceItem:
    source = SourceItem(
        account_id=account_id,
        provider="gmail",
        external_object_id=object_id,
        external_parent_id=f"{object_id}-thread",
        source_version="1",
        source_fingerprint=fingerprint,
        received_at=datetime.now(UTC),
        status="classified",
    )
    session.add(source)
    session.flush()
    return source


def _event_candidate(
    session,
    *,
    source: SourceItem,
    index: int,
    mutation: str,
    title: str,
    fields: dict,
    semantic_key: str,
) -> SemanticCandidate:
    candidate = SemanticCandidate(
        source_item_id=source.id,
        candidate_index=index,
        candidate_key=f"candidate-{index}",
        semantic_key=semantic_key,
        kind="event",
        mutation=mutation,
        title=title,
        summary=f"Evidence for {title}.",
        fields=fields,
        confidence=0.95,
        status="pending",
    )
    session.add(candidate)
    session.flush()
    return candidate


@pytest.mark.integration
def test_new_evidence_supersedes_current_edited_revision_and_aggregate_card(session) -> None:
    now = datetime.now(UTC)
    account = Account(
        provider="google",
        external_account_id="semantic-supersession",
        capabilities=["gmail", "google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    source = SourceItem(
        account_id=account.id,
        provider="gmail",
        external_object_id="supersession-message",
        external_parent_id="supersession-thread",
        source_version="1",
        source_fingerprint="a" * 64,
        received_at=now,
        status="classified",
    )
    session.add(source)
    session.flush()
    child = QueueItem(
        primary_source_item_id=source.id,
        deduplication_key="semantic-supersession-child",
        material_fingerprint="b" * 64,
        category="calendar_change",
        title="Original event",
        summary="Original inferred formulation.",
        status="awaiting_approval",
        priority="normal",
        presentation="proposal",
        received_at=now,
    )
    brief_queue = QueueItem(
        deduplication_key="semantic-supersession-brief",
        material_fingerprint="c" * 64,
        category="morning_brief",
        title="Morning brief",
        summary="One decision.",
        status="completed",
        priority="normal",
        presentation="awareness",
        received_at=now,
        resolved_at=now,
        resolution_code="morning_brief_published",
    )
    session.add_all((child, brief_queue))
    session.flush()
    candidate = SemanticCandidate(
        source_item_id=source.id,
        candidate_index=0,
        candidate_key="original-event",
        semantic_key="d" * 64,
        kind="event",
        mutation="create",
        title="Original event",
        summary="Original inferred formulation.",
        confidence=0.9,
        status="proposed",
        queue_item_id=child.id,
    )
    canonical = CanonicalEvent(
        canonical_key="semantic-supersession-event",
        title="Original event",
        status="proposed",
        event_spec={"title": "Original event"},
        authority="inferred",
    )
    window = TriageWindow(
        window_kind="overnight",
        local_date=date(2026, 8, 26),
        timezone="America/Los_Angeles",
        starts_at=now - timedelta(hours=9),
        ends_at=now - timedelta(hours=1),
        status="published",
    )
    session.add_all((candidate, canonical, window))
    session.flush()
    brief = DailyBrief(
        brief_kind="morning",
        local_date=date(2026, 8, 26),
        window_id=window.id,
        queue_item_id=brief_queue.id,
        status="published",
        content_sha256="e" * 64,
        published_at=now,
    )
    session.add(brief)
    session.flush()
    session.add(
        DailyBriefItem(
            brief_id=brief.id,
            semantic_candidate_id=candidate.id,
            section="Calendar",
            display_order=0,
        )
    )
    action = Action(
        queue_item_id=child.id,
        action_type="calendar_create_event",
        status="approval_pending",
        current_revision=2,
    )
    session.add(action)
    session.flush()
    parameters = {"canonical_event_id": str(canonical.id), "event": {"title": "Original"}}
    preview = {"event": {"title": "Original"}}
    revision_one = ActionRevision(
        action_id=action.id,
        revision=1,
        action_type="calendar_create_event",
        account_id=account.id,
        parameters=parameters,
        parameters_sha256=sha256_json(parameters),
        preview=preview,
        preview_sha256=sha256_json(preview),
        risk_class="external_private_write",
        authority="inferred",
        target_versions={"queue_item": {"id": str(child.id), "version": 1}},
        created_by_actor_type="docket",
    )
    edited_parameters = {
        "canonical_event_id": str(canonical.id),
        "event": {"title": "User-edited title"},
    }
    edited_preview = {"event": {"title": "User-edited title"}}
    revision_two = ActionRevision(
        action_id=action.id,
        revision=2,
        action_type="calendar_create_event",
        account_id=account.id,
        parameters=edited_parameters,
        parameters_sha256=sha256_json(edited_parameters),
        preview=edited_preview,
        preview_sha256=sha256_json(edited_preview),
        risk_class="external_private_write",
        authority="inferred",
        target_versions={"queue_item": {"id": str(child.id), "version": 1}},
        created_by_actor_type="plugin",
    )
    session.add_all((revision_one, revision_two))
    session.flush()
    old_approval = Approval(
        action_revision_id=revision_one.id,
        status="superseded",
        short_code_sha256="f" * 64,
        authorized_user_id="000000000000000001",
        expires_at=now + timedelta(days=1),
    )
    current_approval = Approval(
        action_revision_id=revision_two.id,
        status="pending",
        short_code_sha256="0" * 64,
        authorized_user_id="000000000000000001",
        expires_at=now + timedelta(days=1),
    )
    session.add_all((old_approval, current_approval))
    thread = DiscordDailyThread(
        guild_id="000000000000000002",
        channel_id="000000000000000004",
        local_date=date(2026, 8, 26),
        thread_name="2026-08-26",
        thread_id="100000000000000001",
        status="active",
    )
    session.add(thread)
    session.flush()
    projection = DiscordProjection(
        queue_item_id=brief_queue.id,
        daily_thread_id=thread.id,
        projection_version=3,
        message_id="100000000000000002",
        render_schema_version=1,
        render_sha256="1" * 64,
        component_sha256="2" * 64,
        view_action_revision_id=revision_two.id,
        view_mode="brief_review",
        view_page=1,
        status="delivered",
    )
    session.add(projection)
    session.flush()
    current_approval.control_projection_id = projection.id
    session.flush()

    SemanticCandidateCompiler._supersede_pending_formulation(
        session,
        canonical,
        reason="newer_evidence_superseded_proposal",
    )
    session.flush()

    assert action.status == "superseded"
    assert old_approval.status == "superseded"
    assert current_approval.status == "superseded"
    assert candidate.status == "resolved"
    assert candidate.resolution["disposition"] == "newer_evidence_superseded_proposal"
    assert child.status == "completed"
    assert child.resolution_code == "newer_evidence_superseded_proposal"
    refresh = session.scalar(select(OutboxEvent))
    assert refresh is not None
    assert refresh.aggregate_id == brief_queue.id
    assert refresh.payload == {
        "queue_item_id": str(brief_queue.id),
        "projection_id": str(projection.id),
        "target_local_date": "2026-08-26",
        "status": "superseded",
    }


@pytest.mark.integration
def test_update_and_cancellation_reconcile_one_pending_create_formulation(
    session_factory,
) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        account = Account(
            provider="google",
            external_account_id="semantic-pending-lifecycle",
            capabilities=["gmail", "google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        account_id = account.id
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
        create_source = _source(
            session,
            account_id=account.id,
            object_id="pending-create",
            fingerprint="3" * 64,
        )
        original = _event_candidate(
            session,
            source=create_source,
            index=0,
            mutation="create",
            title="Lifecycle review",
            fields={
                "event": {
                    "title": "Lifecycle review",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-10T14:00:00",
                        "end_local": "2026-09-10T14:30:00",
                    },
                },
                "correlation": {"sender_event_id": "lifecycle-review-1"},
                "entity_mentions": [],
                "context_labels": [],
                "missing_fields": [],
            },
            semantic_key="4" * 64,
        )
        original_id = original.id

    compiler = SemanticCandidateCompiler(session_factory, settings)
    assert compiler.run_due_once()
    with session_factory() as session:
        canonical = session.scalar(select(CanonicalEvent))
        original = session.get(SemanticCandidate, original_id)
        first_approval = session.scalar(select(Approval))
        assert canonical is not None and original is not None
        assert original.status == "proposed"
        assert first_approval is not None and first_approval.status == "pending"
        canonical_id = canonical.id
        first_approval_id = first_approval.id

    with session_factory.begin() as session:
        duplicate_source = _source(
            session,
            account_id=account_id,
            object_id="pending-create-confirmation",
            fingerprint="9" * 64,
        )
        duplicate = _event_candidate(
            session,
            source=duplicate_source,
            index=0,
            mutation="create",
            title="Lifecycle review",
            fields={
                "event": {
                    "title": "Lifecycle review",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-10T14:00:00",
                        "end_local": "2026-09-10T14:30:00",
                    },
                },
                "correlation": {"sender_event_id": "lifecycle-review-1"},
                "entity_mentions": [],
                "context_labels": [],
                "missing_fields": [],
            },
            semantic_key="a" * 64,
        )
        duplicate_id = duplicate.id
    assert compiler.run_due_once()
    with session_factory() as session:
        duplicate = session.get(SemanticCandidate, duplicate_id)
        assert duplicate is not None and duplicate.status == "resolved"
        assert duplicate.resolution["disposition"] == "duplicate_observation"
        assert session.scalar(select(Approval.id).where(Approval.status == "pending")) == (
            first_approval_id
        )

    with session_factory.begin() as session:
        unchanged_source = _source(
            session,
            account_id=account_id,
            object_id="pending-unchanged-update",
            fingerprint="b" * 64,
        )
        unchanged = _event_candidate(
            session,
            source=unchanged_source,
            index=0,
            mutation="update",
            title="Lifecycle review unchanged",
            fields={
                "event": {
                    "title": "Lifecycle review",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-10T14:00:00",
                        "end_local": "2026-09-10T14:30:00",
                    },
                },
                "correlation": {"sender_event_id": "lifecycle-review-1"},
                "entity_mentions": [],
                "context_labels": [],
                "missing_fields": [],
            },
            semantic_key="c" * 64,
        )
        unchanged_id = unchanged.id
    assert compiler.run_due_once()
    with session_factory() as session:
        unchanged = session.get(SemanticCandidate, unchanged_id)
        assert unchanged is not None and unchanged.status == "resolved"
        assert unchanged.resolution["disposition"] == "duplicate_observation"
        assert session.scalar(select(Approval.id).where(Approval.status == "pending")) == (
            first_approval_id
        )

    with session_factory.begin() as session:
        update_source = _source(
            session,
            account_id=account_id,
            object_id="pending-update",
            fingerprint="5" * 64,
        )
        update = _event_candidate(
            session,
            source=update_source,
            index=0,
            mutation="update",
            title="Lifecycle review shifted",
            fields={
                "event": {
                    "title": "Lifecycle review shifted",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-10T15:00:00",
                        "end_local": "2026-09-10T15:30:00",
                    },
                },
                "correlation": {"sender_event_id": "lifecycle-review-1"},
                "entity_mentions": [],
                "context_labels": [],
                "missing_fields": [],
            },
            semantic_key="6" * 64,
        )
        update_id = update.id
    assert compiler.run_due_once()
    with session_factory() as session:
        canonical = session.get(CanonicalEvent, canonical_id)
        original = session.get(SemanticCandidate, original_id)
        update = session.get(SemanticCandidate, update_id)
        first_approval = session.get(Approval, first_approval_id)
        pending = list(session.scalars(select(Approval).where(Approval.status == "pending")))
        assert canonical is not None and canonical.title == "Lifecycle review shifted"
        assert original is not None and original.status == "resolved"
        assert original.resolution["disposition"] == "newer_evidence_superseded_proposal"
        assert update is not None and update.status == "proposed"
        assert first_approval is not None and first_approval.status == "superseded"
        assert len(pending) == 1
        update_approval_id = pending[0].id

    with session_factory.begin() as session:
        cancel_source = _source(
            session,
            account_id=account_id,
            object_id="pending-cancel",
            fingerprint="7" * 64,
        )
        cancellation = _event_candidate(
            session,
            source=cancel_source,
            index=0,
            mutation="cancel",
            title="Lifecycle review cancelled",
            fields={
                "event": None,
                "correlation": {"sender_event_id": "lifecycle-review-1"},
                "entity_mentions": [],
                "context_labels": [],
                "missing_fields": [],
            },
            semantic_key="8" * 64,
        )
        cancellation_id = cancellation.id
    assert compiler.run_due_once()
    with session_factory() as session:
        canonical = session.get(CanonicalEvent, canonical_id)
        update = session.get(SemanticCandidate, update_id)
        cancellation = session.get(SemanticCandidate, cancellation_id)
        update_approval = session.get(Approval, update_approval_id)
        assert canonical is not None and canonical.status == "cancelled"
        assert update is not None and update.status == "resolved"
        assert update_approval is not None and update_approval.status == "superseded"
        assert cancellation is not None and cancellation.status == "resolved"
        assert cancellation.resolution["disposition"] == "pending_create_cancelled"
        assert session.scalar(
            select(Approval.id).where(Approval.status == "pending")
        ) is None

    with session_factory.begin() as session:
        repeated_cancel_source = _source(
            session,
            account_id=account_id,
            object_id="pending-cancel-repeat",
            fingerprint="d" * 64,
        )
        repeated_cancel = _event_candidate(
            session,
            source=repeated_cancel_source,
            index=0,
            mutation="cancel",
            title="Lifecycle review cancellation confirmed",
            fields={
                "event": None,
                "correlation": {"sender_event_id": "lifecycle-review-1"},
                "entity_mentions": [],
                "context_labels": [],
                "missing_fields": [],
            },
            semantic_key="e" * 64,
        )
        repeated_cancel_id = repeated_cancel.id
    assert compiler.run_due_once()
    with session_factory() as session:
        repeated_cancel = session.get(SemanticCandidate, repeated_cancel_id)
        assert repeated_cancel is not None and repeated_cancel.status == "resolved"
        assert repeated_cancel.resolution["disposition"] == "canonical_already_cancelled"
        assert session.scalar(select(Approval.id).where(Approval.status == "pending")) is None
