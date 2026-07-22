import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    CalendarLink,
    ExecutionAttempt,
    Operation,
    OutboxEvent,
    QueueItem,
    Record,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import CalendarEventRequest
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.operations import OperationRunner


def schedule(*, days: list[str] | None = None, start_time: str = "10:30:00") -> dict:
    meeting_days = days or ["MO", "WE"]
    return {
        "meeting_type": "lecture",
        "days": meeting_days,
        "start_time": start_time,
        "end_time": "11:50:00",
        "location": "Building 14",
        "start_date": "2026-08-24",
        "end_date": "2026-12-18",
        "timezone": "America/Los_Angeles",
        "first_occurrence_date": "2026-08-26" if meeting_days == ["WE"] else "2026-08-24",
    }


def add_operation(
    session: Session,
    *,
    record: Record,
    account: Account,
    operation_type: str,
    record_version: int,
    event_schedule: dict,
    external_event_id: str | None = None,
    provider_etag: str | None = None,
) -> Operation:
    queue_item = QueueItem(
        deduplication_key=f"manual_action:{uuid.uuid4()}",
        material_fingerprint=uuid.uuid4().hex * 2,
        category="calendar_change",
        title=operation_type,
        summary="Calendar operation test",
        status="executing",
        priority="normal",
    )
    session.add(queue_item)
    session.flush()
    action = Action(
        queue_item_id=queue_item.id,
        record_id=record.id,
        action_type=operation_type,
        status="ready",
    )
    session.add(action)
    session.flush()
    parameters = {
        "record_id": str(record.id),
        "record_version": record_version,
        "meeting_id": "lecture-mo-we-1",
        "calendar_id": "docket-smoke-calendar",
        "summary": "CSC 101 - Fundamentals",
        "schedule": event_schedule,
    }
    if external_event_id is not None:
        parameters["external_event_id"] = external_event_id
        parameters["provider_etag"] = provider_etag
    revision = ActionRevision(
        action_id=action.id,
        revision=1,
        action_type=operation_type,
        account_id=account.id,
        parameters=parameters,
        parameters_sha256="a" * 64,
        preview={"action_type": operation_type},
        preview_sha256="b" * 64,
        risk_class="external_private_write",
        target_versions={"record": {"id": str(record.id), "version": record_version}},
        created_by_actor_type="hermes",
    )
    session.add(revision)
    session.flush()
    approval = Approval(
        action_revision_id=revision.id,
        status="consumed",
        short_code_sha256=uuid.uuid4().hex * 2,
        authorized_user_id="000000000000000001",
        expires_at=utc_now() + timedelta(minutes=15),
    )
    session.add(approval)
    session.flush()
    operation_id = uuid.uuid4()
    operation = Operation(
        id=operation_id,
        action_revision_id=revision.id,
        approval_id=approval.id,
        idempotency_key=f"{operation_type}:{uuid.uuid4()}",
        operation_type=operation_type,
        account_id=account.id,
        status="pending",
        provider_correlation=str(operation_id),
        next_attempt_at=utc_now(),
    )
    session.add(operation)
    session.flush()
    approval.consumed_operation_id = operation.id
    return operation


def seed_create(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory.begin() as session:
        record = Record(
            record_type="course",
            canonical_key=f"course:{uuid.uuid4()}:csc-101:01",
            schema_version=1,
            title="CSC 101",
            status="active",
            data={},
        )
        account = Account(
            provider="google",
            external_account_id=f"test-{uuid.uuid4()}",
            capabilities=["google_calendar"],
        )
        session.add_all([record, account])
        session.flush()
        operation = add_operation(
            session,
            record=record,
            account=account,
            operation_type="calendar_create_meeting",
            record_version=1,
            event_schedule=schedule(),
        )
        return operation.id


@pytest.mark.integration
def test_calendar_creation_executes_once_and_links_result(session_factory) -> None:
    operation_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)

    assert runner.run_due_once() is True
    assert runner.run_due_once() is False

    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        link = session.scalar(select(CalendarLink))
        attempt = session.scalar(select(ExecutionAttempt))
        assert operation is not None and link is not None and attempt is not None
        assert operation.status == "succeeded"
        assert attempt.status == "succeeded"
        assert link.external_event_id in provider.events
        assert link.last_synced_version == 1
        assert link.synced_snapshot == next(iter(provider.events.values())).snapshot
        assert len(provider.events) == 1


@pytest.mark.integration
def test_unknown_create_outcome_reconciles_without_duplicate(session_factory) -> None:
    operation_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    provider.next_create_outcome = "unknown_after_write"
    runner = OperationRunner(session_factory, provider, consistency_window_seconds=0)

    assert runner.run_due_once() is True
    with session_factory() as session:
        assert session.get(Operation, operation_id).status == "reconciliation_required"
        assert session.scalar(select(CalendarLink)) is None
    assert len(provider.events) == 1

    assert runner.reconcile_once() is True
    with session_factory() as session:
        assert session.get(Operation, operation_id).status == "succeeded"
        assert session.scalar(select(CalendarLink)) is not None
    assert len(provider.events) == 1


@pytest.mark.integration
def test_zero_match_reconciliation_returns_same_operation_to_execution(
    session_factory,
) -> None:
    operation_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider, consistency_window_seconds=0)
    claim = runner.claim_due()
    assert claim is not None
    runner.mark_provider_call_started(claim)
    with session_factory.begin() as session:
        operation = session.get(Operation, operation_id)
        operation.status = "reconciliation_required"
        operation.lease_token = None
        operation.leased_until = None
        operation.next_attempt_at = utc_now()

    assert runner.reconcile_once() is True
    with session_factory() as session:
        assert session.get(Operation, operation_id).status == "pending"
    assert runner.run_due_once() is True
    assert len(provider.events) == 1


@pytest.mark.integration
def test_expired_lease_distinguishes_before_and_after_call(session_factory) -> None:
    first_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider, lease_seconds=1)
    first_claim = runner.claim_due()
    assert first_claim is not None
    with session_factory.begin() as session:
        session.get(Operation, first_id).leased_until = utc_now() - timedelta(seconds=1)
    assert runner.recover_expired_leases() == 1
    with session_factory() as session:
        first = session.get(Operation, first_id)
        assert first.status == "pending"
    with session_factory.begin() as session:
        session.get(Operation, first_id).status = "failed"

    second_id = seed_create(session_factory)
    second_claim = runner.claim_due()
    assert second_claim is not None
    runner.mark_provider_call_started(second_claim)
    with session_factory.begin() as session:
        session.get(Operation, second_id).leased_until = utc_now() - timedelta(seconds=1)
    assert runner.recover_expired_leases() == 1
    with session_factory() as session:
        second = session.get(Operation, second_id)
        assert second.status == "reconciliation_required"


@pytest.mark.integration
def test_calendar_update_modifies_linked_event_in_place(session_factory) -> None:
    create_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once() is True

    with session_factory.begin() as session:
        create = session.get(Operation, create_id)
        link = session.scalar(select(CalendarLink))
        assert create is not None and link is not None
        record = session.get(Record, link.record_id)
        account = session.get(Account, link.account_id)
        assert record is not None and account is not None
        record.version = 2
        update = add_operation(
            session,
            record=record,
            account=account,
            operation_type="calendar_update_meeting",
            record_version=2,
            event_schedule=schedule(days=["WE"], start_time="12:30:00"),
            external_event_id=link.external_event_id,
            provider_etag=link.provider_etag,
        )
        update_id = update.id
        original_event_id = link.external_event_id

    assert runner.run_due_once() is True
    with session_factory() as session:
        update = session.get(Operation, update_id)
        link = session.scalar(select(CalendarLink))
        assert update is not None and link is not None
        assert update.status == "succeeded"
        assert link.external_event_id == original_event_id
        assert link.last_synced_version == 2
        assert link.synced_snapshot["start"]["dateTime"].endswith("12:30:00")
        assert len(provider.events) == 1


@pytest.mark.integration
def test_multiple_reconciliation_matches_remain_visible(session_factory) -> None:
    operation_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    provider.next_create_outcome = "unknown_after_write"
    runner = OperationRunner(session_factory, provider, consistency_window_seconds=0)
    assert runner.run_due_once() is True
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        revision = session.get(ActionRevision, operation.action_revision_id)
        request = CalendarEventRequest(
            calendar_id=revision.parameters["calendar_id"],
            provider_correlation=operation.provider_correlation,
            summary=revision.parameters["summary"],
            schedule=revision.parameters["schedule"],
        )
    provider.add_correlation_duplicate(request)

    assert runner.reconcile_once() is True
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        alert = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "system.alert.requested")
        )
        assert operation.status == "reconciliation_required"
        assert alert is not None
        assert alert.payload["match_count"] == 2


@pytest.mark.integration
def test_transient_failure_retries_same_operation_with_new_attempt(session_factory) -> None:
    operation_id = seed_create(session_factory)
    provider = FakeCalendarProvider()
    provider.next_create_outcome = "transient"
    runner = OperationRunner(session_factory, provider)

    assert runner.run_due_once() is True
    with session_factory.begin() as session:
        operation = session.get(Operation, operation_id)
        assert operation.status == "pending"
        assert operation.attempt_count == 1
        operation.next_attempt_at = utc_now()

    assert runner.run_due_once() is True
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.operation_id == operation_id)
                .order_by(ExecutionAttempt.attempt_number)
            )
        )
        assert operation.status == "succeeded"
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert len(provider.events) == 1
