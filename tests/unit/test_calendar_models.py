import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    CalendarLink,
    ExecutionAttempt,
    Operation,
    QueueItem,
    Record,
)


def milestone_two_graph(session: Session) -> tuple[Record, Account, ActionRevision, Approval]:
    term = Record(
        record_type="term",
        canonical_key="term:cal-poly:fall-2026",
        schema_version=1,
        title="Fall 2026",
        data={},
        status="active",
    )
    account = Account(
        provider="google",
        external_account_id="primary",
        capabilities=["google_calendar"],
    )
    queue_item = QueueItem(
        deduplication_key="manual_action:request-1",
        material_fingerprint="a" * 64,
        category="calendar_change",
        title="Create CSC 101 lecture",
        summary="Monday and Wednesday, 10:30-11:50",
        status="awaiting_approval",
        priority="normal",
    )
    session.add_all([term, account, queue_item])
    session.flush()
    action = Action(
        queue_item_id=queue_item.id,
        record_id=term.id,
        action_type="calendar_create_meeting",
        status="approval_pending",
    )
    session.add(action)
    session.flush()
    revision = ActionRevision(
        action_id=action.id,
        revision=1,
        action_type=action.action_type,
        account_id=account.id,
        parameters={"meeting_id": "lecture-mo-we-1"},
        parameters_sha256="b" * 64,
        preview={"summary": "CSC 101"},
        preview_sha256="c" * 64,
        risk_class="external_private_write",
        target_versions={"record": {str(term.id): 1}},
        created_by_actor_type="hermes",
    )
    session.add(revision)
    session.flush()
    approval = Approval(
        action_revision_id=revision.id,
        status="pending",
        short_code_sha256="d" * 64,
        authorized_user_id="000000000000000001",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session.add(approval)
    session.flush()
    return term, account, revision, approval


def test_only_one_pending_approval_exists_per_revision(session: Session) -> None:
    _, _, revision, _ = milestone_two_graph(session)
    session.add(
        Approval(
            action_revision_id=revision.id,
            status="pending",
            short_code_sha256="e" * 64,
            authorized_user_id="000000000000000001",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_action_revision_cannot_be_updated(session: Session) -> None:
    _, _, revision, _ = milestone_two_graph(session)
    revision.preview = {"summary": "mutated after approval"}

    with pytest.raises(ValueError, match="immutable"):
        session.flush()


def test_operation_attempt_number_is_unique(session: Session) -> None:
    _, account, revision, approval = milestone_two_graph(session)
    operation = Operation(
        action_revision_id=revision.id,
        approval_id=approval.id,
        idempotency_key=f"calendar:create:{uuid.uuid4()}",
        operation_type="calendar_create_meeting",
        account_id=account.id,
        status="pending",
        provider_correlation=str(uuid.uuid4()),
    )
    session.add(operation)
    session.flush()
    request_summary = {"parameters_sha256": revision.parameters_sha256}
    session.add_all(
        [
            ExecutionAttempt(
                operation_id=operation.id,
                attempt_number=1,
                kind="execute",
                request_summary=request_summary,
                status="started",
            ),
            ExecutionAttempt(
                operation_id=operation.id,
                attempt_number=1,
                kind="reconcile",
                request_summary=request_summary,
                status="started",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_calendar_link_target_is_unique(session: Session) -> None:
    record, account, _, _ = milestone_two_graph(session)
    shared = {
        "record_id": record.id,
        "meeting_id": "lecture-mo-we-1",
        "account_id": account.id,
        "calendar_id": "calendar@group.calendar.google.com",
        "last_synced_version": 1,
    }
    session.add_all(
        [
            CalendarLink(
                **shared,
                external_event_id="event-1",
                provider_correlation=str(uuid.uuid4()),
            ),
            CalendarLink(
                **shared,
                external_event_id="event-2",
                provider_correlation=str(uuid.uuid4()),
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()
