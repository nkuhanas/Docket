from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    ChangeSet,
    ExecutionAttempt,
    IntentSession,
    Operation,
    OperationTarget,
    ProviderAccount,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.operations import OperationRunner


def _seed_failed_operations(
    session_factory,
    error_codes: list[str],
) -> tuple[str, list[uuid.UUID], list[str]]:
    operation_ids: list[uuid.UUID] = []
    idempotency_keys: list[str] = []
    with session_factory.begin() as session:
        intent_session = IntentSession(
            conversation_ref="discord:calendar-recovery",
            source_utterance_ref="utt_01M1Q000000000000000000000",
            semantic_state="ready",
            commit_state="committed",
        )
        account = ProviderAccount(
            provider="google",
            external_account_id="calendar-recovery-account",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add_all([intent_session, account])
        session.flush()
        changeset = ChangeSet(
            intent_session_id=intent_session.id,
            intent_session_ref=intent_session.ref_id,
            idempotency_key="calendar-recovery-changeset",
            basis_refs=[intent_session.source_utterance_ref],
            state="committed",
            committed_at=utc_now(),
        )
        session.add(changeset)
        session.flush()
        for index, error_code in enumerate(error_codes, start=1):
            operation_key = f"calendar-recovery-operation-{index}"
            target_key = f"calendar-recovery-target-{index}"
            target_ref = f"evt_01M1Q0000000000000000000{index}"
            parameters = {
                "calendar_id": "academic@example.com",
                "event": {
                    "title": f"Recovery event {index}",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-08T09:00:00",
                        "end_local": "2026-09-08T10:00:00",
                        "timezone": "America/Los_Angeles",
                    },
                },
            }
            operation = Operation(
                originating_changeset_ref=changeset.ref_id,
                basis_refs=list(changeset.basis_refs),
                canonical_target_refs=[target_ref],
                idempotency_key=operation_key,
                operation_type="calendar_create_event",
                account_id=account.id,
                status="failed",
                provider_correlation=f"calendar-recovery-correlation-{index}",
                attempt_count=1,
                last_error_code=error_code,
                last_error_message="Prior safe provider failure.",
            )
            session.add(operation)
            session.flush()
            target = OperationTarget(
                operation_id=operation.id,
                target_key=target_key,
                canonical_target_ref=target_ref,
                target_kind="event",
                idempotency_key=f"{operation_key}:target",
                parameters=parameters,
                parameters_sha256=sha256_json(parameters),
                status="failed",
                attempt_count=1,
                last_error_code=error_code,
            )
            session.add(target)
            session.flush()
            session.add(
                ExecutionAttempt(
                    operation_id=operation.id,
                    operation_target_id=target.id,
                    attempt_number=1,
                    kind="execute",
                    request_summary={"parameters_sha256": target.parameters_sha256},
                    status="failed",
                    error_code=error_code,
                    error_message="Prior safe provider failure.",
                    started_at=utc_now(),
                    completed_at=utc_now(),
                )
            )
            operation_ids.append(operation.id)
            idempotency_keys.append(operation.idempotency_key)
        changeset_ref = changeset.ref_id
    return changeset_ref, operation_ids, idempotency_keys


@pytest.mark.integration
def test_requeue_auth_failures_preserves_operation_identity_and_attempt_history(
    session_factory,
) -> None:
    changeset_ref, operation_ids, idempotency_keys = _seed_failed_operations(
        session_factory,
        ["google_auth_invalid", "google_auth_invalid"],
    )
    runner = OperationRunner(session_factory, FakeCalendarProvider())

    before = runner.auth_failure_recovery_status(changeset_ref)
    assert before.projection() == {
        "changeset_ref": changeset_ref,
        "total": 2,
        "failed_auth": 2,
        "succeeded": 0,
        "active": 0,
        "other_terminal": 0,
        "requeued": 0,
    }

    recovered = runner.requeue_auth_failures(changeset_ref)
    assert recovered.requeued == 2
    with session_factory() as session:
        operations = list(
            session.scalars(select(Operation).order_by(Operation.created_at, Operation.ref_id))
        )
        targets = list(
            session.scalars(select(OperationTarget).order_by(OperationTarget.target_key))
        )
        assert [operation.id for operation in operations] == operation_ids
        assert [operation.idempotency_key for operation in operations] == idempotency_keys
        assert all(operation.status == "pending" for operation in operations)
        assert all(operation.attempt_count == 1 for operation in operations)
        assert all(operation.last_error_code is None for operation in operations)
        assert all(target.status == "pending" for target in targets)
        assert all(target.attempt_count == 1 for target in targets)
        assert session.scalar(select(func.count(ExecutionAttempt.id))) == 2
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "operation.requeued_after_auth_restore"
                )
            )
        )
        assert len(audits) == 2
        assert all(audit.basis_refs for audit in audits)
        assert all(audit.data["prior_error_code"] == "google_auth_invalid" for audit in audits)

    assert runner.run_due_once() is True
    with session_factory() as session:
        first = session.get(Operation, operation_ids[0])
        assert first is not None and first.status == "succeeded"
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.operation_id == operation_ids[0])
                .order_by(ExecutionAttempt.attempt_number)
            )
        )
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]


@pytest.mark.integration
def test_requeue_auth_failures_validates_credentials_before_state_change(session_factory) -> None:
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory,
        ["google_auth_invalid"],
    )
    provider = FakeCalendarProvider()
    provider.next_authorization_outcome = "invalid"
    runner = OperationRunner(session_factory, provider)

    with pytest.raises(CalendarProviderError) as caught:
        runner.requeue_auth_failures(changeset_ref)
    assert caught.value.code == "google_auth_invalid"
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation is not None and operation.status == "failed"
        assert operation.last_error_code == "google_auth_invalid"
        assert session.scalar(select(func.count(AuditEvent.id))) == 0


@pytest.mark.integration
def test_requeue_auth_failures_refuses_mixed_terminal_failures(session_factory) -> None:
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory,
        ["google_auth_invalid", "google_calendar_rejected"],
    )
    runner = OperationRunner(session_factory, FakeCalendarProvider())

    with pytest.raises(DocketError) as caught:
        runner.requeue_auth_failures(changeset_ref)
    assert caught.value.code == "calendar_recovery_mixed_failure"
    with session_factory() as session:
        operations = [session.get(Operation, operation_id) for operation_id in operation_ids]
        assert all(
            operation is not None and operation.status == "failed" for operation in operations
        )
        assert session.scalar(select(func.count(AuditEvent.id))) == 0


@pytest.mark.integration
def test_requeue_auth_failures_rolls_back_the_whole_scope_on_invalid_target(
    session_factory,
) -> None:
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory,
        ["google_auth_invalid", "google_auth_invalid"],
    )
    with session_factory.begin() as session:
        second_target = session.scalar(
            select(OperationTarget).where(OperationTarget.operation_id == operation_ids[1])
        )
        assert second_target is not None
        second_target.status = "pending"
    runner = OperationRunner(session_factory, FakeCalendarProvider())

    with pytest.raises(DocketError) as caught:
        runner.requeue_auth_failures(changeset_ref)
    assert caught.value.code == "calendar_recovery_invalid_operation_state"
    with session_factory() as session:
        operations = [session.get(Operation, operation_id) for operation_id in operation_ids]
        assert all(
            operation is not None
            and operation.status == "failed"
            and operation.last_error_code == "google_auth_invalid"
            for operation in operations
        )
        assert session.scalar(select(func.count(AuditEvent.id))) == 0
