import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.canonical import sha256_json
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
    CanonicalEvent,
    ExecutionAttempt,
    Operation,
    OperationBundle,
    OperationItem,
    OutboxEvent,
    ProviderEventBinding,
    QueueItem,
    Record,
    ReminderRule,
    SourceItem,
)
from docket.models.base import utc_now
from docket.policy import BATCH_CALENDAR_ACTION_TYPES, GMAIL_MUTATION_ACTION_TYPES
from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarEventResult,
    CalendarProvider,
    CalendarProviderError,
    CalendarUnknownOutcome,
    event_matches_request,
)
from docket.providers.google.gmail import (
    GmailMutationProvider,
    GmailMutationRequest,
    GmailMutationResult,
    GmailProviderError,
    GmailUnknownOutcome,
)
from docket.services.operational_logs import enqueue_action_system_log


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
    operation_item_id: uuid.UUID | None = None

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
            reminder_plan=(dict(reminder_plan) if isinstance(reminder_plan, dict) else None),
            logical_key=self.parameters.get("logical_key"),
            priority=str(self.parameters.get("priority", "normal")),
            priority_basis=str(self.parameters.get("priority_basis", "default")),
            reminder_plan_sha256=self.parameters.get("reminder_plan_sha256"),
            origin_kind=self.parameters.get("origin_kind"),
            operation_type=self.operation_type,
        )

    def gmail_request(self) -> GmailMutationRequest:
        return GmailMutationRequest(
            message_id=str(self.parameters["message_id"]),
            source_version=str(self.parameters["source_version"]),
            remove_label_id=str(self.parameters["remove_label_id"]),
            provider_correlation=self.provider_correlation,
        )


class OperationRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: CalendarProvider,
        *,
        gmail_provider: GmailMutationProvider | None = None,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        consistency_window_seconds: int = 30,
        execution_enabled: bool = True,
        gmail_execution_enabled: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.gmail_provider = gmail_provider
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.consistency_window_seconds = consistency_window_seconds
        self.execution_enabled = execution_enabled
        self.gmail_execution_enabled = gmail_execution_enabled

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
        eligible_types: set[str] = set()
        if self.execution_enabled:
            eligible_types.update(
                {
                    "calendar_create_event",
                    "calendar_update_event",
                    "calendar_update_reminders",
                    "calendar_cancel_event",
                }
            )
        if self.gmail_execution_enabled and self.gmail_provider is not None:
            eligible_types.update(GMAIL_MUTATION_ACTION_TYPES)
        if not eligible_types:
            return None
        operation = session.scalar(
            select(Operation)
            .where(
                Operation.status == desired_status,
                Operation.operation_type.in_(eligible_types),
                Operation.operation_type.not_in(BATCH_CALENDAR_ACTION_TYPES),
                or_(
                    Operation.predecessor_operation_id.is_(None),
                    Operation.predecessor_operation_id.in_(
                        select(Operation.id).where(
                            Operation.status == OperationStatus.SUCCEEDED.value
                        )
                    ),
                ),
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

    def _claim_batch_item(self, session: Session, *, reconcile: bool) -> ClaimedOperation | None:
        now = utc_now()
        desired_status = "reconciliation_required" if reconcile else "pending"
        item = session.scalar(
            select(OperationItem)
            .join(Operation, Operation.id == OperationItem.operation_id)
            .where(
                Operation.operation_type.in_(BATCH_CALENDAR_ACTION_TYPES),
                OperationItem.status == desired_status,
                or_(
                    OperationItem.next_attempt_at.is_(None),
                    OperationItem.next_attempt_at <= now,
                ),
                or_(
                    OperationItem.leased_until.is_(None),
                    OperationItem.leased_until < now,
                ),
            )
            .order_by(OperationItem.created_at, OperationItem.item_key)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        operation = session.get(Operation, item.operation_id)
        assert operation is not None
        _revision, action, queue_item = self._bound_entities(session, operation)
        lease_token = uuid.uuid4()
        item.lease_token = lease_token
        item.leased_until = now + timedelta(seconds=self.lease_seconds)
        if not reconcile:
            item.status = "running"
            operation.status = OperationStatus.RUNNING.value
            action.status = ActionStatus.EXECUTING.value
            queue_item.status = QueueItemStatus.EXECUTING.value
        item.attempt_count += 1
        attempt = ExecutionAttempt(
            operation_id=operation.id,
            operation_item_id=item.id,
            attempt_number=item.attempt_count,
            kind=(AttemptKind.RECONCILE.value if reconcile else AttemptKind.EXECUTE.value),
            request_summary={
                "operation_type": item.item_type,
                "parameters_sha256": item.parameters_sha256,
                "item_key": item.item_key,
            },
            status=AttemptStatus.STARTED.value,
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        return ClaimedOperation(
            operation_id=operation.id,
            operation_item_id=item.id,
            attempt_id=attempt.id,
            lease_token=lease_token,
            operation_type=item.item_type,
            provider_correlation=str(item.id),
            parameters=dict(item.parameters),
        )

    def claim_due(self) -> ClaimedOperation | None:
        if not self.execution_enabled and not self.gmail_execution_enabled:
            return None
        with self.session_factory.begin() as session:
            batch = (
                self._claim_batch_item(session, reconcile=False) if self.execution_enabled else None
            )
            return batch or self._claim(session, reconcile=False)

    def claim_reconciliation(self) -> ClaimedOperation | None:
        if not self.execution_enabled and not self.gmail_execution_enabled:
            return None
        with self.session_factory.begin() as session:
            batch = (
                self._claim_batch_item(session, reconcile=True) if self.execution_enabled else None
            )
            return batch or self._claim(session, reconcile=True)

    def mark_provider_call_started(self, claim: ClaimedOperation) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if (
                operation is None
                or attempt is None
                or (
                    claim.operation_item_id is None
                    and (
                        operation.lease_token != claim.lease_token
                        or operation.status != OperationStatus.RUNNING.value
                    )
                )
                or (
                    claim.operation_item_id is not None
                    and (
                        (item := session.get(OperationItem, claim.operation_item_id)) is None
                        or item.lease_token != claim.lease_token
                        or item.status != "running"
                    )
                )
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
        *,
        parameters_override: dict[str, Any] | None = None,
        classification_override: dict[str, Any] | None = None,
    ) -> None:
        revision, _action, _queue_item = OperationRunner._bound_entities(session, operation)
        parameters = parameters_override or revision.parameters
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
                    end_timezone_value if isinstance(end_timezone_value, str) else timezone
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
        classification = (
            classification_override
            if classification_override is not None
            else revision.preview.get("classification", {})
        )
        if isinstance(classification, dict):
            row.recurrence_kind = str(classification.get("recurrence_kind", "one_time"))
            row.system_tags = list(classification.get("system_tags", []))
            row.operator_tags = list(classification.get("operator_tags", []))
            row.priority = str(classification.get("priority", "normal"))
            row.priority_basis = str(classification.get("priority_basis", "default"))
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
        *,
        parameters_override: dict[str, Any] | None = None,
        manifest_item_key: str | None = None,
    ) -> None:
        revision, _action, _queue_item = OperationRunner._bound_entities(session, operation)
        parameters = parameters_override or revision.parameters
        plan = parameters.get("reminder_plan")
        if not isinstance(plan, dict):
            return
        desired_leads = {int(value) for value in plan.get("lead_seconds", [])}
        rules = list(
            session.scalars(
                select(ReminderRule).where(
                    ReminderRule.account_id == operation.account_id,
                    ReminderRule.calendar_id == parameters["calendar_id"],
                    ReminderRule.scope == "event",
                    ReminderRule.provider_event_id == result.external_event_id,
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
                    calendar_id=str(parameters["calendar_id"]),
                    scope="event",
                    provider_event_id=result.external_event_id,
                    lead_seconds=lead_seconds,
                    queue_channel_id=get_settings().queue_channel_id,
                    enabled=True,
                    created_by_actor_id=(
                        revision.created_by_actor_id or get_settings().operator_discord_user_id
                    ),
                )
                session.add(rule)
                session.flush()
                selected[lead_seconds] = rule
        now = utc_now()
        plan_statement = select(CalendarReminderPlan).where(
            CalendarReminderPlan.action_revision_id == revision.id
        )
        if manifest_item_key is None:
            plan_statement = plan_statement.where(CalendarReminderPlan.manifest_item_key.is_(None))
        else:
            plan_statement = plan_statement.where(
                CalendarReminderPlan.manifest_item_key == manifest_item_key
            )
        plans = session.scalars(plan_statement).all()
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
        revision, _action, _queue_item = OperationRunner._bound_entities(session, operation)
        rows = list(
            session.scalars(
                select(CalendarEventCache).where(
                    CalendarEventCache.account_id == operation.account_id,
                    CalendarEventCache.calendar_id == revision.parameters["calendar_id"],
                    or_(
                        CalendarEventCache.provider_event_id == result.external_event_id,
                        CalendarEventCache.recurring_event_id == result.external_event_id,
                    ),
                )
            )
        )
        for row in rows:
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
                link.reminder_plan_sha256 = parameters.get("reminder_plan_sha256")
                before = parameters.get("provider_before")
                link.synced_snapshot = {
                    **(dict(before) if isinstance(before, dict) else result.snapshot),
                    "status": "cancelled",
                }
            OperationRunner._apply_cancelled_cache(session, operation, result)
            OperationRunner._activate_reminder_plan(session, operation, result)
        else:
            if link is None:
                classification = revision.preview.get("classification", {})
                if not isinstance(classification, dict):
                    classification = {}
                link = CalendarLink(
                    canonical_event_id=(
                        uuid.UUID(str(parameters["canonical_event_id"]))
                        if parameters.get("canonical_event_id") is not None
                        else None
                    ),
                    record_id=None,
                    meeting_id=None,
                    origin_kind=(
                        "standalone"
                        if operation.operation_type == "calendar_create_event"
                        else "adopted_provider_event"
                    ),
                    logical_key=str(parameters["logical_key"]),
                    account_id=operation.account_id,
                    calendar_id=str(parameters["calendar_id"]),
                    external_event_id=result.external_event_id,
                    provider_etag=result.provider_etag,
                    provider_correlation=operation.provider_correlation,
                    last_synced_version=revision.revision,
                    recurrence_kind=str(classification.get("recurrence_kind", "one_time")),
                    system_tags=list(classification.get("system_tags", [])),
                    operator_tags=list(classification.get("operator_tags", [])),
                    priority=str(parameters.get("priority", "normal")),
                    priority_basis=str(parameters.get("priority_basis", "default")),
                    reminder_plan_sha256=parameters.get("reminder_plan_sha256"),
                    synced_snapshot=result.snapshot,
                )
                session.add(link)
                session.flush()
            else:
                if (
                    operation.operation_type
                    in {
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
                link.last_synced_version = revision.revision
                link.synced_snapshot = result.snapshot
                if parameters.get("reminder_plan_sha256") is not None:
                    link.reminder_plan_sha256 = parameters["reminder_plan_sha256"]
                if (
                    link.canonical_event_id is None
                    and parameters.get("canonical_event_id") is not None
                ):
                    link.canonical_event_id = uuid.UUID(str(parameters["canonical_event_id"]))
            OperationRunner._upsert_calendar_cache(session, operation, result)
            OperationRunner._activate_reminder_plan(session, operation, result)
        canonical_event_id = parameters.get("canonical_event_id")
        if canonical_event_id is not None:
            canonical_event = session.get(CanonicalEvent, uuid.UUID(str(canonical_event_id)))
            if canonical_event is None:
                raise DocketError(
                    code="canonical_event_not_found",
                    message="The operation lost its canonical event binding.",
                )
            event_spec = parameters.get("event")
            if isinstance(event_spec, dict):
                canonical_event.event_spec = dict(event_spec)
                canonical_event.title = str(event_spec.get("title") or canonical_event.title)
            reminder_plan = parameters.get("reminder_plan")
            if isinstance(reminder_plan, dict):
                canonical_event.reminder_plan = dict(reminder_plan)
            entity_refs = parameters.get("entity_refs")
            if isinstance(entity_refs, list):
                canonical_event.entity_refs = [
                    dict(value) for value in entity_refs if isinstance(value, dict)
                ]
            context_labels = parameters.get("context_labels")
            if isinstance(context_labels, list):
                canonical_event.context_labels = [str(value) for value in context_labels]
            canonical_event.status = (
                "cancelled" if operation.operation_type == "calendar_cancel_event" else "active"
            )
            canonical_event.version += 1
            binding = session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.account_id == operation.account_id,
                    ProviderEventBinding.calendar_id == parameters["calendar_id"],
                    ProviderEventBinding.provider_event_id == result.external_event_id,
                )
            )
            if binding is None:
                binding = ProviderEventBinding(
                    canonical_event_id=canonical_event.id,
                    account_id=operation.account_id,
                    calendar_id=str(parameters["calendar_id"]),
                    provider_event_id=result.external_event_id,
                    provider_etag=result.provider_etag,
                    status=(
                        "cancelled"
                        if operation.operation_type == "calendar_cancel_event"
                        else "active"
                    ),
                    provider_snapshot=dict(result.snapshot),
                )
                session.add(binding)
            else:
                binding.canonical_event_id = canonical_event.id
                binding.provider_etag = result.provider_etag
                binding.status = (
                    "cancelled" if operation.operation_type == "calendar_cancel_event" else "active"
                )
                binding.provider_snapshot = dict(result.snapshot)
                binding.independently_modified_at = None
                binding.version += 1
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
            "record_version": None,
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
        if operation.bundle_id is None and queue_item.presentation != "suppressed":
            session.add(
                OutboxEvent(
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=(
                        f"discord_projection:{queue_item.id}:operation:{operation.id}:ok"
                    ),
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "action_id": str(action.id),
                        "operation_id": str(operation.id),
                        "status": "succeeded",
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
        if operation.bundle_id is None:
            enqueue_action_system_log(
                session,
                action=action,
                revision=revision,
                state="succeeded",
                result=operation.result,
            )
        else:
            OperationRunner._update_operation_bundle(session, operation)

    @staticmethod
    def _update_operation_bundle(session: Session, changed: Operation) -> None:
        if changed.bundle_id is None:
            return
        bundle = session.get(OperationBundle, changed.bundle_id)
        if bundle is None:
            raise DocketError(
                code="invalid_operation_state",
                message="A bundled operation lost its durable bundle.",
            )
        operations = list(
            session.scalars(
                select(Operation)
                .where(Operation.bundle_id == bundle.id)
                .order_by(Operation.created_at, Operation.id)
            )
        )
        if not operations:
            raise DocketError(
                code="invalid_operation_state",
                message="An operation bundle has no durable operations.",
            )
        by_id = {operation.id: operation for operation in operations}
        for operation in operations:
            if operation.status != OperationStatus.PENDING.value:
                continue
            predecessor = (
                by_id.get(operation.predecessor_operation_id)
                if operation.predecessor_operation_id is not None
                else None
            )
            if predecessor is None or predecessor.status not in {
                OperationStatus.FAILED.value,
                OperationStatus.PARTIAL_FAILED.value,
            }:
                continue
            operation.status = OperationStatus.FAILED.value
            operation.next_attempt_at = None
            operation.last_error_code = "bundle_dependency_failed"
            operation.last_error_message = (
                "A required earlier operation failed; this operation was not attempted."
            )
            _revision, child_action, child_queue = OperationRunner._bound_entities(
                session, operation
            )
            child_action.status = ActionStatus.FAILED.value
            child_queue.status = QueueItemStatus.FAILED.value
            child_queue.resolved_at = utc_now()
            child_queue.resolution_code = "bundle_dependency_failed"
            child_queue.version += 1

        counts = {
            state: sum(operation.status == state for operation in operations)
            for state in (
                OperationStatus.PENDING.value,
                OperationStatus.RUNNING.value,
                OperationStatus.SUCCEEDED.value,
                OperationStatus.PARTIAL_FAILED.value,
                OperationStatus.FAILED.value,
                OperationStatus.RECONCILIATION_REQUIRED.value,
            )
        }
        previous_status = bundle.status
        if counts[OperationStatus.RECONCILIATION_REQUIRED.value]:
            bundle.status = OperationStatus.RECONCILIATION_REQUIRED.value
        elif counts[OperationStatus.PENDING.value] or counts[OperationStatus.RUNNING.value]:
            bundle.status = "running"
        elif counts[OperationStatus.FAILED.value] or counts[OperationStatus.PARTIAL_FAILED.value]:
            bundle.status = (
                OperationStatus.PARTIAL_FAILED.value
                if counts[OperationStatus.SUCCEEDED.value]
                or counts[OperationStatus.PARTIAL_FAILED.value]
                else OperationStatus.FAILED.value
            )
        else:
            bundle.status = OperationStatus.SUCCEEDED.value
        bundle.result = {
            "operation_count": len(operations),
            "counts": counts,
            "operations": [
                {
                    "operation_id": str(operation.id),
                    "operation_type": operation.operation_type,
                    "status": operation.status,
                    "error_code": operation.last_error_code,
                }
                for operation in operations
            ],
        }
        if bundle.status != previous_status:
            bundle.version += 1

        parent_revision = session.get(ActionRevision, bundle.action_revision_id)
        if parent_revision is None:
            raise DocketError(
                code="invalid_operation_state",
                message="An operation bundle lost its decision revision.",
            )
        parent_action = session.get(Action, parent_revision.action_id)
        parent_queue = (
            session.get(QueueItem, parent_action.queue_item_id)
            if parent_action is not None and parent_action.queue_item_id is not None
            else None
        )
        if parent_action is None or parent_queue is None:
            raise DocketError(
                code="invalid_operation_state",
                message="An operation bundle lost its decision surface.",
            )
        if bundle.status == "running":
            parent_action.status = ActionStatus.EXECUTING.value
            parent_queue.status = QueueItemStatus.EXECUTING.value
            parent_queue.resolved_at = None
            parent_queue.resolution_code = None
        elif bundle.status == OperationStatus.SUCCEEDED.value:
            parent_action.status = ActionStatus.SUCCEEDED.value
            parent_queue.status = QueueItemStatus.COMPLETED.value
            parent_queue.resolved_at = utc_now()
            parent_queue.resolution_code = f"calendar_conflict_{bundle.resolution}"
        elif bundle.status == OperationStatus.RECONCILIATION_REQUIRED.value:
            parent_action.status = ActionStatus.RECONCILIATION_REQUIRED.value
            parent_queue.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
            parent_queue.resolved_at = None
            parent_queue.resolution_code = None
        else:
            parent_action.status = (
                ActionStatus.PARTIAL_FAILED.value
                if bundle.status == OperationStatus.PARTIAL_FAILED.value
                else ActionStatus.FAILED.value
            )
            parent_queue.status = QueueItemStatus.FAILED.value
            parent_queue.resolved_at = utc_now()
            parent_queue.resolution_code = (
                "calendar_conflict_partial"
                if bundle.status == OperationStatus.PARTIAL_FAILED.value
                else "calendar_conflict_failed"
            )
        parent_queue.version += 1
        if bundle.status == previous_status:
            return
        session.add(
            AuditEvent(
                event_type="operation_bundle.state_changed",
                entity_type="operation_bundle",
                entity_id=bundle.id,
                actor_type="docket",
                actor_id=None,
                request_id=None,
                data={
                    "status": bundle.status,
                    "resolution": bundle.resolution,
                    "result": bundle.result,
                },
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=parent_queue.id,
                deduplication_key=(
                    f"discord_projection:{parent_queue.id}:bundle:{bundle.id}:v{bundle.version}"
                ),
                payload={
                    "queue_item_id": str(parent_queue.id),
                    "action_id": str(parent_action.id),
                    "operation_bundle_id": str(bundle.id),
                    "status": bundle.status,
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        if bundle.status in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.PARTIAL_FAILED.value,
            OperationStatus.FAILED.value,
            OperationStatus.RECONCILIATION_REQUIRED.value,
        }:
            enqueue_action_system_log(
                session,
                action=parent_action,
                revision=parent_revision,
                state=bundle.status,
                result=bundle.result,
            )

    @staticmethod
    def _update_batch_parent(session: Session, operation: Operation) -> None:
        """Derive the aggregate state exclusively from its durable item ledger."""
        revision, action, queue_item = OperationRunner._bound_entities(session, operation)
        previous_status = operation.status
        items = list(
            session.scalars(
                select(OperationItem)
                .where(OperationItem.operation_id == operation.id)
                .order_by(OperationItem.item_key)
            )
        )
        if not items:
            raise DocketError(
                code="invalid_operation_state",
                message="A schedule operation has no durable items.",
            )
        counts = {
            state: sum(item.status == state for item in items)
            for state in (
                "pending",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
            )
        }
        failures = [
            {
                "item_key": item.item_key,
                "status": item.status,
                "error_code": item.last_error_code,
            }
            for item in items
            if item.status in {"failed", "reconciliation_required"}
        ]
        operation.result = {
            "item_count": len(items),
            "counts": counts,
            "failures": failures,
        }
        operation.last_error_code = failures[0]["error_code"] if failures else None
        operation.last_error_message = None
        operation.lease_token = None
        operation.leased_until = None
        operation.next_attempt_at = None

        if counts["reconciliation_required"]:
            operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
            action.status = ActionStatus.RECONCILIATION_REQUIRED.value
            queue_item.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
            queue_item.resolved_at = None
            queue_item.resolution_code = None
        elif counts["pending"] or counts["running"]:
            operation.status = OperationStatus.RUNNING.value
            action.status = ActionStatus.EXECUTING.value
            queue_item.status = QueueItemStatus.EXECUTING.value
            queue_item.resolved_at = None
            queue_item.resolution_code = None
        elif counts["failed"]:
            partially_succeeded = counts["succeeded"] > 0
            operation.status = (
                OperationStatus.PARTIAL_FAILED.value
                if partially_succeeded
                else OperationStatus.FAILED.value
            )
            action.status = (
                ActionStatus.PARTIAL_FAILED.value
                if partially_succeeded
                else ActionStatus.FAILED.value
            )
            queue_item.status = QueueItemStatus.FAILED.value
            queue_item.resolved_at = utc_now()
            queue_item.resolution_code = (
                "calendar_schedule_partial" if partially_succeeded else "calendar_schedule_failed"
            )
        else:
            if not OperationRunner._apply_course_transition(
                session,
                operation,
                revision,
                action,
                queue_item,
            ):
                queue_item.version += 1
                if operation.bundle_id is not None:
                    OperationRunner._update_operation_bundle(session, operation)
                elif operation.status != previous_status:
                    enqueue_action_system_log(
                        session,
                        action=action,
                        revision=revision,
                        state=operation.status,
                        result=operation.result,
                    )
                return
            operation.status = OperationStatus.SUCCEEDED.value
            action.status = ActionStatus.SUCCEEDED.value
            queue_item.status = QueueItemStatus.COMPLETED.value
            queue_item.resolved_at = utc_now()
            queue_item.resolution_code = (
                "calendar_course_dropped"
                if revision.action_type == "calendar_drop_course"
                else "calendar_course_synchronized"
                if revision.action_type == "calendar_reconcile_course"
                else "calendar_schedule_synchronized"
            )
        queue_item.version += 1
        if operation.bundle_id is not None:
            OperationRunner._update_operation_bundle(session, operation)
        elif operation.status != previous_status and operation.status in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.PARTIAL_FAILED.value,
            OperationStatus.FAILED.value,
            OperationStatus.RECONCILIATION_REQUIRED.value,
        }:
            enqueue_action_system_log(
                session,
                action=action,
                revision=revision,
                state=operation.status,
                result=operation.result,
            )

    @staticmethod
    def _apply_course_transition(
        session: Session,
        operation: Operation,
        revision: ActionRevision,
        action: Action,
        queue_item: QueueItem,
    ) -> bool:
        if revision.action_type != "calendar_drop_course":
            return True
        try:
            record_id = uuid.UUID(str(revision.parameters["record_id"]))
            expected_version = int(revision.parameters["record_version"])
        except (KeyError, TypeError, ValueError):
            record_id = uuid.UUID(int=0)
            expected_version = -1
        record = session.get(Record, record_id)
        if record is not None and record.status == "active" and record.version == expected_version:
            active_links = list(
                session.scalars(select(CalendarLink).where(CalendarLink.record_id == record.id))
            )
            if any(link.synced_snapshot.get("status") != "cancelled" for link in active_links):
                operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
                operation.last_error_code = "course_archive_links_active"
                action.status = ActionStatus.RECONCILIATION_REQUIRED.value
                queue_item.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
                queue_item.resolved_at = None
                queue_item.resolution_code = None
                result = dict(operation.result or {})
                failures = list(result.get("failures") or [])
                failures.append(
                    {
                        "item_key": f"course:{record_id}:archive",
                        "status": "reconciliation_required",
                        "error_code": "course_archive_links_active",
                    }
                )
                result["failures"] = failures
                operation.result = result
                return False
            record.status = "archived"
            record.version += 1
            session.add(
                AuditEvent(
                    event_type="record.archived",
                    entity_type="record",
                    entity_id=record.id,
                    actor_type="docket",
                    actor_id=None,
                    request_id=None,
                    data={
                        "version": record.version,
                        "reason": revision.parameters.get("reason"),
                        "operation_id": str(operation.id),
                    },
                )
            )
            result = dict(operation.result or {})
            result["record_transition"] = {
                "record_id": str(record.id),
                "status": "archived",
                "version": record.version,
            }
            operation.result = result
            return True
        if (
            record is not None
            and record.status == "archived"
            and record.version == expected_version + 1
        ):
            return True
        operation.status = OperationStatus.RECONCILIATION_REQUIRED.value
        operation.last_error_code = "course_archive_transition_conflict"
        action.status = ActionStatus.RECONCILIATION_REQUIRED.value
        queue_item.status = QueueItemStatus.RECONCILIATION_REQUIRED.value
        queue_item.resolved_at = None
        queue_item.resolution_code = None
        result = dict(operation.result or {})
        failures = list(result.get("failures") or [])
        failures.append(
            {
                "item_key": f"course:{record_id}:archive",
                "status": "reconciliation_required",
                "error_code": "course_archive_transition_conflict",
            }
        )
        result["failures"] = failures
        operation.result = result
        return False

    @staticmethod
    def _apply_batch_item_success(
        session: Session,
        operation: Operation,
        item: OperationItem,
        attempt: ExecutionAttempt,
        result: CalendarEventResult,
    ) -> None:
        revision, action, queue_item = OperationRunner._bound_entities(session, operation)
        parameters = dict(item.parameters)
        if sha256_json(parameters) != item.parameters_sha256:
            raise DocketError(
                code="operation_item_binding_mismatch",
                message="The immutable schedule item hash no longer matches.",
            )
        logical_key = str(parameters["logical_key"])
        link = session.scalar(
            select(CalendarLink).where(
                CalendarLink.account_id == operation.account_id,
                CalendarLink.calendar_id == parameters["calendar_id"],
                CalendarLink.logical_key == logical_key,
            )
        )
        if link is None:
            if item.item_type != "calendar_create_event":
                raise DocketError(
                    code="calendar_link_conflict",
                    message="The schedule update no longer has a linked event.",
                )
            classification = parameters.get("classification", {})
            if not isinstance(classification, dict):
                classification = {}
            link = CalendarLink(
                record_id=uuid.UUID(str(parameters["record_id"])),
                meeting_id=str(parameters["meeting_id"]),
                origin_kind="course_meeting",
                logical_key=logical_key,
                account_id=operation.account_id,
                calendar_id=str(parameters["calendar_id"]),
                external_event_id=result.external_event_id,
                provider_etag=result.provider_etag,
                provider_correlation=str(item.id),
                last_synced_version=int(parameters["record_version"]),
                recurrence_kind=str(classification.get("recurrence_kind", "recurring")),
                system_tags=list(classification.get("system_tags", [])),
                operator_tags=list(classification.get("operator_tags", [])),
                priority=str(parameters.get("priority", "normal")),
                priority_basis=str(parameters.get("priority_basis", "default")),
                reminder_plan_sha256=parameters.get("reminder_plan_sha256"),
                synced_snapshot=result.snapshot,
            )
            session.add(link)
            session.flush()
        elif item.item_type == "calendar_cancel_event":
            link.provider_etag = None
            link.provider_correlation = str(item.id)
            link.last_synced_version = int(parameters["record_version"])
            link.reminder_plan_sha256 = parameters.get("reminder_plan_sha256")
            before = parameters.get("provider_before")
            link.synced_snapshot = {
                **(dict(before) if isinstance(before, dict) else result.snapshot),
                "status": "cancelled",
            }
        else:
            if (
                item.item_type == "calendar_update_event"
                and link.external_event_id != result.external_event_id
            ):
                raise DocketError(
                    code="calendar_link_conflict",
                    message="The schedule update returned a different event identity.",
                )
            link.external_event_id = result.external_event_id
            link.provider_etag = result.provider_etag
            link.provider_correlation = str(item.id)
            link.last_synced_version = int(parameters["record_version"])
            link.reminder_plan_sha256 = parameters.get("reminder_plan_sha256")
            link.synced_snapshot = result.snapshot

        classification_value = parameters.get("classification")
        classification = (
            dict(classification_value) if isinstance(classification_value, dict) else {}
        )
        if item.item_type == "calendar_cancel_event":
            OperationRunner._apply_cancelled_cache(session, operation, result)
        else:
            OperationRunner._upsert_calendar_cache(
                session,
                operation,
                result,
                parameters_override=parameters,
                classification_override=classification,
            )
        OperationRunner._activate_reminder_plan(
            session,
            operation,
            result,
            parameters_override=parameters,
            manifest_item_key=item.item_key,
        )
        now = utc_now()
        attempt.status = AttemptStatus.SUCCEEDED.value
        attempt.provider_request_id = result.provider_request_id or attempt.provider_request_id
        attempt.response_summary = {
            "item_key": item.item_key,
            "external_event_id": result.external_event_id,
            "provider_etag": result.provider_etag,
        }
        attempt.completed_at = now
        item.status = "succeeded"
        item.lease_token = None
        item.leased_until = None
        item.next_attempt_at = None
        item.result = {
            "calendar_link_id": str(link.id),
            "external_event_id": result.external_event_id,
            "record_version": int(parameters["record_version"]),
        }
        item.last_error_code = None
        OperationRunner._update_batch_parent(session, operation)
        session.add(
            AuditEvent(
                event_type="operation_item.succeeded",
                entity_type="operation_item",
                entity_id=item.id,
                actor_type="docket",
                actor_id=None,
                request_id=None,
                data={
                    "operation_id": str(operation.id),
                    "action_revision_id": str(revision.id),
                    "attempt_id": str(attempt.id),
                    "item_key": item.item_key,
                    "external_event_id": result.external_event_id,
                    "parameters_sha256": item.parameters_sha256,
                },
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=(
                    f"discord_projection:{queue_item.id}:operation-item:"
                    f"{item.id}:attempt:{item.attempt_count}:ok"
                ),
                payload={
                    "queue_item_id": str(queue_item.id),
                    "action_id": str(action.id),
                    "operation_id": str(operation.id),
                    "operation_item_id": str(item.id),
                    "status": item.status,
                    "parent_status": operation.status,
                },
                status=OutboxStatus.PENDING.value,
            )
        )

    @staticmethod
    def _apply_gmail_success(
        session: Session,
        operation: Operation,
        attempt: ExecutionAttempt,
        result: GmailMutationResult,
    ) -> None:
        revision, action, queue_item = OperationRunner._bound_entities(session, operation)
        parameters = revision.parameters
        if result.message_id != parameters.get("message_id"):
            raise DocketError(
                code="gmail_result_binding_mismatch",
                message="Gmail returned a result for a different message.",
            )
        try:
            source_id = uuid.UUID(str(parameters["source_item_id"]))
        except ValueError as exc:
            raise DocketError(
                code="invalid_operation_state",
                message="The Gmail operation has an invalid source binding.",
            ) from exc
        source = session.get(SourceItem, source_id)
        if (
            source is None
            or source.account_id != operation.account_id
            or source.external_object_id != result.message_id
            or source.source_version != parameters.get("source_version")
        ):
            raise DocketError(
                code="gmail_source_binding_mismatch",
                message="The Gmail operation source binding is no longer valid.",
            )
        headers = dict(source.minimal_headers)
        headers["label_ids"] = list(result.label_ids)
        headers["provider_observed_version"] = result.source_version
        source.minimal_headers = headers
        now = utc_now()
        attempt.status = AttemptStatus.SUCCEEDED.value
        attempt.provider_request_id = result.provider_request_id or attempt.provider_request_id
        attempt.response_summary = {
            "disposition": result.disposition,
            "removed_label_id": parameters["remove_label_id"],
            "source_version": result.source_version,
        }
        attempt.completed_at = now
        operation.status = OperationStatus.SUCCEEDED.value
        operation.lease_token = None
        operation.leased_until = None
        operation.next_attempt_at = None
        operation.result = {
            "message_id": result.message_id,
            "source_version": result.source_version,
            "removed_label_id": parameters["remove_label_id"],
            "disposition": result.disposition,
        }
        operation.last_error_code = None
        operation.last_error_message = None
        action.status = ActionStatus.SUCCEEDED.value
        pending_sibling = session.scalar(
            select(Action.id).where(
                Action.queue_item_id == queue_item.id,
                Action.id != action.id,
                Action.status == ActionStatus.APPROVAL_PENDING.value,
            )
        )
        if pending_sibling is not None:
            queue_item.status = QueueItemStatus.AWAITING_APPROVAL.value
            queue_item.resolved_at = None
            queue_item.resolution_code = None
        else:
            queue_item.status = QueueItemStatus.COMPLETED.value
            queue_item.resolved_at = now
            queue_item.resolution_code = (
                "gmail_archived"
                if operation.operation_type == "gmail_archive_message"
                else "gmail_marked_read"
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
                    "message_id": result.message_id,
                    "source_version": result.source_version,
                    "parameters_sha256": revision.parameters_sha256,
                    "disposition": result.disposition,
                },
            )
        )
        session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=(
                    f"discord_projection:{queue_item.id}:operation:{operation.id}:ok"
                ),
                payload={
                    "queue_item_id": str(queue_item.id),
                    "action_id": str(action.id),
                    "operation_id": str(operation.id),
                    "status": "succeeded",
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        enqueue_action_system_log(
            session,
            action=action,
            revision=revision,
            state="succeeded",
            result=operation.result,
        )

    def finish_success(self, claim: ClaimedOperation, result: CalendarEventResult) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            if claim.operation_item_id is not None:
                item = session.get(OperationItem, claim.operation_item_id)
                if (
                    item is None
                    or item.lease_token != claim.lease_token
                    or item.status not in {"running", "reconciliation_required"}
                ):
                    raise DocketError(
                        code="operation_lease_lost",
                        message="Operation item lease was lost.",
                    )
                self._apply_batch_item_success(session, operation, item, attempt, result)
                return
            if operation.lease_token != claim.lease_token:
                raise DocketError(
                    code="operation_lease_lost",
                    message="Operation lease was lost.",
                )
            self._apply_success(session, operation, attempt, result)

    def finish_gmail_success(
        self,
        claim: ClaimedOperation,
        result: GmailMutationResult,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None:
                raise DocketError(
                    code="operation_lease_lost",
                    message="Operation lease was lost.",
                )
            if claim.operation_item_id is not None or operation.lease_token != claim.lease_token:
                raise DocketError(
                    code="operation_lease_lost",
                    message="Operation lease was lost.",
                )
            self._apply_gmail_success(session, operation, attempt, result)

    def finish_error(
        self,
        claim: ClaimedOperation,
        error: CalendarProviderError | GmailProviderError,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            if claim.operation_item_id is not None:
                item = session.get(OperationItem, claim.operation_item_id)
                if (
                    item is None
                    or item.lease_token != claim.lease_token
                    or item.status != "running"
                ):
                    raise DocketError(
                        code="operation_lease_lost",
                        message="Operation item lease was lost.",
                    )
                revision, action, queue_item = self._bound_entities(session, operation)
                now = utc_now()
                attempt.status = (
                    AttemptStatus.UNKNOWN.value
                    if isinstance(error, (CalendarUnknownOutcome, GmailUnknownOutcome))
                    else AttemptStatus.FAILED.value
                )
                attempt.error_code = error.code
                attempt.error_message = error.safe_message
                attempt.completed_at = now
                item.lease_token = None
                item.leased_until = None
                item.last_error_code = error.code
                if isinstance(error, (CalendarUnknownOutcome, GmailUnknownOutcome)):
                    item.status = "reconciliation_required"
                    item.next_attempt_at = now
                elif error.transient and item.attempt_count < self.max_attempts:
                    item.status = "pending"
                    item.next_attempt_at = now + timedelta(seconds=min(300, 2**item.attempt_count))
                else:
                    item.status = "failed"
                    item.next_attempt_at = None
                if item.status in {"failed", "reconciliation_required"}:
                    plan_status = (
                        "reconciliation_required"
                        if item.status == "reconciliation_required"
                        else "cancelled"
                    )
                    for plan in session.scalars(
                        select(CalendarReminderPlan).where(
                            CalendarReminderPlan.action_revision_id == revision.id,
                            CalendarReminderPlan.manifest_item_key == item.item_key,
                            CalendarReminderPlan.status.in_(("planned", "reconciliation_required")),
                        )
                    ):
                        plan.status = plan_status
                self._update_batch_parent(session, operation)
                session.add(
                    AuditEvent(
                        event_type="operation_item.failed",
                        entity_type="operation_item",
                        entity_id=item.id,
                        actor_type="docket",
                        actor_id=None,
                        request_id=None,
                        data={
                            "operation_id": str(operation.id),
                            "attempt_id": str(attempt.id),
                            "item_key": item.item_key,
                            "error_code": error.code,
                            "unknown_outcome": isinstance(
                                error, (CalendarUnknownOutcome, GmailUnknownOutcome)
                            ),
                            "will_retry": item.status in {"pending", "reconciliation_required"},
                        },
                    )
                )
                session.add(
                    OutboxEvent(
                        event_type="discord.projection.refresh_requested",
                        aggregate_type="queue_item",
                        aggregate_id=queue_item.id,
                        deduplication_key=(
                            f"discord_projection:{queue_item.id}:operation-item:"
                            f"{item.id}:attempt:{item.attempt_count}:error"
                        ),
                        payload={
                            "queue_item_id": str(queue_item.id),
                            "action_id": str(action.id),
                            "operation_id": str(operation.id),
                            "operation_item_id": str(item.id),
                            "status": item.status,
                            "parent_status": operation.status,
                        },
                        status=OutboxStatus.PENDING.value,
                    )
                )
                return
            if operation.lease_token != claim.lease_token:
                raise DocketError(
                    code="operation_lease_lost",
                    message="Operation lease was lost.",
                )
            revision, action, queue_item = self._bound_entities(session, operation)
            now = utc_now()
            attempt.status = (
                AttemptStatus.UNKNOWN.value
                if isinstance(error, (CalendarUnknownOutcome, GmailUnknownOutcome))
                else AttemptStatus.FAILED.value
            )
            attempt.error_code = error.code
            attempt.error_message = error.safe_message
            attempt.completed_at = now
            operation.lease_token = None
            operation.leased_until = None
            operation.last_error_code = error.code
            operation.last_error_message = error.safe_message
            if isinstance(error, (CalendarUnknownOutcome, GmailUnknownOutcome)):
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
                if (
                    revision.action_type in GMAIL_MUTATION_ACTION_TYPES
                    and session.scalar(
                        select(Action.id).where(
                            Action.queue_item_id == queue_item.id,
                            Action.id != action.id,
                            Action.status == ActionStatus.APPROVAL_PENDING.value,
                        )
                    )
                    is not None
                ):
                    queue_item.status = QueueItemStatus.AWAITING_APPROVAL.value
            if operation.status in {
                OperationStatus.FAILED.value,
                OperationStatus.RECONCILIATION_REQUIRED.value,
            }:
                plan_status = (
                    "reconciliation_required"
                    if operation.status == OperationStatus.RECONCILIATION_REQUIRED.value
                    else "cancelled"
                )
                for plan in session.scalars(
                    select(CalendarReminderPlan).where(
                        CalendarReminderPlan.action_revision_id == revision.id,
                        CalendarReminderPlan.status.in_(("planned", "reconciliation_required")),
                    )
                ):
                    plan.status = plan_status
            queue_item.version += 1
            if operation.bundle_id is not None:
                self._update_operation_bundle(session, operation)
                return
            if revision.action_type in GMAIL_MUTATION_ACTION_TYPES:
                session.add(
                    AuditEvent(
                        event_type="operation.failed",
                        entity_type="operation",
                        entity_id=operation.id,
                        actor_type="docket",
                        actor_id=None,
                        request_id=None,
                        data={
                            "action_revision_id": str(revision.id),
                            "attempt_id": str(attempt.id),
                            "error_code": error.code,
                            "unknown_outcome": isinstance(
                                error,
                                (CalendarUnknownOutcome, GmailUnknownOutcome),
                            ),
                            "will_retry": operation.status
                            in {
                                OperationStatus.PENDING.value,
                                OperationStatus.RECONCILIATION_REQUIRED.value,
                            },
                        },
                    )
                )
                session.add(
                    OutboxEvent(
                        event_type="discord.projection.refresh_requested",
                        aggregate_type="queue_item",
                        aggregate_id=queue_item.id,
                        deduplication_key=(
                            f"discord_projection:{queue_item.id}:operation:"
                            f"{operation.id}:attempt:{operation.attempt_count}:error"
                        ),
                        payload={
                            "queue_item_id": str(queue_item.id),
                            "action_id": str(action.id),
                            "operation_id": str(operation.id),
                            "status": operation.status,
                        },
                        status=OutboxStatus.PENDING.value,
                    )
                )
            if operation.status in {
                OperationStatus.FAILED.value,
                OperationStatus.RECONCILIATION_REQUIRED.value,
            }:
                enqueue_action_system_log(
                    session,
                    action=action,
                    revision=revision,
                    state=operation.status,
                    result=operation.result,
                )

    def run_due_once(self) -> bool:
        claim = self.claim_due()
        if claim is None:
            return False
        self.mark_provider_call_started(claim)
        if claim.operation_type in GMAIL_MUTATION_ACTION_TYPES:
            if self.gmail_provider is None:
                self.finish_error(
                    claim,
                    GmailProviderError(
                        "gmail_provider_disabled",
                        "The Gmail mutation provider is disabled.",
                        transient=False,
                    ),
                )
                return True
            try:
                gmail_result = self.gmail_provider.mutate_message(claim.gmail_request())
            except GmailProviderError as exc:
                self.finish_error(claim, exc)
            else:
                self.finish_gmail_success(claim, gmail_result)
            return True
        request = claim.calendar_request()
        try:
            if claim.operation_type == "calendar_create_event":
                calendar_result = self.provider.create_event(request)
            elif claim.operation_type in {
                "calendar_update_event",
                "calendar_update_reminders",
            }:
                calendar_result = self.provider.update_event(request)
            elif claim.operation_type == "calendar_cancel_event":
                calendar_result = self.provider.cancel_event(request)
            else:
                raise CalendarProviderError(
                    "unsupported_operation",
                    "No provider handler exists for this operation.",
                    transient=False,
                )
        except CalendarProviderError as exc:
            self.finish_error(claim, exc)
        else:
            self.finish_success(claim, calendar_result)
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
                        Operation.operation_type.not_in(BATCH_CALENDAR_ACTION_TYPES),
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
            items = list(
                session.scalars(
                    select(OperationItem)
                    .join(Operation, Operation.id == OperationItem.operation_id)
                    .where(
                        Operation.operation_type.in_(BATCH_CALENDAR_ACTION_TYPES),
                        OperationItem.status == "running",
                        OperationItem.leased_until < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for item in items:
                parent_operation = session.get(Operation, item.operation_id)
                assert parent_operation is not None
                attempt = session.scalar(
                    select(ExecutionAttempt)
                    .where(
                        ExecutionAttempt.operation_id == parent_operation.id,
                        ExecutionAttempt.operation_item_id == item.id,
                    )
                    .order_by(ExecutionAttempt.attempt_number.desc())
                    .limit(1)
                )
                if attempt is None:
                    item.status = "pending"
                    item.last_error_code = "worker_crash_before_attempt_persisted"
                elif attempt.provider_request_id is None:
                    attempt.status = AttemptStatus.FAILED.value
                    attempt.error_code = "worker_crash_before_provider_call"
                    attempt.completed_at = now
                    item.status = "pending"
                    item.last_error_code = attempt.error_code
                else:
                    attempt.status = AttemptStatus.UNKNOWN.value
                    attempt.error_code = "worker_crash_after_provider_call_started"
                    attempt.completed_at = now
                    item.status = "reconciliation_required"
                    item.last_error_code = attempt.error_code
                item.lease_token = None
                item.leased_until = None
                item.next_attempt_at = now
                self._update_batch_parent(session, parent_operation)
                recovered += 1
        return recovered

    def _defer_reconciliation(
        self, claim: ClaimedOperation, *, error_code: str, delay_seconds: int
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_code = error_code
            attempt.completed_at = utc_now()
            if claim.operation_item_id is not None:
                item = session.get(OperationItem, claim.operation_item_id)
                if (
                    item is None
                    or item.lease_token != claim.lease_token
                    or item.status != "reconciliation_required"
                ):
                    raise DocketError(
                        code="operation_lease_lost",
                        message="Operation item lease was lost.",
                    )
                item.lease_token = None
                item.leased_until = None
                item.next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)
                item.last_error_code = error_code
                self._update_batch_parent(session, operation)
                return
            if operation.lease_token != claim.lease_token:
                raise DocketError(
                    code="operation_lease_lost",
                    message="Operation lease was lost.",
                )
            operation.lease_token = None
            operation.leased_until = None
            operation.next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)

    def _finish_reconciliation_no_match(self, claim: ClaimedOperation) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(Operation, claim.operation_id)
            attempt = session.get(ExecutionAttempt, claim.attempt_id)
            if operation is None or attempt is None:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            now = utc_now()
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.response_summary = {"matches": 0}
            attempt.completed_at = now
            if claim.operation_item_id is not None:
                item = session.get(OperationItem, claim.operation_item_id)
                if (
                    item is None
                    or item.lease_token != claim.lease_token
                    or item.status != "reconciliation_required"
                ):
                    raise DocketError(
                        code="operation_lease_lost",
                        message="Operation item lease was lost.",
                    )
                item.status = "pending"
                item.lease_token = None
                item.leased_until = None
                item.next_attempt_at = now
                item.last_error_code = None
                self._update_batch_parent(session, operation)
                return
            if operation.lease_token != claim.lease_token:
                raise DocketError(
                    code="operation_lease_lost",
                    message="Operation lease was lost.",
                )
            _, action, queue_item = self._bound_entities(session, operation)
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
            if operation is None or attempt is None:
                raise DocketError(code="operation_lease_lost", message="Operation lease was lost.")
            _, _, queue_item = self._bound_entities(session, operation)
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_code = "calendar_reconciliation_conflict"
            attempt.response_summary = {"matches": match_count}
            attempt.completed_at = utc_now()
            aggregate_type = "operation"
            aggregate_id = operation.id
            attempt_number = operation.attempt_count
            if claim.operation_item_id is not None:
                item = session.get(OperationItem, claim.operation_item_id)
                if (
                    item is None
                    or item.lease_token != claim.lease_token
                    or item.status != "reconciliation_required"
                ):
                    raise DocketError(
                        code="operation_lease_lost",
                        message="Operation item lease was lost.",
                    )
                item.lease_token = None
                item.leased_until = None
                item.next_attempt_at = utc_now() + timedelta(minutes=5)
                item.last_error_code = "calendar_reconciliation_conflict"
                self._update_batch_parent(session, operation)
                aggregate_type = "operation_item"
                aggregate_id = item.id
                attempt_number = item.attempt_count
            else:
                if operation.lease_token != claim.lease_token:
                    raise DocketError(
                        code="operation_lease_lost",
                        message="Operation lease was lost.",
                    )
                operation.lease_token = None
                operation.leased_until = None
                operation.next_attempt_at = utc_now() + timedelta(minutes=5)
            session.add(
                OutboxEvent(
                    event_type="system.alert.requested",
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    deduplication_key=(
                        f"system_alert:{aggregate_type}:{aggregate_id}:reconcile:{attempt_number}"
                    ),
                    payload={
                        "operation_id": str(operation.id),
                        "operation_item_id": (
                            str(claim.operation_item_id)
                            if claim.operation_item_id is not None
                            else None
                        ),
                        "queue_item_id": str(queue_item.id),
                        "error_code": "calendar_reconciliation_conflict",
                        "match_count": match_count,
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )

    def _claim_age(self, claim: ClaimedOperation) -> timedelta:
        with self.session_factory() as session:
            if claim.operation_item_id is not None:
                item = session.get(OperationItem, claim.operation_item_id)
                if item is None:
                    raise DocketError(
                        code="invalid_operation_state",
                        message="Operation item disappeared during reconciliation.",
                    )
                return utc_now() - _as_utc(item.updated_at)
            operation = session.get(Operation, claim.operation_id)
            if operation is None:
                raise DocketError(
                    code="invalid_operation_state",
                    message="Operation disappeared during reconciliation.",
                )
            return utc_now() - _as_utc(operation.updated_at)

    def reconcile_once(self) -> bool:
        claim = self.claim_reconciliation()
        if claim is None:
            return False
        if claim.operation_type in GMAIL_MUTATION_ACTION_TYPES:
            if self.gmail_provider is None:
                self.finish_error(
                    claim,
                    GmailProviderError(
                        "gmail_provider_disabled",
                        "The Gmail mutation provider is disabled.",
                        transient=False,
                    ),
                )
                return True
            gmail_request = claim.gmail_request()
            try:
                gmail_current = self.gmail_provider.get_label_state(gmail_request)
            except GmailProviderError as exc:
                if exc.transient:
                    self._defer_reconciliation(
                        claim,
                        error_code=exc.code,
                        delay_seconds=8,
                    )
                else:
                    self.finish_error(claim, exc)
                return True
            if gmail_request.remove_label_id not in gmail_current.label_ids:
                self.finish_gmail_success(
                    claim,
                    GmailMutationResult(
                        message_id=gmail_current.message_id,
                        source_version=gmail_current.source_version,
                        label_ids=gmail_current.label_ids,
                        provider_request_id=gmail_current.provider_request_id,
                        disposition="reconciled",
                    ),
                )
                return True
            age = self._claim_age(claim)
            if age.total_seconds() >= self.consistency_window_seconds:
                self._finish_reconciliation_no_match(claim)
            else:
                remaining = max(
                    1,
                    self.consistency_window_seconds - int(age.total_seconds()),
                )
                self._defer_reconciliation(
                    claim,
                    error_code="gmail_consistency_window",
                    delay_seconds=remaining,
                )
            return True
        calendar_request = claim.calendar_request()
        try:
            if claim.operation_type == "calendar_create_event":
                matches = self.provider.find_by_correlation(calendar_request)
            else:
                calendar_current = self.provider.get_event(calendar_request)
                matches = [calendar_current] if calendar_current is not None else []
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
                        external_event_id=str(calendar_request.external_event_id),
                        provider_etag=None,
                        provider_request_id=None,
                        snapshot={**calendar_request.snapshot(), "status": "cancelled"},
                    )
                )
                self.finish_success(claim, cancelled)
                return True
            calendar_current = matches[0]
            if calendar_current.provider_etag != calendar_request.provider_etag:
                self._finish_reconciliation_conflict(claim, match_count=1)
                return True
            age = self._claim_age(claim)
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
        exact = [match for match in matches if event_matches_request(match, calendar_request)]
        if len(matches) == 1 and len(exact) == 1:
            self.finish_success(claim, exact[0])
        elif len(matches) == 0:
            if claim.operation_type in {
                "calendar_update_event",
                "calendar_update_reminders",
            }:
                self._finish_reconciliation_conflict(claim, match_count=0)
                return True
            age = self._claim_age(claim)
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
