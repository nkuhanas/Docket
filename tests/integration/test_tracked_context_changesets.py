from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from docket.config import get_settings
from docket.domain.public_refs import new_public_ref
from docket.models import (
    AuditEvent,
    CanonicalEvent,
    ChangeSet,
    IntentSession,
    Item,
    Operation,
    OperatorUtterance,
    Task,
    TemporalBinding,
)
from docket.schemas.authority import ChangeSetCommit, ChangeSetContent, ChangeSetPrepare
from docket.services.change_sets import ChangeSetService
from docket.services.tracked_context import TrackedContextService


@pytest.mark.integration
def test_one_changeset_atomically_creates_item_task_and_due_date(session) -> None:
    utterance = OperatorUtterance(
        actor_ref=f"discord_user:{get_settings().operator_discord_user_id}",
        transport="discord",
        source_message_ref="discord_message:1:2:3",
        conversation_ref="discord:1:2",
        said_at=datetime.now(UTC),
        verbatim_text="Track Problem Set 4 and its October 3 due date.",
        content_hash="1" * 64,
        request_key="discord:1:2:3:0",
    )
    session.add(utterance)
    session.flush()
    intent = IntentSession(
        conversation_ref=utterance.conversation_ref,
        source_utterance_ref=utterance.ref_id,
        semantic_state="ready",
        commit_state="not_attempted",
    )
    session.add(intent)
    session.flush()

    content = ChangeSetContent.model_validate(
        {
            "basis_refs": [utterance.ref_id],
            "tracked_context_changes": [
                {
                    "mutation_type": "item_create",
                    "change_id": "problem-set-4",
                    "action": "create",
                    "object_type": "item",
                    "affected_fields": ["title", "kind"],
                    "basis_refs": [utterance.ref_id],
                    "create_spec": {
                        "title": "Problem Set 4",
                        "kind": "academic.assignment",
                    },
                },
                {
                    "mutation_type": "task_create",
                    "change_id": "complete-problem-set-4",
                    "action": "create",
                    "object_type": "task",
                    "affected_fields": ["item_ref", "task_state"],
                    "basis_refs": [utterance.ref_id],
                    "create_spec": {
                        "item_change_id": "problem-set-4",
                        "title": "Complete Problem Set 4",
                    },
                },
                {
                    "mutation_type": "temporal_binding_create",
                    "change_id": "problem-set-4-due",
                    "action": "create",
                    "object_type": "temporal_binding",
                    "affected_fields": ["subject_ref", "role", "temporal_value"],
                    "basis_refs": [utterance.ref_id],
                    "create_spec": {
                        "subject_change_id": "complete-problem-set-4",
                        "role": "due_by",
                        "temporal_value": {
                            "kind": "date",
                            "date": "2026-10-03",
                            "timezone": "America/Los_Angeles",
                        },
                    },
                },
            ],
        }
    )
    service = ChangeSetService(
        session,
        handlers=TrackedContextService(session).handlers(),
    )
    changeset, created = service.prepare(
        ChangeSetPrepare(
            intent_session_ref=intent.ref_id,
            expected_session_version=intent.version,
            idempotency_key="test:tracked-context:problem-set-4",
            content=content,
        )
    )
    assert created is True
    assert changeset.state == "validated"

    committed, affected_refs = service.commit(
        ChangeSetCommit(
            changeset_ref=changeset.ref_id,
            expected_version=changeset.version,
            idempotency_key=changeset.idempotency_key,
            authority_utterance_ref=utterance.ref_id,
        )
    )

    item = session.scalar(select(Item))
    task = session.scalar(select(Task))
    temporal = session.scalar(select(TemporalBinding))
    assert item is not None and task is not None and temporal is not None
    assert task.item_ref == item.ref_id
    assert temporal.subject_ref == task.ref_id
    assert temporal.role == "due_by"
    assert temporal.temporal_value == {
        "kind": "date",
        "date": "2026-10-03",
        "timezone": "America/Los_Angeles",
    }
    assert committed.state == "committed"
    assert set(affected_refs) == {item.ref_id, task.ref_id, temporal.ref_id}
    assert session.scalar(select(func.count(CanonicalEvent.id))) == 0
    assert session.scalar(select(func.count(Operation.id))) == 0
    # compiled + one audit per canonical primitive + committed
    assert session.scalar(select(func.count(AuditEvent.id))) == 5


@pytest.mark.integration
def test_temporal_supersession_preserves_prior_binding(session) -> None:
    item = Item(
        title="Midterm Exam",
        kind="academic.exam",
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref="chg_01M18DYEYJVVJ7TW5VQQBCA6NC",
    )
    session.add(item)
    session.flush()
    prior = TemporalBinding(
        subject_ref=item.ref_id,
        role="scheduled_on",
        temporal_value={
            "kind": "date",
            "date": "2026-09-18",
            "timezone": "America/Los_Angeles",
        },
        basis_refs=[],
        decision_refs=[],
        source_refs=[],
        created_by_changeset_ref="chg_01M18DYEYJVVJ7TW5VQQBCA6NC",
    )
    session.add(prior)
    session.flush()

    utterance = OperatorUtterance(
        actor_ref=f"discord_user:{get_settings().operator_discord_user_id}",
        transport="discord",
        source_message_ref="discord_message:1:2:4",
        conversation_ref="discord:1:2",
        said_at=datetime.now(UTC),
        verbatim_text="The midterm is at 10:10 AM.",
        content_hash="2" * 64,
        request_key="discord:1:2:4:0",
    )
    session.add(utterance)
    session.flush()
    changeset = ChangeSet(
        ref_id=new_public_ref("chg"),
        intent_session_id=uuid.uuid4(),
        intent_session_ref="ses_01M18DYEYJVVJ7TW5VQQBCA6NC",
        idempotency_key="test:temporal:supersede",
        basis_refs=[utterance.ref_id],
        state="validated",
    )
    change = ChangeSetContent.model_validate(
        {
            "basis_refs": [utterance.ref_id],
            "expected_versions": {prior.ref_id: prior.version},
            "tracked_context_changes": [
                {
                    "mutation_type": "temporal_binding_supersede",
                    "change_id": "midterm-exact-time",
                    "action": "supersede",
                    "object_type": "temporal_binding",
                    "object_ref": prior.ref_id,
                    "affected_fields": ["temporal_value", "canonical_status"],
                    "basis_refs": [utterance.ref_id],
                    "create_spec": {
                        "subject_ref": item.ref_id,
                        "role": "occurs_at",
                        "temporal_value": {
                            "kind": "datetime",
                            "local_datetime": "2026-09-18T10:10:00",
                            "timezone": "America/Los_Angeles",
                        },
                    },
                }
            ],
        }
    ).tracked_context_changes[0]

    affected = TrackedContextService(session).apply_temporal_binding(
        session, changeset, change
    )
    replacement = session.scalar(
        select(TemporalBinding).where(TemporalBinding.ref_id != prior.ref_id)
    )
    assert replacement is not None
    assert prior.canonical_status == "historical"
    assert replacement.supersedes_ref == prior.ref_id
    assert set(affected) == {prior.ref_id, replacement.ref_id}
