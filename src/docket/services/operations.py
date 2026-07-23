import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.enums import (
    ActionStatus,
    AttemptKind,
    AttemptStatus,
    OperationStatus,
    OutboxStatus,
    QueueItemStatus,
)
from docket.domain.errors import DocketError
from docket.models import (
    Action,
    ActionRevision,
    AuditEvent,
    CalendarEventCache,
    CalendarLink,
    CalendarReminderPlan,
    CalendarSyncState,
    ExecutionAttempt,
    Operation,
    OutboxEvent,
    QueueItem,
    ReminderRule,
)
from docket.models.base import utc_now
from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarEventResult,
    CalendarProvider,
    CalendarProviderError,
    CalendarUnknownOutcome,
    event_matches_request,
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ClaimedOperation:
    operation_id: uuid.UUID
    attempt_id: uuid.UUID
    lease_token: uuid.UUID
    operation_type: str
    provider_correlation: str
    parameters: dict[str, Any]

    def calendar_request(self) -> CalendarEventRequest:
        event = self.parameters.get("event")
        event_spec = dict(event) if isinstance(event, dict) else None
        provider_before = self.parameters.get("provider_before")
        before = dict(provider_before) if isinstance(provider_before, dict) else {}
        summary = (
            str(event_spec["title"])
            if event_spec is not None
            else str(self.parameters.get("summary") or before.get("summary") or "Docket event")
        )
        schedule = self.parameters.get("schedule")
        reminder_plan = self.parameters.get("reminder_plan")
        return CalendarEventRequest(
            calendar_id=str(self.parameters["calendar_id"]),
            provider_correlation=self.provider_correlation,
            summary=summary,
            schedule=dict(schedule) if isinstance(schedule, dict) else None,
            external_event_id=self.parameters.get("external_event_id"),
            provider_etag=self.parameters.get("provider_etag"),
            event_spec=event_spec,
            reminder_plan=(
                dict(reminder_plan) if isinstance(reminder_plan, dict) else None
            ),
            logical_key=self.parameters.get("logical_key"),
            priority=str(self.parameters.get("priority", "normal")),
            priority_basis=str(self.parameters.get("priority_basis", "default")),
            reminder_plan_sha256=self.parameters.get("reminder_plan_sha256"),
            operation_type=self.operation_type,
        )


class OperationRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: CalendarProvider,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        consistency_window_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.consistency_window_seconds = consistency_window_seconds

    @staticmethod
    def _bound_entities(
        session: Session, operation: Operation
    ) -> tuple[ActionRevision, Action, QueueItem]:
        revision = session.get(ActionRevision, operation.action_revision_id)
        if revision is None:
            raise DocketError(code="invalid_operation_state", message="Action revision is missing.")
        action = session.get(Action, revision.action_id)
        if action is None or action.queue_item_id is None:
            raise DocketError(code="invalid_operation_state", message="Action state is missing.")
        queue_item = session.get(QueueItem, action.queue_item_id)
        if queue_item is None:
            raise DocketError(code="invalid_operation_state", message="Queue item is missing.")
        return revision, action, queue_item

    def _claim(self, session: Session, *, reconcile: bool) -> ClaimedOperation | None:
        now = utc_now()
        desired_status = (
            OperationStatus.RECONCILIATION_REQUIRED.value
            if reconcile
            else OperationStatus.PENDING.value
        )
        operation = session.scalar(
            select(Operation)
            .where(
                Operation.status == desired_status,
                or_(Operation.next_attempt_at.is_(None), Operation.next_attempt_at <= now),
                or_(Operation.leased_until.is_(None), Operation.leased_until < now),
            )
            .order_by(Operation.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if operation is None:
            return None
        revision, action, queue_item = self._bound_entities(session, operation)
        lease_token = uuid.uuid4()
        operation.lease_token = lease_token
        operation.leased_until = now + timedelta(seconds=self.lease_seconds)
        if not reconcile:
            operation.status = OperationStatus.RUNNING.value
            action.status = ActionStatus.EXECUTING.value
            queue_item.status = QueueItemStatus.EXECUTING.value
        operation.attempt_count += 1
        attempt = ExecutionAttempt(
            operation_id=operation.id,
            attempt_number=operation.attempt_count,
            kind=(AttemptKind.RECONCILE.value if reconcile else AttemptKind.EXECUTE.value),
            request_summary={
                "operation_type": operation.operation_type,
                "parameters_sha256": revision.parameters_sha256,
                "provider_correlation": operation.provider_correlation,
            },
            status=AttemptStatus.STARTED.value,
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        return ClaimedOperation(
            operation_id=operation.id,
            attempt_id=attempt.id,
            lease_token=lease_token,
            operation_type=operation.operation_type,
            provider_correlation=operation.provider_correlation,
            parameters=dict(revision.parameters),
        )

    def claim_due(self) -> ClaimedOperation | None:
        with self.session_factory.begin() as session:
            return self._claim(session, reconcile=False)

    def claim_reconciliation(self) -> ClaimedOperation | None:
        with self.session_factory.begin() as session:
            return self._claim(session, reconcile=True)

    def mark_provider_call_started(self, claim: ClaimedOperation) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if (
                operation is None
                or attempt is None
                or operation.lease_token != claim.lease_token
                or operation.status != OperationStatus.RUNNING.value
            ):
                raise DocketError(
                    code="operation_lease_lost", message="Operation execution lease was lost."
                )
            attempt.provider_request_id = f"call-started:{claim.lease_token}"

    @staticmethod
    def _upsert_calendar_cache(
        session: Session,
        operation: Operation,
        result: CalendarEventResult,
    ) -> None:
        revision, _action, _queue_item = OperationRunner._bound_entities(session, operation)
        parameters = revision.parameters
        calendar_id = str(parameters["calendar_id"])
        state = session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == operation.account_id,
                CalendarSyncState.calendar_id == calendar_id,
            )
        )
        generation = (
            state.snapshot_generation
            if state is not None and state.snapshot_generation is not None
            else uuid.uuid5(uuid.NAMESPACE_URL, f"calendar-write:{operation.id}")
        )
        snapshot = result.snapshot
        start = snapshot.get("start")
        end = snapshot.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise DocketError(
                code="calendar_cache_invalid_write_result",
                message="Calendar write result could not be normalized into the local cache.",
            )
        is_all_day = isinstance(start.get("date"), str)
        start_at: datetime | None = None
        end_at: datetime | None = None
        start_date: Any = None
        end_date: Any = None
        timezone: str | None = None
        try:
            if is_all_day:
                if not isinstance(end.get("date"), str):
                    raise ValueError
                from datetime import date as calendar_date

                start_date = calendar_date.fromisoformat(str(start["date"]))
                end_date = calendar_date.fromisoformat(str(end["date"]))
                event = parameters.get("event")
                timing = event.get("timing") if isinstance(event, dict) else None
                timezone = (
                    str(timing.get("timezone"))
                    if isinstance(timing, dict) and timing.get("timezone")
                    else get_settings().timezone
                )
                if end_date <= start_date:
                    raise ValueError
            else:
                if (
                    not isinstance(start.get("dateTime"), str)
                    or not isinstance(end.get("dateTime"), str)
                    or not isinstance(start.get("timeZone"), str)
                ):
                    raise ValueError
                timezone = str(start["timeZone"])
                zone = ZoneInfo(timezone)
                start_at = (
                    datetime.fromisoformat(str(start["dateTime"]))
                    .replace(tzinfo=zone)
                    .astimezone(UTC)
                )
                end_timezone_value = end.get("timeZone")
                end_timezone = (
                    end_timezone_value
                    if isinstance(end_timezone_value, str)
                    else timezone
                )
                end_at = (
                    datetime.fromisoformat(str(end["dateTime"]))
                    .replace(tzinfo=ZoneInfo(end_timezone))
                    .astimezone(UTC)
                )
                if end_at <= start_at:
                    raise ValueError
        except (ValueError, TypeError) as exc:
            raise DocketError(
                code="calendar_cache_invalid_write_result",
                message="Calendar write result contained invalid local time data.",
            ) from exc
        row = session.scalar(
            select(CalendarEventCache).where(
                CalendarEventCache.account_id == operation.account_id,
                CalendarEventCache.calendar_id == calendar_id,
                CalendarEventCache.provider_event_id == result.external_event_id,
            )
        )
        now = utc_now()
        if row is None:
            row = CalendarEventCache(
                account_id=operation.account_id,
                calendar_id=calendar_id,
                provider_event_id=result.external_event_id,
                snapshot_generation=generation,
                status="confirmed",
                is_all_day=False,
                synced_at=now,
            )
            session.add(row)
        row.snapshot_generation = generation
        row.status = "confirmed"
        summary = snapshot.get("summary")
        location = snapshot.get("location")
        row.summary = summary[:512] if isinstance(summary, str) and summary else None
        row.location = location[:1000] if isinstance(location, str) and location else None
        row.is_all_day = is_all_day
        row.start_at = start_at
        row.end_at = end_at
        row.start_date = start_date
        row.end_date = end_date
        row.timezone = timezone
        event = parameters.get("event")
        classification = revision.preview.get("classification", {})
        if isinstance(classification, dict):
            row.recurrence_kind = str(
                classification.get("recurrence_kind", "one_time")
            )
            row.system_tags = list(classification.get("system_tags", []))
            row.operator_tags = list(classification.get("operator_tags", []))
            row.priority = str(classification.get("priority", "normal"))
            row.priority_basis = str(
                classification.get("priority_basis", "default")
            )
        elif isinstance(event, dict):
            row.recurrence_kind = "recurring" if event.get("recurrence") else "one_time"
        row.has_attendees = False
        row.organizer_is_self = True
        reminders = snapshot.get("reminders")
        row.provider_reminders = dict(reminders) if isinstance(reminders, dict) else {}
        row.provider_etag = result.provider_etag
        row.synced_at = now
        session.flush()

    @staticmethod
    def _activate_reminder_plan(
        session: Session,
        operation: Operation,
        result: CalendarEventResult,
    ) -> None:
        revision, _action, _queue_item = OperationRunner._bound_entities(
            session, operation
        )
        plan = revision.parameters.get("reminder_plan")
        if not isinstance(plan, dict):
            return
        desired_leads = {int(value) for value in plan.get("lead_seconds", [])}
        rules = list(
            session.scalars(
                select(ReminderRule).where(
                    ReminderRule.account_id == operation.account_id,
                    ReminderRule.calendar_id == revision.parameters["calendar_id"],
                    ReminderRule.scope == "event",
                    ReminderRule.provider_event_id == result.external_event_id,
                    ReminderRule.source_kind == "canonical_plan",
                )
            )
        )
        selected: dict[int, ReminderRule] = {}
        for rule in rules:
            if rule.lead_seconds in desired_leads and rule.lead_seconds not in selected:
                selected[rule.lead_seconds] = rule
                if not rule.enabled:
                    rule.enabled = True
                    rule.version += 1
            elif rule.enabled:
                rule.enabled = False
                rule.version += 1
        for lead_seconds in sorted(desired_leads):
            if lead_seconds not in selected:
                rule = ReminderRule(
                    account_id=operation.account_id,
                    calendar_id=str(revision.parameters["calendar_id"]),
                    scope="event",
                    provider_event_id=result.external_event_id,
                    lead_seconds=lead_seconds,
                    queue_channel_id=get_settings().queue_channel_id,
                    source_kind="canonical_plan",
                    enabled=True,
                    created_by_actor_id=(
                        revision.created_by_actor_id
                        or get_settings().operator_discord_user_id
                    ),
                )
                session.add(rule)
                session.flush()
                selected[lead_seconds] = rule
        now = utc_now()
        plans = session.scalars(
            select(CalendarReminderPlan).where(
                CalendarReminderPlan.action_revision_id == revision.id
            )
        ).all()
        for planned in plans:
            matched_rule = selected.get(planned.lead_seconds)
            if matched_rule is None:
                planned.status = "cancelled"
                continue
            planned.reminder_rule_id = matched_rule.id
            planned.status = "activated"
            planned.provider_applied_at = now
        from docket.services.reminders import materialize_reminders

        materialize_reminders(
            session,
            now=now,
            rule_ids={rule.id for rule in rules} | {rule.id for rule in selected.values()},
        )

    @staticmethod
    def _apply_cancelled_cache(
        session: Session,
        operation: Operation,
        result: CalendarEventResult,
    ) -> None:
        revision, _action, _queue_item = OperationRunner._bound_entities(
            session, operation
        )
        row = session.scalar(
            select(CalendarEventCache).where(
                CalendarEventCache.account_id == operation.account_id,
                CalendarEventCache.calendar_id == revision.parameters["calendar_id"],
                CalendarEventCache.provider_event_id == result.external_event_id,
            )
        )
        if row is not None:
            row.status = "cancelled"
            row.provider_etag = None
            row.provider_reminders = {
                "useDefault": False,
                "overrides": [],
            }
            row.synced_at = utc_now()

    @staticmethod
    def _apply_success(
        session: Session,
        operation: Operation,
        attempt: ExecutionAttempt,
        result: CalendarEventResult,
    ) -> None:
        revision, action, queue_item = OperationRunner._bound_entities(session, operation)
        parameters = revision.parameters
        meeting_action = operation.operation_type in {
            "calendar_create_meeting",
            "calendar_update_meeting",
        }
        if meeting_action:
            link = session.scalar(
                select(CalendarLink).where(
                    CalendarLink.record_id
                    == uuid.UUID(str(parameters["record_id"])),
                    CalendarLink.meeting_id == parameters["meeting_id"],
                    CalendarLink.account_id == operation.account_id,
                    CalendarLink.calendar_id == parameters["calendar_id"],
                )
            )
        else:
            link = session.scalar(
                select(CalendarLink).where(
                    CalendarLink.account_id == operation.account_id,
                    CalendarLink.calendar_id == parameters["calendar_id"],
                    CalendarLink.logical_key == parameters["logical_key"],
                )
            )
        if operation.operation_type == "calendar_cancel_event":
            if link is not None:
                link.provider_etag = None
                link.provider_correlation = operation.provider_correlation
                link.reminder_plan_sha256 = parameters.get(
                    "reminder_plan_sha256"
                )
                link.synced_snapshot = result.snapshot
            OperationRunner._apply_cancelled_cache(session, operation, result)
            OperationRunner._activate_reminder_plan(session, operation, result)
        else:
            if link is None:
                classification = revision.preview.get("classification", {})
                if not isinstance(classification, dict):
                    classification = {}
                link = CalendarLink(
                    record_id=(
                        uuid.UUID(str(parameters["record_id"]))
                        if meeting_action
                        else None
                    ),
                    meeting_id=(
                        str(parameters["meeting_id"]) if meeting_action else None
                    ),
                    origin_kind=(
                        "course_meeting"
                        if meeting_action
                        else "standalone"
                        if operation.operation_type == "calendar_create_event"
                        else "adopted_provider_event"
                    ),
                    logical_key=(
                        f"course:{parameters['record_id']}:{parameters['meeting_id']}"
                        if meeting_action
                        else str(parameters["logical_key"])
                    ),
                    account_id=operation.account_id,
                    calendar_id=str(parameters["calendar_id"]),
                    external_event_id=result.external_event_id,
                    provider_etag=result.provider_etag,
                    provider_correlation=operation.provider_correlation,
                    last_synced_version=(
                        int(parameters["record_version"])
                        if meeting_action
                        else revision.revision
                    ),
                    recurrence_kind=str(
                        classification.get(
                            "recurrence_kind",
                            "recurring" if meeting_action else "one_time",
                        )
                    ),
                    system_tags=list(
                        classification.get(
                            "system_tags",
                            ["recurring", "timed", "course_meeting"]
                            if meeting_action
                            else [],
                        )
                    ),
                    operator_tags=list(
                        classification.get("operator_tags", [])
                    ),
                    priority=str(parameters.get("priority", "normal")),
                    priority_basis=str(
                        parameters.get("priority_basis", "default")
                    ),
                    reminder_plan_sha256=parameters.get(
                        "reminder_plan_sha256"
                    ),
                    synced_snapshot=result.snapshot,
                )
                session.add(link)
                session.flush()
            else:
                if (
                    operation.operation_type
                    in {
                        "calendar_update_meeting",
                        "calendar_update_event",
                        "calendar_update_reminders",
                    }
                    and link.external_event_id != result.external_event_id
                ):
                    raise DocketError(
                        code="calendar_link_conflict",
                        message="Calendar update returned a different external event ID.",
                    )
                link.external_event_id = result.external_event_id
                link.provider_etag = result.provider_etag
                link.provider_correlation = operation.provider_correlation
                link.last_synced_version = (
                    int(parameters["record_version"])
                    if meeting_action
                    else revision.revision
                )
                link.synced_snapshot = result.snapshot
                if parameters.get("reminder_plan_sha256") is not None:
                    link.reminder_plan_sha256 = parameters[
                        "reminder_plan_sha256"
                    ]
            OperationRunner._upsert_calendar_cache(session, operation, result)
            OperationRunner._activate_reminder_plan(session, operation, result)
        attempt.status = AttemptStatus.SUCCEEDED.value
        attempt.provider_request_id = result.provider_request_id or attempt.provider_request_id
        attempt.response_summary = {
            "external_event_id": result.external_event_id,
            "provider_etag": result.provider_etag,
        }
        attempt.completed_at = utc_now()
        operation.status = OperationStatus.SUCCEEDED.value
        operation.lease_token = None
        operation.leased_until = None
        operation.next_attempt_at = None
        operation.result = {
            "calendar_link_id": str(link.id) if link is not None else None,
            "external_event_id": result.external_event_id,
            "record_version": (
                int(parameters["record_version"]) if meeting_action else None
            ),
        }
        operation.last_error_code = None
        operation.last_error_message = None
        action.status = ActionStatus.SUCCEEDED.value
        queue_item.status = QueueItemStatus.COMPLETED.value
        queue_item.resolved_at = utc_now()
        queue_item.resolution_code = (
            "calendar_cancelled"
            if operation.operation_type == "calendar_cancel_event"
            else "calendar_synchronized"
        )
        queue_item.version += 1
        session.add(
            AuditEvent(
                event_type="operation.succeeded",
                entity_type="operation",
                entity_id=operation.id,
                actor_type="docket",
                actor_id=None,
                request_id=None,
                data={
                    "action_revision_id": str(revision.id),
                    "attempt_id": str(attempt.id),
                    "external_event_id": result.external_event_id,
                    "parameters_sha256": revision.parameters_sha256,
                },
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=f"discord_projection:{queue_item.id}:operation:{operation.id}:ok",
                payload={
                    "queue_item_id": str(queue_item.id),
                    "action_id": str(action.id),
                    "operation_id": str(operation.id),
                    "status": "succeeded",
                },
                status=OutboxStatus.PENDING.value,
            )
        )

    def finish_success(self, claim: ClaimedOperation, result: CalendarEventResult) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None or operation.lease_token != claim.lease_token:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            self._apply_success(session, operation, attempt, result)

    def finish_error(self, claim: ClaimedOperation, error: CalendarProviderError) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None or operation.lease_token != claim.lease_token:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            _, action, queue_item = self._bound_entities(session, operation)
            now = utc_now()
            attempt.status = (
                AttemptStatus.UNKNOWN.value
                if isinstance(error, CalendarUnknownOutcome)
                else AttemptStatus.FAILED.value
            )
            attempt.error_code = error.code
            attempt.error_message = error.safe_message
            attempt.completed_at = now
            operation.lease_token = None
            operation.leased_until = None
            operation.last_error_code = error.code
            operation.last_error_message = error.safe_message
            if isinstance(error, CalendarUnknownOutcome):
                operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
                operation.next_attempt_at = now
                action.status = ActionStatus.RECONCILIATION_REQUIRED.value
                queue_item.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
            elif error.transient and operation.attempt_count < self.max_attempts:
                operation.status = OperationStatus.PENDING.value
                operation.next_attempt_at = now + timedelta(
                    seconds=min(300, 2**operation.attempt_count)
                )
                action.status = ActionStatus.READY.value
            else:
                operation.status = OperationStatus.FAILED.value
                operation.next_attempt_at = None
                action.status = ActionStatus.FAILED.value
                queue_item.status = QueueItemStatus.FAILED.value
            queue_item.version += 1

    def run_due_once(self) -> bool:
        claim = self.claim_due()
        if claim is None:
            return False
        self.mark_provider_call_started(claim)
        request = claim.calendar_request()
        try:
            if claim.operation_type in {
                "calendar_create_meeting",
                "calendar_create_event",
            }:
                result = self.provider.create_event(request)
            elif claim.operation_type in {
                "calendar_update_meeting",
                "calendar_update_event",
                "calendar_update_reminders",
            }:
                result = self.provider.update_event(request)
            elif claim.operation_type == "calendar_cancel_event":
                result = self.provider.cancel_event(request)
            else:
                raise CalendarProviderError(
                    "unsupported_operation",
                    "No provider handler exists for this operation.",
                    transient=False,
                )
        except CalendarProviderError as exc:
            self.finish_error(claim, exc)
        else:
            self.finish_success(claim, result)
        return True

    def recover_expired_leases(self) -> int:
        recovered = 0
        now = utc_now()
        with self.session_factory.begin() as session:
            operations = list(
                session.scalars(
                    select(Operation)
                    .where(
                        Operation.status == OperationStatus.RUNNING.value,
                        Operation.leased_until < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for operation in operations:
                _, action, queue_item = self._bound_entities(session, operation)
                attempt = session.scalar(
                    select(ExecutionAttempt)
                    .where(ExecutionAttempt.operation_id == operation.id)
                    .order_by(ExecutionAttempt.attempt_number.desc())
                    .limit(1)
                )
                if attempt is None:
                    operation.status = OperationStatus.PENDING.value
                    action.status = ActionStatus.READY.value
                elif attempt.provider_request_id is None:
                    attempt.status = AttemptStatus.FAILED.value
                    attempt.error_code = "worker_crash_before_provider_call"
                    attempt.completed_at = now
                    operation.status = OperationStatus.PENDING.value
                    action.status = ActionStatus.READY.value
                else:
                    attempt.status = AttemptStatus.UNKNOWN.value
                    attempt.error_code = "worker_crash_after_provider_call_started"
                    attempt.completed_at = now
                    operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
                    action.status = ActionStatus.RECONCILIATION_REQUIRED.value
                    queue_item.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
                operation.lease_token = None
                operation.leased_until = None
                operation.next_attempt_at = now
                recovered += 1
        return recovered

    def _defer_reconciliation(
        self, claim: ClaimedOperation, *, error_code: str, delay_seconds: int
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None or operation.lease_token != claim.lease_token:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_code = error_code
            attempt.completed_at = utc_now()
            operation.lease_token = None
            operation.leased_until = None
            operation.next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)

    def _finish_reconciliation_no_match(self, claim: ClaimedOperation) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None or operation.lease_token != claim.lease_token:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            _, action, queue_item = self._bound_entities(session, operation)
            now = utc_now()
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.response_summary = {"matches": 0}
            attempt.completed_at = now
            operation.status = OperationStatus.PENDING.value
            operation.lease_token = None
            operation.leased_until = None
            operation.next_attempt_at = now
            action.status = ActionStatus.READY.value
            queue_item.status = QueueItemStatus.EXECUTING.value
            queue_item.version += 1

    def _finish_reconciliation_conflict(self, claim: ClaimedOperation, *, match_count: int) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None or operation.lease_token != claim.lease_token:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            _, _, queue_item = self._bound_entities(session, operation)
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_code = "calendar_reconciliation_conflict"
            attempt.response_summary = {"matches": match_count}
            attempt.completed_at = utc_now()
            operation.lease_token = None
            operation.leased_until = None
            operation.next_attempt_at = utc_now() + timedelta(minutes=5)
            session.add(
                OutboxEvent(
                    event_type="system.alert.requested",
                    aggregate_type="operation",
                    aggregate_id=operation.id,
                    deduplication_key=(
                        f"system_alert:operation:{operation.id}:reconcile:{operation.attempt_count}"
                    ),
                    payload={
                        "operation_id": str(operation.id),
                        "queue_item_id": str(queue_item.id),
                        "error_code": "calendar_reconciliation_conflict",
                        "match_count": match_count,
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )

    def reconcile_once(self) -> bool:
        claim = self.claim_reconciliation()
        if claim is None:
            return False
        request = claim.calendar_request()
        try:
            if claim.operation_type in {
                "calendar_create_meeting",
                "calendar_create_event",
            }:
                matches = self.provider.find_by_correlation(request)
            else:
                current = self.provider.get_event(request)
                matches = [current] if current is not None else []
        except CalendarProviderError as exc:
            if exc.transient:
                self._defer_reconciliation(claim, error_code=exc.code, delay_seconds=8)
            else:
                self._finish_reconciliation_conflict(claim, match_count=-1)
            return True
        if claim.operation_type == "calendar_cancel_event":
            if not matches or matches[0].snapshot.get("status") == "cancelled":
                cancelled = (
                    matches[0]
                    if matches
                    else CalendarEventResult(
                        external_event_id=str(request.external_event_id),
                        provider_etag=None,
                        provider_request_id=None,
                        snapshot={**request.snapshot(), "status": "cancelled"},
                    )
                )
                self.finish_success(claim, cancelled)
                return True
            current = matches[0]
            if current.provider_etag != request.provider_etag:
                self._finish_reconciliation_conflict(claim, match_count=1)
                return True
            with self.session_factory() as session:
                operation = session.get(Operation, claim.operation_id)
                assert operation is not None
                age = utc_now() - _as_utc(operation.updated_at)
            if age.total_seconds() >= self.consistency_window_seconds:
                self._finish_reconciliation_no_match(claim)
            else:
                remaining = max(
                    1,
                    self.consistency_window_seconds - int(age.total_seconds()),
                )
                self._defer_reconciliation(
                    claim,
                    error_code="calendar_consistency_window",
                    delay_seconds=remaining,
                )
            return True
        exact = [match for match in matches if event_matches_request(match, request)]
        if len(matches) == 1 and len(exact) == 1:
            self.finish_success(claim, exact[0])
        elif len(matches) == 0:
            if claim.operation_type in {
                "calendar_update_meeting",
                "calendar_update_event",
                "calendar_update_reminders",
            }:
                self._finish_reconciliation_conflict(claim, match_count=0)
                return True
            with self.session_factory() as session:
                operation = session.get(Operation, claim.operation_id)
                assert operation is not None
                age = utc_now() - _as_utc(operation.updated_at)
            if age.total_seconds() >= self.consistency_window_seconds:
                self._finish_reconciliation_no_match(claim)
            else:
                remaining = max(1, self.consistency_window_seconds - int(age.total_seconds()))
                self._defer_reconciliation(
                    claim,
                    error_code="calendar_consistency_window",
                    delay_seconds=remaining,
                )
        else:
            self._finish_reconciliation_conflict(claim, match_count=len(matches))
        return True
