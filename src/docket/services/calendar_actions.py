import calendar as month_calendar
import hmac
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import (
    ActionStatus,
    ApprovalStatus,
    CommandStatus,
    IntentAuthority,
    OperationStatus,
    OutboxStatus,
    QueueItemStatus,
    QueuePresentation,
)
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarEventCache,
    CalendarLink,
    CalendarReminderPlan,
    CalendarSyncState,
    CanonicalEvent,
    CommandRequest,
    Entity,
    Operation,
    OutboxEvent,
    ProviderEventBinding,
    QueueItem,
    QueueItemSource,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.schemas.actions import (
    CancelCalendarEventProposal,
    CreateCalendarEventProposal,
    DirectExecutionResult,
    InferredCalendarEventInput,
    ProposalResult,
    ProposeCalendarEventInput,
    UpdateCalendarEventProposal,
    UpdateCalendarRemindersProposal,
)
from docket.schemas.calendar import (
    CalendarProfileResult,
    CalendarReminderPlanInput,
    StandaloneCalendarEventInput,
    TimedEventTiming,
)
from docket.security import issue_approval_token, issue_short_code, short_code_sha256
from docket.services.calendar_profile import CalendarProfileService
from docket.services.operation_materialization import operation_idempotency_key
from docket.services.operational_logs import enqueue_action_system_log
from docket.services.proposal_dedup import find_materially_identical_pending_proposal
from docket.services.queue import queue_projection_date
from docket.services.source_context import validate_configured_discord_source

_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _replayed_proposal(result: dict[str, Any]) -> ProposalResult:
    replayed = dict(result)
    replayed["disposition"] = "replayed_request"
    return ProposalResult.model_validate(replayed)


def _replayed_direct(result: dict[str, Any]) -> DirectExecutionResult:
    replayed = dict(result)
    replayed["disposition"] = "replayed_request"
    return DirectExecutionResult.model_validate(replayed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _event_bounds(
    event: StandaloneCalendarEventInput,
) -> tuple[datetime, datetime, date, date]:
    timing = event.timing
    if isinstance(timing, TimedEventTiming):
        zone = ZoneInfo(timing.timezone)
        fold = timing.fold or 0
        start = timing.start_local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        end = timing.end_local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        return start, end, timing.start_local.date(), timing.end_local.date()
    zone = ZoneInfo(timing.timezone)
    start = datetime.combine(timing.start_date, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(timing.end_date, time.min, tzinfo=zone).astimezone(UTC)
    return start, end, timing.start_date, timing.end_date


def _add_months(value: date, months: int) -> tuple[int, int]:
    absolute = value.year * 12 + value.month - 1 + months
    return absolute // 12, absolute % 12 + 1


def _recurrence_dates(event: StandaloneCalendarEventInput) -> list[date]:
    _start_at, _end_at, start_date, _end_date = _event_bounds(event)
    recurrence = event.recurrence
    if recurrence is None:
        return [start_date]
    candidates: list[date] = []
    until = recurrence.until_date
    if recurrence.frequency == "daily":
        current = start_date
        while len(candidates) < (recurrence.count or 1000):
            if until is not None and current > until:
                break
            candidates.append(current)
            current += timedelta(days=recurrence.interval)
    elif recurrence.frequency == "weekly":
        selected = {_WEEKDAY_CODES.index(day) for day in recurrence.weekdays}
        current = start_date
        while len(candidates) < (recurrence.count or 1000):
            if until is not None and current > until:
                break
            week = (current - start_date).days // 7
            if week % recurrence.interval == 0 and current.weekday() in selected:
                candidates.append(current)
            current += timedelta(days=1)
    else:
        month_index = 0
        while len(candidates) < (recurrence.count or 1000):
            year, month = _add_months(start_date, month_index * recurrence.interval)
            last_day = month_calendar.monthrange(year, month)[1]
            for day in recurrence.month_days:
                if day > last_day:
                    continue
                candidate = date(year, month, day)
                if candidate < start_date:
                    continue
                if until is not None and candidate > until:
                    return _apply_recurrence_exceptions(candidates, recurrence)
                candidates.append(candidate)
                if recurrence.count is not None and len(candidates) >= recurrence.count:
                    return _apply_recurrence_exceptions(candidates, recurrence)
            month_index += 1
            if until is not None:
                first_of_next = date(*_add_months(start_date, month_index * recurrence.interval), 1)
                if first_of_next > until:
                    break
    return _apply_recurrence_exceptions(candidates, recurrence)


def _apply_recurrence_exceptions(candidates: list[date], recurrence: Any) -> list[date]:
    excluded = set(recurrence.excluded_dates)
    dates = {candidate for candidate in candidates if candidate not in excluded}
    dates.update(recurrence.additional_dates)
    return sorted(dates)


def _occurrence_intervals(
    event: StandaloneCalendarEventInput,
) -> list[tuple[datetime, datetime]]:
    timing = event.timing
    dates = _recurrence_dates(event)
    zone = ZoneInfo(timing.timezone)
    if isinstance(timing, TimedEventTiming):
        fold = timing.fold or 0
        duration = timing.end_local - timing.start_local
        return [
            (
                datetime.combine(occurrence, timing.start_local.time())
                .replace(tzinfo=zone, fold=fold)
                .astimezone(UTC),
                datetime.combine(
                    occurrence + timedelta(days=duration.days),
                    timing.end_local.time(),
                )
                .replace(tzinfo=zone, fold=fold)
                .astimezone(UTC),
            )
            for occurrence in dates
        ]
    day_span = timing.end_date - timing.start_date
    return [
        (
            datetime.combine(occurrence, time.min, tzinfo=zone).astimezone(UTC),
            datetime.combine(occurrence + day_span, time.min, tzinfo=zone).astimezone(UTC),
        )
        for occurrence in dates
    ]


def _provider_snapshot(row: CalendarEventCache) -> dict[str, Any]:
    return {
        "provider_event_id": row.provider_event_id,
        "provider_etag": row.provider_etag,
        "status": row.status,
        "summary": row.summary,
        "location": row.location,
        "is_all_day": row.is_all_day,
        "start_at": _as_utc(row.start_at).isoformat() if row.start_at else None,
        "end_at": _as_utc(row.end_at).isoformat() if row.end_at else None,
        "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "timezone": row.timezone,
        "recurrence_kind": row.recurrence_kind,
        "system_tags": list(row.system_tags),
        "operator_tags": list(row.operator_tags),
        "priority": row.priority,
        "priority_basis": row.priority_basis,
        "provider_reminders": dict(row.provider_reminders),
        "synced_at": _as_utc(row.synced_at).isoformat(),
    }


class CalendarActionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _start_command(
        self,
        request: ProposeCalendarEventInput | InferredCalendarEventInput,
        *,
        operation_name: str,
    ) -> tuple[CommandRequest, ProposalResult | DirectExecutionResult | None]:
        payload = request.model_dump(mode="json")
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing is not None:
            if existing.operation_name != operation_name or existing.input_sha256 != input_sha256:
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation=operation_name,
                )
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                if existing.result.get("operation_id") is not None:
                    return existing, _replayed_direct(existing.result)
                return existing, _replayed_proposal(existing.result)
            raise DocketError(
                code="request_in_progress",
                message="The Calendar proposal exists but has not completed successfully.",
                details={"request_key": request.request_key, "status": existing.status},
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name=operation_name,
            input_sha256=input_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    def _explicit_entity_bindings(
        self,
        request: ProposeCalendarEventInput | InferredCalendarEventInput,
        canonical_event: CanonicalEvent | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if isinstance(request, InferredCalendarEventInput):
            return (
                list(canonical_event.entity_refs) if canonical_event is not None else [],
                list(canonical_event.context_labels) if canonical_event is not None else [],
            )
        if request.entity_bindings is None:
            entity_refs = list(canonical_event.entity_refs) if canonical_event is not None else []
        else:
            entity_refs = []
            seen: set[tuple[uuid.UUID, str]] = set()
            for binding in request.entity_bindings:
                key = (binding.entity_id, binding.role)
                if key in seen:
                    continue
                seen.add(key)
                entity = self.session.get(Entity, binding.entity_id)
                if entity is None or entity.status != "active":
                    raise DocketError(
                        code="event_entity_unresolved",
                        message=(
                            "Every explicit event entity binding must reference one active "
                            "canonical entity."
                        ),
                        details={"entity_id": str(binding.entity_id)},
                    )
                entity_refs.append(
                    {
                        "entity_id": str(entity.id),
                        "entity_class": entity.entity_class,
                        "canonical_name": entity.canonical_name,
                        "role": binding.role,
                    }
                )
        context_labels = (
            list(request.context_labels)
            if request.context_labels is not None
            else list(canonical_event.context_labels)
            if canonical_event is not None
            else []
        )
        return entity_refs, context_labels

    @staticmethod
    def _finish_command(
        command: CommandRequest, result: ProposalResult | DirectExecutionResult
    ) -> None:
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()

    def _validate_target(
        self,
        request: ProposeCalendarEventInput | InferredCalendarEventInput,
        *,
        authority: IntentAuthority,
    ) -> tuple[Account, CalendarProfileResult, CalendarSyncState]:
        settings = get_settings()
        account = self.session.get(Account, request.account_id)
        if (
            account is None
            or account.provider != "google"
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="invalid_account",
                message="The action requires an enabled Google Calendar account.",
                details={"account_id": str(request.account_id)},
            )
        if not hmac.compare_digest(request.calendar_id, settings.google_calendar_id):
            raise DocketError(
                code="calendar_not_allowed",
                message="The requested calendar is not the configured Docket calendar.",
            )
        profile = CalendarProfileService(self.session).get()
        if profile.proposal_mode == "off" and authority == IntentAuthority.INFERRED:
            raise DocketError(
                code="calendar_proposals_disabled",
                message="The Calendar profile currently suppresses new proposals.",
            )
        state = self.session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == account.id,
                CalendarSyncState.calendar_id == request.calendar_id,
            )
        )
        now = utc_now()
        if (
            state is None
            or state.status != "current"
            or state.last_success_at is None
            or (now - _as_utc(state.last_success_at)).total_seconds()
            > settings.calendar_stale_seconds
        ):
            raise DocketError(
                code="calendar_freshness_required",
                message="A current complete Calendar snapshot is required before proposal.",
            )
        return account, profile, state

    def _target_event(
        self,
        account_id: uuid.UUID,
        calendar_id: str,
        provider_event_id: str,
    ) -> CalendarEventCache:
        matches = list(
            self.session.scalars(
                select(CalendarEventCache).where(
                    CalendarEventCache.account_id == account_id,
                    CalendarEventCache.calendar_id == calendar_id,
                    CalendarEventCache.provider_event_id == provider_event_id,
                    CalendarEventCache.status != "cancelled",
                )
            )
        )
        if len(matches) != 1:
            raise DocketError(
                code=("calendar_event_not_found" if not matches else "calendar_event_ambiguous"),
                message="The exact Calendar event could not be resolved safely.",
                details={"provider_event_id": provider_event_id},
            )
        target = matches[0]
        if target.recurrence_kind == "recurring" and target.recurring_event_id is None:
            raise DocketError(
                code="calendar_series_scope_required",
                message=(
                    "A recurring master requires explicit series scope; "
                    "one occurrence requires its exact provider event ID."
                ),
            )
        if target.has_attendees or target.organizer_is_self is False:
            raise DocketError(
                code="calendar_event_not_private",
                message=(
                    "Docket will not modify an attendee-bearing or externally organized event."
                ),
            )
        return target

    def _target_series(
        self,
        account_id: uuid.UUID,
        calendar_id: str,
        provider_event_id: str,
    ) -> tuple[CalendarEventCache, CalendarLink]:
        link = self._existing_link(account_id, calendar_id, provider_event_id)
        if (
            link is None
            or link.recurrence_kind != "recurring"
            or link.provider_etag is None
            or link.synced_snapshot.get("status") == "cancelled"
        ):
            raise DocketError(
                code="calendar_series_not_found",
                message="The exact Docket-linked recurring series could not be resolved safely.",
            )
        instances = list(
            self.session.scalars(
                select(CalendarEventCache).where(
                    CalendarEventCache.account_id == account_id,
                    CalendarEventCache.calendar_id == calendar_id,
                    CalendarEventCache.recurring_event_id == provider_event_id,
                    CalendarEventCache.status != "cancelled",
                )
            )
        )
        if not instances:
            raise DocketError(
                code="calendar_series_not_found",
                message="The recurring series has no current instances in Docket's fresh cache.",
            )
        if any(row.has_attendees or row.organizer_is_self is False for row in instances):
            raise DocketError(
                code="calendar_event_not_private",
                message=(
                    "Docket will not modify an attendee-bearing or externally organized series."
                ),
            )
        representative = min(
            instances,
            key=lambda row: (
                _as_utc(row.start_at)
                if row.start_at is not None
                else datetime.combine(
                    row.start_date or date.max,
                    time.min,
                    tzinfo=ZoneInfo(row.timezone or get_settings().timezone),
                ).astimezone(UTC),
                row.provider_event_id,
            ),
        )
        snapshot = link.synced_snapshot
        reminders = snapshot.get("reminders")
        target = CalendarEventCache(
            account_id=account_id,
            calendar_id=calendar_id,
            provider_event_id=provider_event_id,
            snapshot_generation=representative.snapshot_generation,
            recurring_event_id=None,
            status=str(snapshot.get("status") or representative.status),
            summary=(
                str(snapshot["summary"])
                if isinstance(snapshot.get("summary"), str)
                else representative.summary
            ),
            location=(
                str(snapshot["location"])
                if isinstance(snapshot.get("location"), str)
                else representative.location
            ),
            is_all_day=representative.is_all_day,
            start_at=representative.start_at,
            end_at=representative.end_at,
            start_date=representative.start_date,
            end_date=representative.end_date,
            timezone=representative.timezone,
            provider_etag=link.provider_etag,
            provider_updated_at=representative.provider_updated_at,
            synced_at=representative.synced_at,
            has_attendees=False,
            organizer_is_self=True,
            recurrence_kind="recurring",
            system_tags=list(link.system_tags),
            operator_tags=list(link.operator_tags),
            priority=link.priority,
            priority_basis=link.priority_basis,
            provider_reminders=(
                dict(reminders)
                if isinstance(reminders, dict)
                else dict(representative.provider_reminders)
            ),
        )
        return target, link

    def _conflicts(
        self,
        *,
        account_id: uuid.UUID,
        calendar_id: str,
        event: StandaloneCalendarEventInput,
        exclude_provider_event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        intervals = _occurrence_intervals(event)
        if not intervals:
            return []
        rows = self.session.scalars(
            select(CalendarEventCache).where(
                CalendarEventCache.account_id == account_id,
                CalendarEventCache.calendar_id == calendar_id,
                CalendarEventCache.status.in_(("confirmed", "tentative")),
            )
        ).all()
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            if exclude_provider_event_id is not None and exclude_provider_event_id in {
                row.provider_event_id,
                row.recurring_event_id,
            }:
                continue
            if row.is_all_day:
                if row.start_date is None or row.end_date is None:
                    continue
                zone = ZoneInfo(row.timezone or get_settings().timezone)
                row_start = datetime.combine(row.start_date, time.min, tzinfo=zone).astimezone(UTC)
                row_end = datetime.combine(row.end_date, time.min, tzinfo=zone).astimezone(UTC)
            else:
                if row.start_at is None or row.end_at is None:
                    continue
                row_start, row_end = _as_utc(row.start_at), _as_utc(row.end_at)
            if not any(start < row_end and end > row_start for start, end in intervals):
                continue
            conflicts.append(
                {
                    "provider_event_id": row.provider_event_id,
                    "provider_etag": row.provider_etag,
                    "summary": row.summary,
                    "start_at": row_start.isoformat(),
                    "end_at": row_end.isoformat(),
                    "can_cancel": not row.has_attendees and row.organizer_is_self is not False,
                    "provider_before": {
                        "provider_event_id": row.provider_event_id,
                        "provider_etag": row.provider_etag,
                        "status": row.status,
                        "summary": row.summary,
                        "location": row.location,
                        "is_all_day": row.is_all_day,
                        "start_at": row_start.isoformat(),
                        "end_at": row_end.isoformat(),
                        "start_date": row.start_date.isoformat() if row.start_date else None,
                        "end_date": row.end_date.isoformat() if row.end_date else None,
                        "timezone": row.timezone,
                        "recurrence_kind": row.recurrence_kind,
                        "system_tags": list(row.system_tags),
                        "operator_tags": list(row.operator_tags),
                        "priority": row.priority,
                        "priority_basis": row.priority_basis,
                        "provider_reminders": dict(row.provider_reminders),
                    },
                }
            )
            if len(conflicts) == 10:
                break
        return conflicts

    def _existing_link(
        self, account_id: uuid.UUID, calendar_id: str, provider_event_id: str
    ) -> CalendarLink | None:
        return self.session.scalar(
            select(CalendarLink).where(
                CalendarLink.account_id == account_id,
                CalendarLink.calendar_id == calendar_id,
                CalendarLink.external_event_id == provider_event_id,
            )
        )

    @staticmethod
    def _action_type(
        proposal: CreateCalendarEventProposal
        | UpdateCalendarEventProposal
        | UpdateCalendarRemindersProposal
        | CancelCalendarEventProposal,
    ) -> str:
        return {
            "create": "calendar_create_event",
            "update": "calendar_update_event",
            "reminders": "calendar_update_reminders",
            "cancel": "calendar_cancel_event",
        }[proposal.kind]

    @staticmethod
    def _reminder_plan(
        proposal: CreateCalendarEventProposal
        | UpdateCalendarEventProposal
        | UpdateCalendarRemindersProposal
        | CancelCalendarEventProposal,
        profile: CalendarProfileResult,
    ) -> CalendarReminderPlanInput | None:
        if isinstance(proposal, CreateCalendarEventProposal):
            return proposal.event.reminder_plan or CalendarReminderPlanInput(
                delivery_channels=profile.default_reminder_delivery_channels,
                lead_seconds=profile.default_reminder_lead_seconds,
            )
        if isinstance(proposal, UpdateCalendarEventProposal):
            if proposal.reminder_disposition == "replace":
                return proposal.reminder_plan
            if proposal.reminder_disposition == "disable":
                return CalendarReminderPlanInput(lead_seconds=[])
            return None
        if isinstance(proposal, UpdateCalendarRemindersProposal):
            return proposal.reminder_plan
        return CalendarReminderPlanInput(lead_seconds=[])

    def propose(self, request: ProposeCalendarEventInput) -> ProposalResult:
        result = self._formulate(
            request,
            authority=IntentAuthority.INFERRED,
            operation_name="docket_propose_calendar_event",
        )
        if not isinstance(result, ProposalResult):
            raise AssertionError("inferred Calendar intent bypassed its decision boundary")
        return result

    def apply_explicit(
        self, request: ProposeCalendarEventInput
    ) -> ProposalResult | DirectExecutionResult:
        return self._formulate(
            request,
            authority=IntentAuthority.EXPLICIT_USER,
            operation_name="docket_apply_calendar_intent",
        )

    def formulate_inferred(self, request: InferredCalendarEventInput) -> ProposalResult:
        result = self._formulate(
            request,
            authority=IntentAuthority.INFERRED,
            operation_name="docket_formulate_inferred_calendar_intent",
        )
        if not isinstance(result, ProposalResult):
            raise AssertionError("inferred Calendar intent bypassed its decision boundary")
        return result

    def _formulate(
        self,
        request: ProposeCalendarEventInput | InferredCalendarEventInput,
        *,
        authority: IntentAuthority,
        operation_name: str,
    ) -> ProposalResult | DirectExecutionResult:
        if isinstance(request, ProposeCalendarEventInput):
            validate_configured_discord_source(self.session, request.source, request.actor_id)
        command, replay = self._start_command(request, operation_name=operation_name)
        if replay is not None:
            return replay
        account, profile, state = self._validate_target(request, authority=authority)
        if (
            authority == IntentAuthority.EXPLICIT_USER
            and get_settings().calendar_write_mode() == "disabled"
        ):
            raise DocketError(
                code="external_writes_disabled",
                message="External Calendar writes are disabled.",
            )
        proposal = request.proposal
        action_type = self._action_type(proposal)
        definition = get_action_definition(action_type)
        reminder_plan = self._reminder_plan(proposal, profile)
        target: CalendarEventCache | None = None
        link: CalendarLink | None = None
        event: StandaloneCalendarEventInput | None = None
        conflicts: list[dict[str, Any]] = []
        logical_key: str
        target_scope = "event"
        canonical_event: CanonicalEvent | None = None

        if isinstance(proposal, CreateCalendarEventProposal):
            event = proposal.event.model_copy(update={"reminder_plan": None})
            intervals = _occurrence_intervals(event)
            if (
                not intervals
                or intervals[0][0] < _as_utc(state.window_start)
                or (intervals[-1][1] > _as_utc(state.window_end))
            ):
                raise DocketError(
                    code="calendar_event_outside_fresh_window",
                    message="The proposed event falls outside Docket's fresh Calendar window.",
                )
            logical_key = f"standalone:{command.id}"
            conflicts = self._conflicts(
                account_id=account.id,
                calendar_id=request.calendar_id,
                event=event,
            )
        else:
            target_scope = proposal.target_scope
            if target_scope == "series":
                target, link = self._target_series(
                    account.id,
                    request.calendar_id,
                    proposal.provider_event_id,
                )
            else:
                target = self._target_event(
                    account.id, request.calendar_id, proposal.provider_event_id
                )
                link = self._existing_link(
                    account.id, request.calendar_id, proposal.provider_event_id
                )
            logical_key = (
                link.logical_key if link is not None else f"provider:{proposal.provider_event_id}"
            )
            if isinstance(proposal, UpdateCalendarEventProposal):
                event = proposal.replacement
                conflicts = self._conflicts(
                    account_id=account.id,
                    calendar_id=request.calendar_id,
                    event=event,
                    exclude_provider_event_id=target.provider_event_id,
                )

        requested_canonical_id = (
            request.canonical_event_id if isinstance(request, InferredCalendarEventInput) else None
        )
        if requested_canonical_id is not None:
            canonical_event = self.session.get(CanonicalEvent, requested_canonical_id)
            if canonical_event is None:
                raise DocketError(
                    code="canonical_event_not_found",
                    message="The inferred formulation lost its canonical event binding.",
                )
        elif target is not None:
            binding = self.session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.account_id == account.id,
                    ProviderEventBinding.calendar_id == request.calendar_id,
                    ProviderEventBinding.provider_event_id == target.provider_event_id,
                )
            )
            canonical_event = (
                self.session.get(CanonicalEvent, binding.canonical_event_id)
                if binding is not None
                else None
            )
        if canonical_event is None:
            entity_refs, context_labels = self._explicit_entity_bindings(request, None)
            canonical_event = CanonicalEvent(
                canonical_key=f"{authority.value}:{command.id}",
                title=(
                    event.title
                    if event is not None
                    else (target.summary if target is not None else "Calendar event")
                ),
                status=(
                    "proposed" if authority == IntentAuthority.INFERRED or conflicts else "active"
                ),
                event_spec=(
                    event.model_dump(mode="json")
                    if event is not None
                    else {"provider_snapshot": _provider_snapshot(target)}
                    if target is not None
                    else {}
                ),
                reminder_plan=(
                    reminder_plan.model_dump(mode="json") if reminder_plan is not None else None
                ),
                entity_refs=entity_refs,
                context_labels=context_labels,
                authority=authority.value,
            )
            self.session.add(canonical_event)
            self.session.flush()
        else:
            entity_refs, context_labels = self._explicit_entity_bindings(
                request,
                canonical_event,
            )
        if link is None:
            logical_key = f"canonical_event:{canonical_event.id}"
        # Conflicts are advisory evidence. They may require an explicit resolution,
        # but never make an otherwise valid formulation fail validation.

        plan_payload = reminder_plan.model_dump(mode="json") if reminder_plan is not None else None
        plan_sha256 = sha256_json(plan_payload) if plan_payload is not None else None
        if (
            isinstance(proposal, UpdateCalendarEventProposal)
            and proposal.reminder_disposition == "preserve"
            and link is not None
        ):
            plan_sha256 = link.reminder_plan_sha256
        event_payload = event.model_dump(mode="json") if event is not None else None
        priority = (
            event.priority if event is not None else (target.priority if target else "normal")
        )
        priority_basis = (
            "default" if event is not None else (target.priority_basis if target else "default")
        )
        parameters: dict[str, Any] = {
            "calendar_id": request.calendar_id,
            "logical_key": logical_key,
            "event": event_payload,
            "reminder_plan": plan_payload,
            "reminder_plan_sha256": plan_sha256,
            "priority": priority,
            "priority_basis": priority_basis,
            "target_scope": target_scope,
            "canonical_event_id": str(canonical_event.id),
            "entity_refs": entity_refs,
            "context_labels": context_labels,
            "conflict_resolution": None if conflicts else "not_applicable",
        }
        if isinstance(proposal, UpdateCalendarEventProposal):
            parameters["reminder_disposition"] = proposal.reminder_disposition
        if isinstance(proposal, CancelCalendarEventProposal):
            parameters["reason"] = proposal.reason
        if target is not None:
            parameters.update(
                {
                    "external_event_id": target.provider_event_id,
                    "provider_etag": target.provider_etag,
                    "provider_before": _provider_snapshot(target),
                }
            )
        parameters_sha256 = sha256_json(parameters)
        material_fingerprint = sha256_json(
            {
                "action_type": action_type,
                "account_id": str(account.id),
                "calendar_id": request.calendar_id,
                "event": event_payload,
                "reminder_plan": plan_payload,
                "reminder_plan_sha256": plan_sha256,
                "reminder_disposition": parameters.get("reminder_disposition"),
                "priority": priority,
                "priority_basis": priority_basis,
                "external_event_id": (target.provider_event_id if target is not None else None),
                "provider_etag": target.provider_etag if target is not None else None,
                "target_scope": target_scope,
                "entity_refs": entity_refs,
                "context_labels": context_labels,
                "reason": parameters.get("reason"),
            }
        )
        preview: dict[str, Any] = {
            "action_type": action_type,
            "target": {
                "account_id": str(account.id),
                "calendar_id": request.calendar_id,
                "logical_key": logical_key,
                "scope": target_scope,
            },
            "event": event_payload,
            "before": _provider_snapshot(target) if target is not None else None,
            "reminder_plan": plan_payload,
            "reminder_disposition": parameters.get("reminder_disposition"),
            "classification": {
                "recurrence_kind": (
                    event.recurrence_kind
                    if event is not None
                    else (target.recurrence_kind if target else "one_time")
                ),
                "system_tags": (
                    event.system_tags
                    if event is not None
                    else (list(target.system_tags) if target else [])
                ),
                "operator_tags": (
                    list(event.operator_tags)
                    if event is not None
                    else (list(target.operator_tags) if target else [])
                ),
                "priority": priority,
                "priority_basis": priority_basis,
            },
            "entity_refs": entity_refs,
            "context_labels": context_labels,
            "conflicts": conflicts,
            "conflict_resolution": None if conflicts else "not_applicable",
            "freshness": {
                "last_success_at": _as_utc(state.last_success_at).isoformat()
                if state.last_success_at
                else None,
                "window_start": _as_utc(state.window_start).isoformat(),
                "window_end": _as_utc(state.window_end).isoformat(),
            },
        }
        if isinstance(proposal, CancelCalendarEventProposal):
            preview["reason"] = proposal.reason
        preview_sha256 = sha256_json(preview)

        now = utc_now()
        needs_decision = authority == IntentAuthority.INFERRED or bool(conflicts)
        matched = (
            find_materially_identical_pending_proposal(
                self.session,
                category="calendar_change",
                material_fingerprint=material_fingerprint,
                now=now,
            )
            if needs_decision
            else None
        )
        if matched is not None:
            result = matched.model_copy(update={"request_id": command.id})
            self.session.add(
                AuditEvent(
                    event_type="action.duplicate_suppressed",
                    entity_type="action",
                    entity_id=result.action_id,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    request_id=command.id,
                    data={
                        "material_fingerprint": material_fingerprint,
                        "matched_queue_item_id": str(result.queue_item_id),
                    },
                )
            )
            self._finish_command(command, result)
            return result

        queue_item = QueueItem(
            primary_source_item_id=(
                request.source_item_id if isinstance(request, InferredCalendarEventInput) else None
            ),
            deduplication_key=(
                f"semantic_candidate:{request.semantic_candidate_id}"
                if isinstance(request, InferredCalendarEventInput)
                else f"manual_action:{request.request_key}"
            ),
            material_fingerprint=material_fingerprint,
            category="calendar_change",
            title=self._queue_title(action_type, event, target),
            summary=self._queue_summary(action_type, event, target),
            status=(
                QueueItemStatus.AWAITING_APPROVAL.value
                if needs_decision
                else QueueItemStatus.EXECUTING.value
            ),
            priority=priority,
            presentation=(
                QueuePresentation.CONFLICT_RESOLUTION.value
                if conflicts and authority == IntentAuthority.EXPLICIT_USER
                else QueuePresentation.PROPOSAL.value
                if needs_decision
                else QueuePresentation.SUPPRESSED.value
            ),
            received_at=utc_now(),
        )
        self.session.add(queue_item)
        self.session.flush()
        if isinstance(request, InferredCalendarEventInput):
            self.session.add(
                QueueItemSource(
                    queue_item_id=queue_item.id,
                    source_item_id=request.source_item_id,
                    relationship="primary",
                )
            )
        action = Action(
            queue_item_id=queue_item.id,
            record_id=link.record_id if link is not None else None,
            action_type=action_type,
            status=(
                ActionStatus.APPROVAL_PENDING.value if needs_decision else ActionStatus.READY.value
            ),
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        target_versions: dict[str, Any] = {
            "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
            "calendar_snapshot": {
                "last_success_at": _as_utc(state.last_success_at).isoformat()
                if state.last_success_at
                else None,
                "provider_event_id": target.provider_event_id if target else None,
                "provider_etag": target.provider_etag if target else None,
                "target_scope": target_scope,
                "conflict_fingerprint": sha256_json(conflicts),
            },
        }
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type=action_type,
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=parameters_sha256,
            preview=preview,
            preview_sha256=preview_sha256,
            risk_class=definition.risk_class.value,
            authority=authority.value,
            target_versions=target_versions,
            created_by_actor_type=request.actor_type,
            created_by_actor_id=request.actor_id,
        )
        self.session.add(revision)
        self.session.flush()
        if reminder_plan is not None:
            for lead_seconds in reminder_plan.lead_seconds:
                self.session.add(
                    CalendarReminderPlan(
                        action_revision_id=revision.id,
                        lead_seconds=lead_seconds,
                        delivery_channels=list(reminder_plan.delivery_channels),
                        status="planned",
                    )
                )

        if not needs_decision:
            operation_id = uuid.uuid4()
            operation = Operation(
                id=operation_id,
                action_revision_id=revision.id,
                approval_id=None,
                idempotency_key=operation_idempotency_key(revision),
                operation_type=revision.action_type,
                account_id=account.id,
                status=OperationStatus.PENDING.value,
                provider_correlation=str(operation_id),
                next_attempt_at=now,
            )
            self.session.add(operation)
            self.session.add(
                AuditEvent(
                    event_type="action.execution_queued",
                    entity_type="action",
                    entity_id=action.id,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    request_id=command.id,
                    data={
                        "action_type": action_type,
                        "authority": authority.value,
                        "operation_id": str(operation.id),
                        "parameters_sha256": parameters_sha256,
                        "preview_sha256": preview_sha256,
                    },
                )
            )
            enqueue_action_system_log(
                self.session,
                action=action,
                revision=revision,
                state="queued",
            )
            direct_result = DirectExecutionResult(
                request_id=command.id,
                disposition="execution_queued",
                queue_item_id=queue_item.id,
                action_id=action.id,
                action_revision_id=revision.id,
                operation_id=operation.id,
                operation_status="pending",
                conflicts=[],
            )
            self._finish_command(command, direct_result)
            return direct_result

        expires_at = now + timedelta(seconds=get_settings().approval_ttl_seconds)
        approval_id = uuid.uuid4()
        signing_key = (
            get_settings().read_secret(get_settings().interaction_signing_key_file).encode()
        )
        short_code = issue_short_code(approval_id, expires_at, signing_key)
        approval_token = issue_approval_token(approval_id, expires_at, signing_key)
        approval = Approval(
            id=approval_id,
            action_revision_id=revision.id,
            status=ApprovalStatus.PENDING.value,
            short_code_sha256=short_code_sha256(short_code),
            authorized_user_id=get_settings().operator_discord_user_id,
            requested_at=now,
            expires_at=expires_at,
        )
        self.session.add(approval)
        defer_projection = (
            isinstance(request, InferredCalendarEventInput) and request.defer_projection
        )
        if not defer_projection:
            self.session.add(
                OutboxEvent(
                    event_type="discord.projection.requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=f"discord_projection:{queue_item.id}:1",
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "action_id": str(action.id),
                        "action_revision_id": str(revision.id),
                        "approval_id": str(approval.id),
                        "approval_token": approval_token,
                        "short_code": short_code,
                        "expires_at": expires_at.isoformat(),
                        "preview": preview,
                        "target_local_date": queue_projection_date(
                            queue_item, get_settings()
                        ).isoformat(),
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
        self.session.add(
            AuditEvent(
                event_type="action.proposed",
                entity_type="action",
                entity_id=action.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "action_type": action_type,
                    "revision": 1,
                    "risk_class": definition.risk_class.value,
                    "parameters_sha256": parameters_sha256,
                    "preview_sha256": preview_sha256,
                    "target_versions": target_versions,
                    "reminder_plan_sha256": plan_sha256,
                },
            )
        )
        result = ProposalResult(
            request_id=command.id,
            disposition="proposed",
            queue_item_id=queue_item.id,
            action_id=action.id,
            action_revision_id=revision.id,
            approval_id=approval.id,
            short_code=short_code,
            expires_at=expires_at,
            preview=preview,
            projection_status="deferred" if defer_projection else "pending",
        )
        self._finish_command(command, result)
        return result

    @staticmethod
    def _queue_title(
        action_type: str,
        event: StandaloneCalendarEventInput | None,
        target: CalendarEventCache | None,
    ) -> str:
        title = event.title if event is not None else (target.summary if target else "event")
        return f"{action_type}: {title}"[:512]

    @staticmethod
    def _queue_summary(
        action_type: str,
        event: StandaloneCalendarEventInput | None,
        target: CalendarEventCache | None,
    ) -> str:
        if event is not None:
            timing = event.timing
            if isinstance(timing, TimedEventTiming):
                when = f"{timing.start_local.isoformat()} to {timing.end_local.isoformat()}"
            else:
                when = f"{timing.start_date.isoformat()} to {timing.end_date.isoformat()}"
            return f"{when} {timing.timezone}"[:2000]
        return f"{action_type} for {target.summary if target else 'Calendar event'}"[:2000]
