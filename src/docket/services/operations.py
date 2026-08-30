from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.domain.canonical import sha256_json
from docket.domain.enums import AttemptKind, AttemptStatus, OperationStatus
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CalendarEventCache,
    CalendarLane,
    ExecutionAttempt,
    Operation,
    OperationTarget,
    ProviderEventBinding,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarEventResult,
    CalendarLaneDeleteResult,
    CalendarLaneProviderResult,
    CalendarLaneRequest,
    CalendarProvider,
    CalendarProviderError,
    CalendarUnknownOutcome,
    event_matches_request,
)


@dataclass(frozen=True, slots=True)
class ClaimedOperation:
    operation_id: uuid.UUID
    operation_target_id: uuid.UUID
    attempt_id: uuid.UUID
    lease_token: uuid.UUID
    operation_type: str
    provider_correlation: str
    parameters: dict[str, Any]
    attempt_number: int

    def calendar_request(self) -> CalendarEventRequest:
        event = self.parameters.get("event")
        event_spec = dict(event) if isinstance(event, dict) else None
        before = self.parameters.get("provider_before")
        provider_before = dict(before) if isinstance(before, dict) else {}
        summary = (
            str(event_spec["title"])
            if event_spec is not None
            else str(provider_before.get("summary") or "Docket event")
        )
        plan = self.parameters.get("reminder_plan")
        return CalendarEventRequest(
            calendar_id=str(self.parameters["calendar_id"]),
            provider_correlation=self.provider_correlation,
            summary=summary,
            external_event_id=self.parameters.get("external_event_id"),
            provider_etag=self.parameters.get("provider_etag"),
            event_spec=event_spec,
            reminder_plan=dict(plan) if isinstance(plan, dict) else None,
            logical_key=self.parameters.get("logical_key"),
            priority=str(self.parameters.get("priority", "normal")),
            priority_basis=str(self.parameters.get("priority_basis", "explicit_operator")),
            reminder_plan_sha256=self.parameters.get("reminder_plan_sha256"),
            origin_kind=(
                "temporal_projection"
                if self.parameters.get("target_kind") == "temporal_projection"
                else "canonical_event"
            ),
            operation_type=self.operation_type,
        )

    def lane_request(self, *, create_if_missing: bool) -> CalendarLaneRequest:
        return CalendarLaneRequest(
            lane=str(self.parameters["lane"]),
            display_name=str(self.parameters["display_name"]),
            color_hex=str(self.parameters["color_hex"]),
            timezone=str(self.parameters["timezone"]),
            calendar_id=self.parameters.get("calendar_id"),
            create_if_missing=create_if_missing,
        )


class OperationRunner:
    """Execute and reconcile clean provider Operations without proposal rows."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: CalendarProvider,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        consistency_window_seconds: int = 30,
        execution_enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.consistency_window_seconds = consistency_window_seconds
        self.execution_enabled = execution_enabled

    @staticmethod
    def _target(session: Session, operation: Operation) -> OperationTarget:
        targets = list(
            session.scalars(
                select(OperationTarget).where(
                    OperationTarget.operation_id == operation.id
                )
            )
        )
        if len(targets) != 1:
            raise DocketError(
                code="invalid_operation_state",
                message="A clean provider Operation requires exactly one target.",
                details={"operation_ref": operation.ref_id, "target_count": len(targets)},
            )
        return targets[0]

    @staticmethod
    def _resolved_parameters(
        session: Session,
        operation: Operation,
        target: OperationTarget,
    ) -> dict[str, Any]:
        parameters = dict(target.parameters)
        lane_ref = parameters.get("lane_ref")
        if (
            operation.operation_type
            in {
                "calendar_create_event",
                "calendar_update_event",
                "calendar_update_reminders",
                "calendar_cancel_event",
            }
            and isinstance(lane_ref, str)
            and not parameters.get("calendar_id")
        ):
            lane = session.scalar(
                select(CalendarLane).where(CalendarLane.ref_id == lane_ref)
            )
            if lane is None or lane.status != "active" or lane.calendar_id is None:
                raise DocketError(
                    code="calendar_lane_provider_binding_unresolved",
                    message="Provider operation is waiting for its lane binding.",
                    details={"lane_ref": lane_ref},
                )
            parameters["calendar_id"] = lane.calendar_id
        return parameters

    def _claim(self, session: Session, *, reconcile: bool) -> ClaimedOperation | None:
        now = utc_now()
        desired = (
            OperationStatus.RECONCILIATION_REQUIRED.value
            if reconcile
            else OperationStatus.PENDING.value
        )
        operation = session.scalar(
            select(Operation)
            .where(
                Operation.status == desired,
                or_(Operation.next_attempt_at.is_(None), Operation.next_attempt_at <= now),
                or_(Operation.leased_until.is_(None), Operation.leased_until < now),
                or_(
                    Operation.predecessor_operation_id.is_(None),
                    Operation.predecessor_operation_id.in_(
                        select(Operation.id).where(
                            Operation.status == OperationStatus.SUCCEEDED.value
                        )
                    ),
                ),
            )
            .order_by(Operation.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if operation is None:
            return None
        target = self._target(session, operation)
        parameters = self._resolved_parameters(session, operation, target)
        if parameters != target.parameters:
            target.parameters = parameters
            target.parameters_sha256 = sha256_json(parameters)
        lease_token = uuid.uuid4()
        operation.lease_token = lease_token
        operation.leased_until = now + timedelta(seconds=self.lease_seconds)
        operation.status = OperationStatus.RUNNING.value
        target.lease_token = lease_token
        target.leased_until = operation.leased_until
        target.status = "running"
        operation.attempt_count += 1
        target.attempt_count += 1
        attempt = ExecutionAttempt(
            operation_id=operation.id,
            operation_target_id=target.id,
            attempt_number=target.attempt_count,
            kind=(AttemptKind.RECONCILE.value if reconcile else AttemptKind.EXECUTE.value),
            request_summary={
                "operation_type": operation.operation_type,
                "parameters_sha256": target.parameters_sha256,
                "provider_correlation": operation.provider_correlation,
                "target_ref": target.canonical_target_ref,
            },
            status=AttemptStatus.STARTED.value,
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        return ClaimedOperation(
            operation_id=operation.id,
            operation_target_id=target.id,
            attempt_id=attempt.id,
            lease_token=lease_token,
            operation_type=operation.operation_type,
            provider_correlation=operation.provider_correlation,
            parameters=parameters,
            attempt_number=attempt.attempt_number,
        )

    def claim_due(self) -> ClaimedOperation | None:
        if not self.execution_enabled:
            return None
        with self.session_factory.begin() as session:
            return self._claim(session, reconcile=False)

    def claim_reconciliation(self) -> ClaimedOperation | None:
        if not self.execution_enabled:
            return None
        with self.session_factory.begin() as session:
            return self._claim(session, reconcile=True)

    def mark_provider_call_started(self, claim: ClaimedOperation) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if (
                operation is None
                or target is None
                or attempt is None
                or operation.lease_token != claim.lease_token
                or target.lease_token != claim.lease_token
                or operation.status != OperationStatus.RUNNING.value
                or target.status != "running"
            ):
                raise DocketError(
                    code="operation_lease_lost",
                    message="Provider Operation execution lease was lost.",
                )
            attempt.provider_request_id = f"call-started:{claim.lease_token}"

    @staticmethod
    def _clear_lease(operation: Operation, target: OperationTarget) -> None:
        operation.lease_token = None
        operation.leased_until = None
        target.lease_token = None
        target.leased_until = None

    @staticmethod
    def _audit(
        session: Session,
        operation: Operation,
        target: OperationTarget,
        event_type: str,
    ) -> None:
        session.add(
            AuditEvent(
                event_type=event_type,
                entity_type="operation",
                entity_id=operation.id,
                actor_type="docket_worker",
                actor_id=None,
                primary_ref=operation.ref_id,
                affected_refs=[operation.ref_id, target.canonical_target_ref],
                basis_refs=list(operation.basis_refs),
                data={
                    "operation_type": operation.operation_type,
                    "target_ref": target.canonical_target_ref,
                    "attempt_count": operation.attempt_count,
                },
            )
        )

    @staticmethod
    def _upsert_binding(
        session: Session,
        operation: Operation,
        target: OperationTarget,
        result: CalendarEventResult,
    ) -> None:
        calendar_id = str(target.parameters["calendar_id"])
        binding = session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.canonical_target_ref
                == target.canonical_target_ref,
                ProviderEventBinding.account_id == operation.account_id,
                ProviderEventBinding.calendar_id == calendar_id,
            )
        )
        if binding is None:
            binding = ProviderEventBinding(
                canonical_target_ref=target.canonical_target_ref,
                target_kind=target.target_kind,
                account_id=operation.account_id,
                calendar_id=calendar_id,
                provider_event_id=result.external_event_id,
                status="active",
                version=1,
            )
            session.add(binding)
        else:
            binding.version += 1
        binding.provider_event_id = result.external_event_id
        binding.provider_etag = result.provider_etag
        binding.provider_snapshot = dict(result.snapshot)
        binding.status = (
            "cancelled"
            if operation.operation_type == "calendar_cancel_event"
            else "active"
        )

    @staticmethod
    def _upsert_cache(
        session: Session,
        operation: Operation,
        target: OperationTarget,
        result: CalendarEventResult,
    ) -> None:
        calendar_id = str(target.parameters["calendar_id"])
        row = session.scalar(
            select(CalendarEventCache).where(
                CalendarEventCache.account_id == operation.account_id,
                CalendarEventCache.calendar_id == calendar_id,
                CalendarEventCache.provider_event_id == result.external_event_id,
            )
        )
        if row is None:
            row = CalendarEventCache(
                account_id=operation.account_id,
                calendar_id=calendar_id,
                provider_event_id=result.external_event_id,
                snapshot_generation=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"calendar-write:{operation.id}"
                ),
                status="confirmed",
                is_all_day=False,
                synced_at=utc_now(),
            )
            session.add(row)
        snapshot = result.snapshot
        start = snapshot.get("start")
        end = snapshot.get("end")
        row.event_type = str(snapshot.get("event_type") or "default")
        row.status = (
            "cancelled"
            if operation.operation_type == "calendar_cancel_event"
            else "confirmed"
        )
        row.summary = (
            str(snapshot["summary"])[:512] if snapshot.get("summary") else None
        )
        row.location = (
            str(snapshot["location"])[:1000] if snapshot.get("location") else None
        )
        row.is_all_day = isinstance(start, dict) and isinstance(start.get("date"), str)
        row.start_at = row.end_at = None
        row.start_date = row.end_date = None
        if row.is_all_day and isinstance(start, dict) and isinstance(end, dict):
            row.start_date = date.fromisoformat(str(start["date"]))
            row.end_date = date.fromisoformat(str(end["date"]))
        elif isinstance(start, dict) and isinstance(end, dict):
            start_at = datetime.fromisoformat(
                str(start["dateTime"]).replace("Z", "+00:00")
            )
            end_at = datetime.fromisoformat(
                str(end["dateTime"]).replace("Z", "+00:00")
            )
            if start_at.tzinfo is None:
                start_at = start_at.replace(
                    tzinfo=ZoneInfo(str(start.get("timeZone") or "UTC"))
                )
            if end_at.tzinfo is None:
                end_at = end_at.replace(
                    tzinfo=ZoneInfo(str(end.get("timeZone") or "UTC"))
                )
            row.start_at = start_at.astimezone(UTC)
            row.end_at = end_at.astimezone(UTC)
        row.timezone = (
            str(start.get("timeZone"))
            if isinstance(start, dict) and start.get("timeZone")
            else None
        )
        row.provider_reminders = (
            dict(snapshot["reminders"])
            if isinstance(snapshot.get("reminders"), dict)
            else {}
        )
        row.provider_etag = result.provider_etag
        row.synced_at = utc_now()

    def _finish_event_success(
        self,
        claim: ClaimedOperation,
        result: CalendarEventResult,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or target is None or attempt is None:
                raise DocketError(
                    code="invalid_operation_state",
                    message="Provider Operation state disappeared during execution.",
                )
            self._upsert_binding(session, operation, target, result)
            self._upsert_cache(session, operation, target, result)
            operation.status = OperationStatus.SUCCEEDED.value
            operation.result = {
                "provider_event_id": result.external_event_id,
                "provider_request_id": result.provider_request_id,
            }
            target.status = "succeeded"
            target.result = dict(operation.result)
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.provider_request_id = result.provider_request_id
            attempt.response_summary = {
                "provider_event_id": result.external_event_id,
                "provider_etag_present": result.provider_etag is not None,
            }
            attempt.completed_at = utc_now()
            self._clear_lease(operation, target)
            self._audit(session, operation, target, "operation.succeeded")

    def _finish_lane_success(
        self,
        claim: ClaimedOperation,
        result: CalendarLaneProviderResult | CalendarLaneDeleteResult,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or target is None or attempt is None:
                raise DocketError(
                    code="invalid_operation_state",
                    message="Provider Operation state disappeared during execution.",
                )
            lane = session.scalar(
                select(CalendarLane).where(
                    CalendarLane.ref_id == target.canonical_target_ref
                )
            )
            if lane is None:
                raise DocketError(
                    code="calendar_lane_unresolved",
                    message="Provider Operation lost its CalendarLane target.",
                )
            if operation.operation_type == "calendar_delete_lane":
                lane.status = "deleted"
                lane.enabled = False
            else:
                lane.calendar_id = result.calendar_id
                lane.status = "active"
                lane.enabled = True
            lane.last_error_code = None
            lane.version += 1
            operation.status = OperationStatus.SUCCEEDED.value
            operation.result = {
                "calendar_id": result.calendar_id,
                "provider_request_id": result.provider_request_id,
            }
            target.status = "succeeded"
            target.result = dict(operation.result)
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.provider_request_id = result.provider_request_id
            attempt.response_summary = {"calendar_id": result.calendar_id}
            attempt.completed_at = utc_now()
            self._clear_lease(operation, target)
            self._audit(session, operation, target, "operation.succeeded")

    def _finish_error(
        self,
        claim: ClaimedOperation,
        error: CalendarProviderError,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or target is None or attempt is None:
                return
            retry = error.transient and operation.attempt_count < self.max_attempts
            operation.status = (
                OperationStatus.PENDING.value
                if retry
                else OperationStatus.FAILED.value
            )
            target.status = "pending" if retry else "failed"
            operation.last_error_code = error.code
            operation.last_error_message = error.safe_message
            target.last_error_code = error.code
            delay = min(300, 2 ** max(0, operation.attempt_count - 1))
            operation.next_attempt_at = (
                utc_now() + timedelta(seconds=delay) if retry else None
            )
            target.next_attempt_at = operation.next_attempt_at
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_code = error.code
            attempt.error_message = error.safe_message
            attempt.completed_at = utc_now()
            self._clear_lease(operation, target)
            self._audit(
                session,
                operation,
                target,
                "operation.retry_scheduled" if retry else "operation.failed",
            )

    def _finish_unknown(self, claim: ClaimedOperation, message: str) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or target is None or attempt is None:
                return
            operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
            target.status = "reconciliation_required"
            operation.last_error_code = "calendar_unknown_outcome"
            operation.last_error_message = message
            target.last_error_code = "calendar_unknown_outcome"
            operation.next_attempt_at = utc_now()
            target.next_attempt_at = operation.next_attempt_at
            attempt.status = AttemptStatus.UNKNOWN.value
            attempt.error_code = "calendar_unknown_outcome"
            attempt.error_message = message
            attempt.completed_at = utc_now()
            self._clear_lease(operation, target)
            self._audit(session, operation, target, "operation.reconciliation_required")

    def _execute(self, claim: ClaimedOperation) -> None:
        self.mark_provider_call_started(claim)
        if claim.operation_type == "calendar_configure_lane":
            result = self.provider.ensure_calendar_lane(
                claim.lane_request(create_if_missing=True)
            )
            self._finish_lane_success(claim, result)
            return
        if claim.operation_type == "calendar_delete_lane":
            result = self.provider.delete_calendar_lane(
                claim.lane_request(create_if_missing=False)
            )
            self._finish_lane_success(claim, result)
            return
        request = claim.calendar_request()
        if claim.operation_type == "calendar_create_event":
            result = self.provider.create_event(request)
        elif claim.operation_type in {
            "calendar_update_event",
            "calendar_update_reminders",
        }:
            result = self.provider.update_event(request)
        elif claim.operation_type == "calendar_cancel_event":
            result = self.provider.cancel_event(request)
        else:  # model constraint prevents this branch
            raise DocketError(
                code="provider_operation_not_supported",
                message="Provider Operation type is unsupported.",
            )
        self._finish_event_success(claim, result)

    def run_due_once(self) -> bool:
        claim = self.claim_due()
        if claim is None:
            return False
        try:
            self._execute(claim)
        except CalendarUnknownOutcome as exc:
            self._finish_unknown(claim, exc.safe_message)
        except CalendarProviderError as exc:
            self._finish_error(claim, exc)
        return True

    def _finish_no_reconciliation_match(self, claim: ClaimedOperation) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or target is None or attempt is None:
                return
            operation.status = OperationStatus.PENDING.value
            target.status = "pending"
            operation.next_attempt_at = utc_now()
            target.next_attempt_at = operation.next_attempt_at
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_code = "reconciliation_no_match"
            attempt.completed_at = utc_now()
            self._clear_lease(operation, target)
            self._audit(session, operation, target, "operation.reconciliation_no_match")

    def _finish_reconciliation_conflict(
        self,
        claim: ClaimedOperation,
        match_count: int,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            target = session.get(OperationTarget, claim.operation_target_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or target is None or attempt is None:
                return
            operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
            target.status = "reconciliation_required"
            operation.last_error_code = "reconciliation_ambiguous"
            target.last_error_code = "reconciliation_ambiguous"
            operation.next_attempt_at = utc_now() + timedelta(minutes=5)
            target.next_attempt_at = operation.next_attempt_at
            attempt.status = AttemptStatus.UNKNOWN.value
            attempt.error_code = "reconciliation_ambiguous"
            attempt.response_summary = {"match_count": match_count}
            attempt.completed_at = utc_now()
            self._clear_lease(operation, target)
            self._audit(session, operation, target, "operation.reconciliation_ambiguous")

    def reconcile_once(self) -> bool:
        claim = self.claim_reconciliation()
        if claim is None:
            return False
        try:
            if claim.operation_type in {
                "calendar_configure_lane",
                "calendar_delete_lane",
            }:
                self._execute(claim)
                return True
            self.mark_provider_call_started(claim)
            request = claim.calendar_request()
            if claim.operation_type == "calendar_create_event":
                matches = [
                    item
                    for item in self.provider.find_by_correlation(request)
                    if event_matches_request(item, request)
                ]
                if len(matches) == 1:
                    self._finish_event_success(claim, matches[0])
                elif not matches:
                    self._finish_no_reconciliation_match(claim)
                else:
                    self._finish_reconciliation_conflict(claim, len(matches))
                return True
            result = self.provider.get_event(request)
            if claim.operation_type == "calendar_cancel_event" and result is None:
                result = CalendarEventResult(
                    external_event_id=str(request.external_event_id),
                    provider_etag=None,
                    provider_request_id=None,
                    snapshot={**request.snapshot(), "status": "cancelled"},
                )
            if result is not None and event_matches_request(result, request):
                self._finish_event_success(claim, result)
            elif result is None:
                self._finish_no_reconciliation_match(claim)
            else:
                self._finish_reconciliation_conflict(claim, 1)
        except CalendarUnknownOutcome as exc:
            self._finish_unknown(claim, exc.safe_message)
        except CalendarProviderError as exc:
            self._finish_error(claim, exc)
        return True

    def recover_expired_leases(self) -> int:
        now = utc_now()
        recovered = 0
        with self.session_factory.begin() as session:
            operations = list(
                session.scalars(
                    select(Operation)
                    .where(
                        Operation.status == OperationStatus.RUNNING.value,
                        Operation.leased_until.is_not(None),
                        Operation.leased_until < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for operation in operations:
                target = self._target(session, operation)
                attempt = session.scalar(
                    select(ExecutionAttempt)
                    .where(
                        ExecutionAttempt.operation_id == operation.id,
                        ExecutionAttempt.status == AttemptStatus.STARTED.value,
                    )
                    .order_by(ExecutionAttempt.attempt_number.desc())
                    .limit(1)
                )
                provider_started = bool(
                    attempt is not None
                    and attempt.provider_request_id
                    and attempt.provider_request_id.startswith("call-started:")
                )
                if provider_started:
                    operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
                    target.status = "reconciliation_required"
                    if attempt is not None:
                        attempt.status = AttemptStatus.UNKNOWN.value
                        attempt.error_code = "execution_interrupted"
                        attempt.completed_at = now
                else:
                    operation.status = OperationStatus.PENDING.value
                    target.status = "pending"
                    if attempt is not None:
                        attempt.status = AttemptStatus.FAILED.value
                        attempt.error_code = "lease_expired_before_provider_call"
                        attempt.completed_at = now
                operation.next_attempt_at = now
                target.next_attempt_at = now
                self._clear_lease(operation, target)
                self._audit(session, operation, target, "operation.lease_recovered")
                recovered += 1
        return recovered

    def pending_count(self) -> int:
        with self.session_factory() as session:
            return len(
                list(
                    session.scalars(
                        select(Operation.id).where(
                            Operation.status.in_(
                                {
                                    OperationStatus.PENDING.value,
                                    OperationStatus.RUNNING.value,
                                    OperationStatus.RECONCILIATION_REQUIRED.value,
                                }
                            )
                        )
                    )
                )
            )
