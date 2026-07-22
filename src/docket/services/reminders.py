from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import CommandStatus, OutboxStatus
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
from docket.models import (
    Account,
    AuditEvent,
    CalendarEventCache,
    CommandRequest,
    OutboxEvent,
    ReminderRule,
    ScheduledNotification,
)
from docket.models.base import utc_now
from docket.schemas.calendar import (
    DisableReminderRuleInput,
    ReminderRuleResult,
    SetReminderRuleInput,
)
from docket.services.source_context import validate_configured_discord_source


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _event_start(event: CalendarEventCache, default_timezone: str) -> datetime | None:
    if event.is_all_day:
        if event.start_date is None:
            return None
        zone = ZoneInfo(event.timezone or default_timezone)
        return datetime.combine(event.start_date, time.min, tzinfo=zone).astimezone(UTC)
    return _aware(event.start_at).astimezone(UTC) if event.start_at is not None else None


def _event_start_key(event: CalendarEventCache, default_timezone: str) -> str | None:
    if event.is_all_day:
        if event.start_date is None:
            return None
        zone = event.timezone or default_timezone
        return f"{event.start_date.isoformat()}@{zone}"
    if event.start_at is None:
        return None
    return _aware(event.start_at).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _matches(rule: ReminderRule, event: CalendarEventCache) -> bool:
    return rule.scope == "calendar" or rule.provider_event_id in {
        event.provider_event_id,
        event.recurring_event_id,
    }


def _cancel_outbox(session: Session, notification: ScheduledNotification, code: str) -> None:
    notification.status = "cancelled"
    notification.last_error_code = code
    if notification.outbox_event_id is not None:
        event = session.get(OutboxEvent, notification.outbox_event_id)
        if event is not None and event.status == OutboxStatus.PENDING.value:
            event.status = OutboxStatus.FAILED.value
            event.last_error_code = code


def materialize_reminders(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    rule_ids: set[uuid.UUID] | None = None,
) -> int:
    """Converge enabled rules and the current complete cache into scheduled rows."""
    settings = settings or get_settings()
    now = _aware(now or utc_now()).astimezone(UTC)
    statement = select(ReminderRule)
    if rule_ids is not None:
        statement = statement.where(ReminderRule.id.in_(rule_ids))
    rules = session.scalars(statement).all()
    changed = 0
    for rule in rules:
        existing = list(
            session.scalars(
                select(ScheduledNotification).where(
                    ScheduledNotification.reminder_rule_id == rule.id
                )
            ).all()
        )
        if not rule.enabled:
            for notification in existing:
                if notification.status in {"pending", "delivering"}:
                    _cancel_outbox(session, notification, "reminder_rule_disabled")
                    changed += 1
            continue
        events = session.scalars(
            select(CalendarEventCache).where(
                CalendarEventCache.account_id == rule.account_id,
                CalendarEventCache.calendar_id == rule.calendar_id,
                CalendarEventCache.status.in_(("confirmed", "tentative")),
            )
        ).all()
        desired: set[tuple[str, str]] = set()
        for event in events:
            if not _matches(rule, event):
                continue
            event_start = _event_start(event, settings.timezone)
            start_key = _event_start_key(event, settings.timezone)
            if event_start is None or start_key is None:
                continue
            desired.add((event.provider_event_id, start_key))
            scheduled_for = event_start - timedelta(seconds=rule.lead_seconds)
            same = next(
                (
                    item
                    for item in existing
                    if item.provider_event_id == event.provider_event_id
                    and item.event_start_key == start_key
                ),
                None,
            )
            movable = next(
                (
                    item
                    for item in existing
                    if item.provider_event_id == event.provider_event_id
                    and item.status == "pending"
                    and item.outbox_event_id is None
                ),
                None,
            )
            selected = same or movable
            missed = event_start <= now
            if selected is None:
                selected = ScheduledNotification(
                    reminder_rule_id=rule.id,
                    calendar_event_id=event.id,
                    provider_event_id=event.provider_event_id,
                    event_start_key=start_key,
                    scheduled_for=max(scheduled_for, now),
                    status="failed" if missed else "pending",
                    last_error_code="missed_stale_calendar" if missed else None,
                )
                session.add(selected)
                existing.append(selected)
                changed += 1
            elif selected.status not in {"delivered", "delivering"}:
                next_status = "failed" if missed else "pending"
                if (
                    selected.event_start_key != start_key
                    or selected.scheduled_for != max(scheduled_for, now)
                    or selected.calendar_event_id != event.id
                    or selected.status != next_status
                ):
                    changed += 1
                selected.calendar_event_id = event.id
                selected.event_start_key = start_key
                selected.scheduled_for = max(scheduled_for, now)
                selected.status = next_status
                selected.last_error_code = "missed_stale_calendar" if missed else None

        for notification in existing:
            identity = (notification.provider_event_id, notification.event_start_key)
            if identity not in desired and notification.status in {"pending", "delivering"}:
                _cancel_outbox(session, notification, "calendar_event_cancelled")
                changed += 1
    return changed


def serialize_rule(rule: ReminderRule) -> dict[str, Any]:
    return {
        "rule_id": str(rule.id),
        "account_id": str(rule.account_id),
        "calendar_id": rule.calendar_id,
        "scope": rule.scope,
        "provider_event_id": rule.provider_event_id,
        "lead_seconds": rule.lead_seconds,
        "destination_channel_id": rule.destination_channel_id,
        "enabled": rule.enabled,
        "version": rule.version,
    }


class ReminderRuleService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def _target(self, account_id: uuid.UUID, calendar_id: str) -> None:
        account = self.session.get(Account, account_id)
        if account is None or account.provider != "google" or not account.enabled:
            raise DocketError(
                code="calendar_account_not_available",
                message="The selected Google account is not enabled.",
            )
        if calendar_id != self.settings.google_calendar_id:
            raise DocketError(
                code="calendar_not_allowed",
                message="The selected calendar is not the configured Docket calendar.",
            )

    def _start_command(
        self,
        *,
        request_key: str,
        operation_name: str,
        payload: dict[str, Any],
        actor_id: str,
    ) -> tuple[CommandRequest, ReminderRuleResult | None]:
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request_key)
        )
        if existing is not None:
            if existing.operation_name != operation_name or existing.input_sha256 != input_sha256:
                raise IdempotencyConflict(request_key)
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                replay = {**existing.result, "disposition": "replayed_request"}
                return existing, ReminderRuleResult.model_validate(replay)
            raise DocketError(
                code="request_in_progress",
                message="The reminder request is already in progress.",
            )
        command = CommandRequest(
            request_key=request_key,
            operation_name=operation_name,
            input_sha256=input_sha256,
            actor_type="hermes",
            actor_id=actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    def set(self, request: SetReminderRuleInput) -> ReminderRuleResult:
        validate_configured_discord_source(request.source, request.actor_id)
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="set_reminder_rule",
            payload=payload,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay
        self._target(request.account_id, request.calendar_id)
        destination = (
            request.destination_channel_id or self.settings.effective_reminder_channel_id()
        )
        if destination != self.settings.effective_reminder_channel_id():
            raise DocketError(
                code="reminder_destination_not_allowed",
                message="Reminder rules may target only the configured reminder channel.",
            )
        if request.scope == "event":
            exists = self.session.scalar(
                select(CalendarEventCache.id).where(
                    CalendarEventCache.account_id == request.account_id,
                    CalendarEventCache.calendar_id == request.calendar_id,
                    or_(
                        CalendarEventCache.provider_event_id == request.provider_event_id,
                        CalendarEventCache.recurring_event_id == request.provider_event_id,
                    ),
                    CalendarEventCache.status != "cancelled",
                )
            )
            if exists is None:
                raise DocketError(
                    code="calendar_event_not_found",
                    message="The requested reminder event is not in the current Calendar cache.",
                )

        disposition = "created"
        if request.rule_id is not None:
            rule = self.session.scalar(
                select(ReminderRule).where(ReminderRule.id == request.rule_id).with_for_update()
            )
            if rule is None:
                raise DocketError(
                    code="reminder_rule_not_found", message="Reminder rule not found."
                )
            assert request.expected_version is not None
            if rule.version != request.expected_version:
                raise VersionConflict(str(rule.id), request.expected_version, rule.version)
            rule.account_id = request.account_id
            rule.calendar_id = request.calendar_id
            rule.scope = request.scope
            rule.provider_event_id = request.provider_event_id
            rule.lead_seconds = request.lead_seconds
            rule.destination_channel_id = destination
            rule.enabled = True
            rule.version += 1
            disposition = "updated"
        else:
            rule = self.session.scalar(
                select(ReminderRule).where(
                    ReminderRule.account_id == request.account_id,
                    ReminderRule.calendar_id == request.calendar_id,
                    ReminderRule.scope == request.scope,
                    ReminderRule.provider_event_id == request.provider_event_id,
                    ReminderRule.lead_seconds == request.lead_seconds,
                    ReminderRule.destination_channel_id == destination,
                    ReminderRule.enabled.is_(True),
                )
            )
            if rule is None:
                rule = ReminderRule(
                    account_id=request.account_id,
                    calendar_id=request.calendar_id,
                    scope=request.scope,
                    provider_event_id=request.provider_event_id,
                    lead_seconds=request.lead_seconds,
                    destination_channel_id=destination,
                    enabled=True,
                    created_by_actor_id=request.actor_id,
                )
                self.session.add(rule)
                self.session.flush()
            else:
                disposition = "matched_existing"
        count = materialize_reminders(self.session, settings=self.settings, rule_ids={rule.id})
        result = ReminderRuleResult(
            request_id=command.id,
            rule_id=rule.id,
            version=rule.version,
            enabled=rule.enabled,
            disposition=disposition,
            materialized_notifications=count,
        )
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()
        self.session.add(
            AuditEvent(
                event_type=f"reminder_rule.{disposition}",
                entity_type="reminder_rule",
                entity_id=rule.id,
                actor_type="hermes",
                actor_id=request.actor_id,
                request_id=command.id,
                data={"version": rule.version},
            )
        )
        return result

    def disable(self, request: DisableReminderRuleInput) -> ReminderRuleResult:
        validate_configured_discord_source(request.source, request.actor_id)
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="disable_reminder_rule",
            payload=payload,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay
        rule = self.session.scalar(
            select(ReminderRule).where(ReminderRule.id == request.rule_id).with_for_update()
        )
        if rule is None:
            raise DocketError(code="reminder_rule_not_found", message="Reminder rule not found.")
        if rule.version != request.expected_version:
            raise VersionConflict(str(rule.id), request.expected_version, rule.version)
        rule.enabled = False
        rule.version += 1
        count = materialize_reminders(self.session, settings=self.settings, rule_ids={rule.id})
        result = ReminderRuleResult(
            request_id=command.id,
            rule_id=rule.id,
            version=rule.version,
            enabled=False,
            disposition="disabled",
            materialized_notifications=count,
        )
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()
        self.session.add(
            AuditEvent(
                event_type="reminder_rule.disabled",
                entity_type="reminder_rule",
                entity_id=rule.id,
                actor_type="hermes",
                actor_id=request.actor_id,
                request_id=command.id,
                data={"version": rule.version, "reason": request.reason},
            )
        )
        return result


class ReminderDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.clock = clock

    def run_due_once(self) -> bool:
        now = _aware(self.clock()).astimezone(UTC)
        with self.session_factory.begin() as session:
            notification = session.scalar(
                select(ScheduledNotification)
                .where(
                    ScheduledNotification.status == "pending",
                    ScheduledNotification.scheduled_for <= now,
                )
                .order_by(ScheduledNotification.scheduled_for, ScheduledNotification.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if notification is None:
                return False
            rule = session.get(ReminderRule, notification.reminder_rule_id)
            event = (
                session.get(CalendarEventCache, notification.calendar_event_id)
                if notification.calendar_event_id is not None
                else None
            )
            if rule is None or not rule.enabled:
                notification.status = "cancelled"
                notification.last_error_code = "reminder_rule_disabled"
                return True
            if event is None or event.status == "cancelled":
                notification.status = "cancelled"
                notification.last_error_code = "calendar_event_cancelled"
                return True
            event_start = _event_start(event, self.settings.timezone)
            if event_start is None or event_start <= now:
                notification.status = "failed"
                notification.last_error_code = "missed_stale_calendar"
                session.add(
                    AuditEvent(
                        event_type="calendar_notification.missed",
                        entity_type="scheduled_notification",
                        entity_id=notification.id,
                        actor_type="docket",
                        actor_id=None,
                        request_id=None,
                        data={"error_code": "missed_stale_calendar"},
                    )
                )
                return True
            key = (
                f"calendar-reminder:{rule.id}:{notification.provider_event_id}:"
                f"{notification.event_start_key}"
            )
            outbox = session.scalar(select(OutboxEvent).where(OutboxEvent.deduplication_key == key))
            if outbox is None:
                outbox = OutboxEvent(
                    event_type="discord.calendar_reminder.requested",
                    aggregate_type="scheduled_notification",
                    aggregate_id=notification.id,
                    deduplication_key=key,
                    payload={"scheduled_notification_id": str(notification.id)},
                    status=OutboxStatus.PENDING.value,
                )
                session.add(outbox)
                session.flush()
            notification.outbox_event_id = outbox.id
            notification.status = "delivering"
            session.add(
                AuditEvent(
                    event_type="calendar_notification.enqueued",
                    entity_type="scheduled_notification",
                    entity_id=notification.id,
                    actor_type="docket",
                    actor_id=None,
                    request_id=None,
                    data={"outbox_event_id": str(outbox.id)},
                )
            )
            return True
