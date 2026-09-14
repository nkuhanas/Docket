from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CalendarLane,
    CanonicalEvent,
    ChangeSet,
    ExecutionAttempt,
    IntentSession,
    Operation,
    OperationTarget,
    ProviderAccount,
    ProviderEventBinding,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import CalendarProviderError
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.services.history import HistoryService
from docket.services.operations import OperationRunner
from docket.services.provider_intents import ProviderIntentService

RECOVERY_CREDENTIAL_REF = "/run/test-google-reauth/token.json"


def _seed_failed_operations(
    session_factory,
    error_codes: list[str],
    *,
    account_key: str = "calendar-recovery-account",
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
            external_account_id=account_key,
            credential_ref=RECOVERY_CREDENTIAL_REF,
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add_all([intent_session, account])
        session.flush()
        changeset = ChangeSet(
            intent_session_id=intent_session.id,
            intent_session_ref=intent_session.ref_id,
            idempotency_key=f"{account_key}:changeset",
            basis_refs=[intent_session.source_utterance_ref],
            state="committed",
            committed_at=utc_now(),
        )
        session.add(changeset)
        session.flush()
        lane = CalendarLane(
            account_id=account.id, lane=account_key, display_name="Academic",
            calendar_id="academic@example.com", color_hex="#3367D6", status="active",
            basis_refs=list(changeset.basis_refs), created_by_changeset_ref=changeset.ref_id,
        )
        session.add(lane)
        session.flush()
        for index, error_code in enumerate(error_codes, start=1):
            operation_key = f"{account_key}:operation-{index}"
            target_key = f"calendar-recovery-target-{index}"
            event = CanonicalEvent(
                canonical_key=f"{account_key}:event-{index}", title=f"Recovery event {index}",
                status="active", authority="explicit_operator", lane_ref=lane.ref_id,
                lane_id=lane.id, basis_refs=list(changeset.basis_refs),
                created_by_changeset_ref=changeset.ref_id,
                event_spec={
                    "title": f"Recovery event {index}",
                    "calendar_lane": lane.lane,
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-09-08T09:00:00",
                        "end_local": "2026-09-08T10:00:00",
                        "timezone": "America/Los_Angeles",
                    },
                },
            )
            session.add(event)
            session.flush()
            target_ref = event.ref_id
            parameters, _ref, _kind = ProviderIntentService(session)._parameters(
                "calendar_create_event", event=event, lane=lane, projection=None,
                account=account, hints={},
            )
            operation = Operation(
                originating_changeset_ref=changeset.ref_id,
                basis_refs=list(changeset.basis_refs),
                canonical_target_refs=[target_ref],
                idempotency_key=operation_key,
                operation_type="calendar_create_event",
                account_id=account.id,
                status="failed",
                provider_correlation=f"{account_key}:correlation-{index}",
                attempt_count=1,
                last_error_code=error_code,
                last_error_message="Prior safe provider failure.",
            )
            session.add(operation)
            session.flush()
            parameters["external_event_id"] = operation.id.hex
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


def _reauth(runner, **kwargs):
    return runner.requeue_after_reauthorization(
        external_account_id="calendar-recovery-account",
        credential_ref=RECOVERY_CREDENTIAL_REF,
        **kwargs,
    )


@pytest.mark.integration
def test_reauthorization_recovers_only_auth_failures_for_the_matching_account(
    session_factory,
) -> None:
    _changeset, ids, _keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"] * 4 + ["google_calendar_rejected"],
    )
    _other_changeset, other_ids, _ = _seed_failed_operations(
        session_factory, ["google_auth_invalid"], account_key="other-account",
    )
    with session_factory.begin() as session:
        # Succeeded, pending and uncertain outcomes are never re-executed by reauth.
        for operation_id, state in zip(
            ids[1:4], ["succeeded", "pending", "reconciliation_required"], strict=True,
        ):
            session.get(Operation, operation_id).status = state
            session.scalar(select(OperationTarget).where(
                OperationTarget.operation_id == operation_id,
            )).status = state
        original = session.get(Operation, ids[0])
        original_target = session.scalar(select(OperationTarget).where(
            OperationTarget.operation_id == ids[0],
        ))
        before = (original.idempotency_key, list(original.basis_refs),
                  original_target.id, dict(original_target.parameters),
                  original_target.parameters_sha256)
    runner = OperationRunner(session_factory, FakeCalendarProvider())
    receipt = _reauth(runner, batch_size=1)
    assert (receipt.examined, receipt.requeued, receipt.skipped) == (1, 1, {})
    assert receipt.projection()["delivery_state"] == "queued"
    assert _reauth(runner).requeued == 0
    with session_factory() as session:
        assert [session.get(Operation, key).status for key in ids] == [
            "pending", "succeeded", "pending", "reconciliation_required", "failed",
        ]
        assert session.get(Operation, other_ids[0]).status == "failed"
        operation = session.get(Operation, ids[0])
        target = session.get(OperationTarget, before[2])
        assert (operation.idempotency_key, operation.basis_refs, target.id,
                target.parameters, target.parameters_sha256) == before
        assert operation.attempt_count == target.attempt_count == 1
        assert operation.last_error_code is target.last_error_code is None
        assert session.scalar(select(func.count(ExecutionAttempt.id))) == 6
        audits = list(session.scalars(select(AuditEvent)))
        assert len(audits) == 1
        assert audits[0].basis_refs == before[1]
        assert audits[0].data["recovery_trigger"] == "google_reauthorization"


@pytest.mark.integration
@pytest.mark.parametrize("barrier", ["gate", "credential", "account", "capability", "disabled"])
def test_reauthorization_recovery_fails_closed_before_requeue(session_factory, barrier) -> None:
    _seed_failed_operations(session_factory, ["google_auth_invalid"])
    provider = FakeCalendarProvider()
    if barrier == "credential":
        provider.next_authorization_outcome = "invalid"
    with session_factory.begin() as session:
        account = session.scalar(select(ProviderAccount))
        if barrier == "account":
            account.credential_ref = "/different/credential.json"
        elif barrier == "capability":
            account.capabilities = ["gmail"]
        elif barrier == "disabled":
            account.enabled = False
    runner = OperationRunner(session_factory, provider, execution_enabled=barrier != "gate")
    with pytest.raises((DocketError, CalendarProviderError)):
        _reauth(runner)
    with session_factory() as session:
        assert session.scalar(select(Operation)).status == "failed"
        assert session.scalar(select(func.count(AuditEvent.id))) == 0


@pytest.mark.integration
@pytest.mark.parametrize("change,reason", [
    ("cancel", "canonical_state_changed"),
    ("title", "canonical_or_provider_state_changed"),
    ("destination", "canonical_or_provider_state_changed"),
    ("identity", "creation_identity_unavailable"),
    ("lease", "invalid_or_claimed_state"),
    ("target_error", "invalid_or_claimed_state"),
    ("draft", "committed_authority_unavailable"),
    ("superseded", "superseded_delivery"),
])
def test_reauthorization_does_not_revive_obsolete_or_invalid_delivery(
    session_factory, change, reason,
) -> None:
    _chg, ids, _keys = _seed_failed_operations(session_factory, ["google_auth_invalid"])
    with session_factory.begin() as session:
        operation = session.get(Operation, ids[0])
        target = session.scalar(select(OperationTarget))
        event = session.scalar(select(CanonicalEvent))
        if change == "cancel":
            event.status = "cancelled"
        elif change == "title":
            event.title = "A later corrected title"
        elif change == "destination":
            session.scalar(select(CalendarLane)).calendar_id = "different@example.com"
        elif change == "identity":
            target.parameters = {**target.parameters, "external_event_id": "wrong"}
            target.parameters_sha256 = sha256_json(target.parameters)
        elif change == "lease":
            operation.leased_until = utc_now() + timedelta(seconds=30)
        elif change == "target_error":
            target.last_error_code = "different_failure"
        elif change == "draft":
            session.scalar(select(ChangeSet)).state = "draft"
        elif change == "superseded":
            newer = Operation(
                originating_changeset_ref=operation.originating_changeset_ref,
                basis_refs=list(operation.basis_refs), canonical_target_refs=[event.ref_id],
                idempotency_key="later-update", operation_type="calendar_update_event",
                account_id=operation.account_id, status="succeeded",
                provider_correlation="later-update",
            )
            session.add(newer)
            session.flush()
            session.add(OperationTarget(
                operation_id=newer.id, target_key=event.ref_id,
                canonical_target_ref=event.ref_id, target_kind="event",
                idempotency_key="later-update-target", parameters={}, parameters_sha256="0" * 64,
                status="succeeded",
            ))
    receipt = _reauth(OperationRunner(session_factory, FakeCalendarProvider()))
    assert receipt.requeued == 0 and receipt.skipped == {reason: 1}
    with session_factory() as session:
        operation = session.get(Operation, ids[0])
        assert operation.status == "failed" and operation.last_error_code == "google_auth_invalid"


@pytest.mark.integration
def test_reauthorization_batches_and_receipts_are_bounded(session_factory) -> None:
    _seed_failed_operations(session_factory, ["google_auth_invalid"] * 27)
    with session_factory.begin() as session:
        for event in session.scalars(select(CanonicalEvent)):
            event.status = "cancelled"
    receipt = _reauth(OperationRunner(session_factory, FakeCalendarProvider()), batch_size=2)
    assert receipt.examined == 27 and receipt.requeued == 0
    output = receipt.projection()
    assert len(output["skipped_operations"]) == 25
    assert output["skipped_operations_omitted"] == 2
    assert len(json.dumps(output).encode()) < 16384


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


@pytest.mark.parametrize("transient", [True, False])
def test_failed_reconciliation_read_never_returns_unknown_write_to_execution(
    session_factory, monkeypatch, transient
):
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"]
    )
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    runner.requeue_auth_failures(changeset_ref)
    provider.next_create_outcome = "unknown_after_write"
    assert runner.run_due_once()

    def unavailable(request):
        raise CalendarProviderError(
            "read_unavailable", "Injected read failure", transient=transient
        )

    monkeypatch.setattr(provider, "find_by_correlation", unavailable)
    resumed = OperationRunner(session_factory, provider)
    assert resumed.reconcile_once()
    assert not resumed.run_due_once()
    assert len(provider.events) == 1
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation.status == "reconciliation_required"
        assert operation.last_error_code == "read_unavailable"
        assert session.scalar(select(ChangeSet)).state == "committed"
        assert (
            session.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == "operation.reconciliation_read_failed"
                )
            )
            is not None
        )


def test_create_response_lost_and_worker_restart_reconcile_exactly_once(session_factory):
    changeset_ref, operation_ids, keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"]
    )
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    runner.requeue_auth_failures(changeset_ref)
    original_claim = runner.claim_due()
    assert original_claim is not None
    runner.mark_provider_call_started(original_claim)
    remote_result = provider.create_event(original_claim.calendar_request())
    # Crash after transmission, before any response/outcome is recorded locally.
    with session_factory.begin() as session:
        operation = session.get(Operation, operation_ids[0])
        operation.leased_until = utc_now() - timedelta(seconds=1)
        target = session.scalar(select(OperationTarget))
        target.leased_until = operation.leased_until

    resumed = OperationRunner(session_factory, provider)
    assert resumed.recover_expired_leases() == 1
    assert not resumed.run_due_once()
    assert resumed.reconcile_once()
    assert not resumed.reconcile_once()
    assert len(provider.events) == 1
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation.status == "succeeded"
        assert operation.idempotency_key == keys[0]
        assert operation.result["provider_event_id"] == remote_result.external_event_id
        attempts = list(
            session.scalars(select(ExecutionAttempt).order_by(ExecutionAttempt.attempt_number))
        )
        assert [item.kind for item in attempts] == ["execute", "execute", "reconcile"]
        assert [item.status for item in attempts] == ["failed", "unknown", "succeeded"]
        assert session.scalar(select(func.count(ProviderEventBinding.id))) == 1
    # A late result from the dead owner cannot replace the reconciled outcome.
    runner._finish_error(
        original_claim, CalendarProviderError("late", "Late failure", transient=False)
    )
    runner._finish_unknown(original_claim, "Late unknown")
    runner._finish_event_success(original_claim, remote_result)
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation.status == "succeeded"
        assert session.get(ExecutionAttempt, original_claim.attempt_id).status == "unknown"
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_type == "operation.succeeded"
                )
            )
            == 1
        )


def test_transient_missing_reconciliation_match_reuses_same_creation_id(
    session_factory, monkeypatch
):
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"]
    )
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    runner.requeue_auth_failures(changeset_ref)
    provider.next_create_outcome = "unknown_after_write"
    assert runner.run_due_once()
    event_id = next(iter(provider.events))
    monkeypatch.setattr(provider, "find_by_correlation", lambda request: [])
    assert runner.reconcile_once()
    assert runner.run_due_once()
    assert list(provider.events) == [event_id]
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation.status == "succeeded"
        assert operation.result["provider_event_id"] == event_id


def test_partial_delivery_recovery_retries_only_failed_operation(session_factory, monkeypatch):
    changeset_ref, operation_ids, keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"] * 3
    )
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    runner.requeue_auth_failures(changeset_ref)
    create = provider.create_event
    calls = []

    def fail_third(request):
        calls.append(request.external_event_id)
        if len(calls) == 3:
            raise CalendarProviderError(
                "google_auth_invalid", "Injected revoked authorization", transient=False
            )
        return create(request)

    monkeypatch.setattr(provider, "create_event", fail_third)
    assert all(runner.run_due_once() for _ in range(3))
    before = runner.auth_failure_recovery_status(changeset_ref)
    assert (before.succeeded, before.failed_auth, before.total) == (2, 1, 3)
    with session_factory() as session:
        status = HistoryService(session).get_entry(changeset_ref, view="delivery")
        assert status["canonical_disposition"] == "committed"
        assert status["provider_state_counts"] == {"confirmed": 2, "failed": 1}
        assert status["provider_operation_count"] == status["delivery_target_count"] == 3
        failed = [row for row in status["items"] if row["delivery_state"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["title"] == "Recovery event 3"
        assert failed[0]["timing"]["start_local"] == "2026-09-08T09:00:00"
        assert failed[0]["error_code"] == "google_auth_invalid"
        assert failed[0]["next_action"] == (
            "restore_provider_authorization_then_recover_same_operation"
        )
        assert status["next"]["restage_request"] is False
        assert "academic@example.com" not in json.dumps(status)
        assert "calendar-recovery-correlation" not in json.dumps(status)
        failed_ref = failed[0]["operation_ref"]
    recovered = OperationRunner(session_factory, provider).requeue_auth_failures(changeset_ref)
    assert recovered.requeued == 1
    assert runner.run_due_once()
    assert not runner.run_due_once()
    assert calls == [calls[0], calls[1], calls[2], calls[2]]
    assert len(provider.events) == 3
    with session_factory() as session:
        operations = [session.get(Operation, ref) for ref in operation_ids]
        assert [operation.idempotency_key for operation in operations] == keys
        assert all(operation.status == "succeeded" for operation in operations)
        assert [operation.attempt_count for operation in operations] == [2, 2, 3]
        assert session.scalar(select(func.count(ChangeSet.id))) == 1
        status = HistoryService(session).get_entry(changeset_ref, view="delivery")
        assert status["provider_state_counts"] == {"confirmed": 3}
        recovered_row = next(row for row in status["items"] if row["operation_ref"] == failed_ref)
        assert recovered_row["delivery_state"] == "confirmed"
        assert "error_code" not in recovered_row


def test_delivery_pages_scope_exact_counts_without_loading_other_requests(session_factory):
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"] * 30
    )
    with session_factory() as session:
        history = HistoryService(session)
        rows = []
        cursor = None
        while True:
            page = history.get_entry(changeset_ref, view="delivery", limit=7, cursor=cursor)
            assert page["provider_state_counts"] == {"failed": 30}
            assert page["provider_operation_count"] == page["delivery_target_count"] == 30
            assert 0 < page["count"] <= 7
            assert len(json.dumps(page, ensure_ascii=False).encode()) < 16 * 1024
            rows.extend(page["items"])
            assert page["omitted_target_count"] == 30 - len(rows)
            cursor = page.get("cursor")
            if cursor is None:
                break
        assert len(rows) == len({row["operation_ref"] for row in rows}) == 30
        assert {row["title"] for row in rows} == {f"Recovery event {n}" for n in range(1, 31)}
        assert all(session.get(Operation, ref).status == "failed" for ref in operation_ids)
        assert all(session.get(Operation, ref).attempt_count == 1 for ref in operation_ids)
        with pytest.raises(DocketError) as error:
            history.get_entry(changeset_ref, view="delivery", cursor="not-json")
        assert error.value.code == "invalid_delivery_cursor"
        with pytest.raises(DocketError) as error:
            history.get_entry(rows[0]["operation_ref"], view="delivery")
        assert error.value.code == "delivery_requires_changeset"


def test_delivery_no_provider_work_and_uncommitted_request_are_distinct(session_factory):
    changeset_ref, _operations, _keys = _seed_failed_operations(session_factory, [])
    with session_factory() as session:
        history = HistoryService(session)
        result = history.get_entry(changeset_ref, view="delivery")
        assert result["canonical_disposition"] == "committed"
        assert result["provider_operation_count"] == 0
        assert result["delivery_target_count"] == 0
        assert result["items"] == []
        assert not result["truncated"]
        draft = ChangeSet(
            intent_session_id=session.scalar(select(IntentSession.id)),
            intent_session_ref=session.scalar(select(IntentSession.ref_id)),
            idempotency_key="uncommitted-status-test", state="draft", basis_refs=[],
        )
        session.add(draft)
        session.flush()
        with pytest.raises(DocketError) as error:
            history.get_entry(draft.ref_id, view="delivery")
        assert error.value.code == "changeset_not_committed"


def test_mismatching_correlated_event_stays_in_reconciliation(session_factory):
    changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"]
    )
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    runner.requeue_auth_failures(changeset_ref)
    provider.next_create_outcome = "unknown_after_write"
    assert runner.run_due_once()
    event_id = next(iter(provider.events))
    original = provider.events[event_id]
    provider.events[event_id] = replace(
        original, snapshot={**original.snapshot, "summary": "Changed remotely"}
    )
    assert runner.reconcile_once()
    assert not runner.run_due_once()
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation.status == "reconciliation_required"
        assert operation.last_error_code == "reconciliation_ambiguous"
    assert len(provider.events) == 1


def test_unknown_create_without_pinned_identity_cannot_retry_on_absent_match(session_factory):
    _changeset_ref, operation_ids, _keys = _seed_failed_operations(
        session_factory, ["google_auth_invalid"]
    )
    with session_factory.begin() as session:
        operation = session.get(Operation, operation_ids[0])
        target = session.scalar(select(OperationTarget))
        target.parameters = {
            key: value for key, value in target.parameters.items() if key != "external_event_id"
        }
        target.parameters_sha256 = sha256_json(target.parameters)
        operation.status = target.status = "reconciliation_required"
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.reconcile_once()
    assert not runner.run_due_once()
    with session_factory() as session:
        operation = session.get(Operation, operation_ids[0])
        assert operation.status == "reconciliation_required"
        assert "external_event_id" not in session.scalar(select(OperationTarget)).parameters
    assert not provider.events
