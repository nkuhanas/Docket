from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import ApprovalStatus, OutboxStatus, RiskClass
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarEventCache,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OutboxEvent,
    QueueItem,
    ReminderRule,
    ScheduledNotification,
)
from docket.models.base import utc_now
from docket.providers.discord import DiscordProjectionAdapter, DiscordProjectionError
from docket.security import (
    issue_projection_approval_token,
    issue_projection_decision_approval_token,
    issue_projection_local_action_token,
    issue_projection_proposal_control_token,
    issue_projection_review_navigation_token,
)
from docket.services.queue import ensure_local_actions

_SUPPORTED_EVENTS = {
    "discord.projection.requested",
    "discord.projection.refresh_requested",
    "discord.thread.ensure_requested",
    "discord.thread.lifecycle_requested",
    "discord.system_alert.requested",
    "discord.system_log.requested",
    "discord.calendar_reminder.requested",
}
_PROJECTION_EVENTS = {
    "discord.projection.requested",
    "discord.projection.refresh_requested",
}
logger = structlog.get_logger(__name__)


class _NavigationCommon(TypedDict):
    revision: ActionRevision
    projection: DiscordProjection
    projection_version: int
    expires_at: datetime
    signing_key: bytes


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _verified_at(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscordProjectionError(
            "invalid_discord_ack", "Acknowledgement contained an invalid timestamp"
        ) from exc
    return _aware(parsed).astimezone(UTC)


class DiscordProjectionRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: DiscordProjectionAdapter,
        settings: Settings,
        *,
        lease_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter
        self.settings = settings
        self.lease_seconds = lease_seconds

    def _lease_one(self) -> tuple[uuid.UUID, uuid.UUID] | None:
        now = utc_now()
        with self.session_factory.begin() as session:
            event = session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(_SUPPORTED_EVENTS),
                    OutboxEvent.status == OutboxStatus.PENDING.value,
                    or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                return None
            lease_token = uuid.uuid4()
            event.status = OutboxStatus.DELIVERING.value
            event.lease_token = lease_token
            event.leased_until = now + timedelta(seconds=self.lease_seconds)
            event.attempt_count += 1
            return event.id, lease_token

    @staticmethod
    def _local_date(queue_item: QueueItem, settings: Settings) -> date:
        received = queue_item.received_at or queue_item.created_at
        return _aware(received).astimezone(ZoneInfo(settings.timezone)).date()

    @staticmethod
    def _thread_name(local_date: date) -> str:
        return f"{local_date.isoformat()} — {local_date.strftime('%A')}"

    def _daily_thread_row(self, session: Session, local_date: date) -> DiscordDailyThread:
        daily_thread = session.scalar(
            select(DiscordDailyThread).where(
                DiscordDailyThread.guild_id == self.settings.discord_guild_id,
                DiscordDailyThread.channel_id == self.settings.queue_channel_id,
                DiscordDailyThread.local_date == local_date,
            )
        )
        if daily_thread is None:
            daily_thread = DiscordDailyThread(
                guild_id=self.settings.discord_guild_id,
                channel_id=self.settings.queue_channel_id,
                local_date=local_date,
                thread_name=self._thread_name(local_date),
                status="pending",
            )
            session.add(daily_thread)
            session.flush()
        return daily_thread

    def _ensure_local_rows(
        self, event_id: uuid.UUID, lease_token: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID]:
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            queue_item = session.get(QueueItem, event.aggregate_id)
            if queue_item is None:
                raise DiscordProjectionError("queue_item_missing", "Queue item is missing")
            requested_date = event.payload.get("target_local_date")
            try:
                local_date = (
                    date.fromisoformat(str(requested_date))
                    if requested_date is not None
                    else self._local_date(queue_item, self.settings)
                )
            except ValueError as exc:
                raise DiscordProjectionError(
                    "invalid_projection_date", "Projection target date is invalid"
                ) from exc
            daily_thread = self._daily_thread_row(session, local_date)
            requested_projection = event.payload.get("projection_id")
            projection = (
                session.get(DiscordProjection, uuid.UUID(str(requested_projection)))
                if requested_projection is not None
                else None
            )
            if projection is not None and (
                projection.queue_item_id != queue_item.id
                or projection.daily_thread_id != daily_thread.id
            ):
                raise DiscordProjectionError(
                    "projection_target_changed", "Projection target binding changed"
                )
            if projection is None:
                projection = session.scalar(
                    select(DiscordProjection).where(
                        DiscordProjection.queue_item_id == queue_item.id,
                        DiscordProjection.daily_thread_id == daily_thread.id,
                    )
                )
            if projection is None:
                projection = DiscordProjection(
                    queue_item_id=queue_item.id,
                    daily_thread_id=daily_thread.id,
                    render_sha256="0" * 64,
                    component_sha256="0" * 64,
                    status="pending",
                )
                session.add(projection)
                session.flush()
            event.payload = {
                **event.payload,
                "projection_id": str(projection.id),
                "target_local_date": local_date.isoformat(),
            }
            return daily_thread.id, projection.id

    def _ensure_notification_thread(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> uuid.UUID:
        with self.session_factory.begin() as session:
            outbox = session.get(OutboxEvent, event_id)
            if (
                outbox is None
                or outbox.status != OutboxStatus.DELIVERING.value
                or outbox.lease_token != lease_token
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            notification = session.get(ScheduledNotification, outbox.aggregate_id)
            if notification is None or notification.status != "delivering":
                raise DiscordProjectionError(
                    "notification_not_delivering", "Calendar notification is not deliverable"
                )
            scheduled_date = (
                _aware(notification.scheduled_for)
                .astimezone(ZoneInfo(self.settings.timezone))
                .date()
            )
            requested_date = outbox.payload.get("target_local_date")
            try:
                local_date = (
                    date.fromisoformat(str(requested_date))
                    if requested_date is not None
                    else scheduled_date
                )
            except ValueError as exc:
                raise DiscordProjectionError(
                    "invalid_projection_date", "Reminder target date is invalid"
                ) from exc
            if local_date != scheduled_date:
                raise DiscordProjectionError(
                    "reminder_target_changed", "Reminder due-date thread binding changed"
                )
            daily_thread = (
                session.get(DiscordDailyThread, notification.daily_thread_id)
                if notification.daily_thread_id is not None
                else None
            )
            if daily_thread is not None and (
                daily_thread.guild_id != self.settings.discord_guild_id
                or daily_thread.channel_id != self.settings.queue_channel_id
                or daily_thread.local_date != local_date
            ):
                raise DiscordProjectionError(
                    "reminder_target_changed", "Stored reminder thread binding changed"
                )
            if daily_thread is None:
                daily_thread = self._daily_thread_row(session, local_date)
                notification.daily_thread_id = daily_thread.id
            outbox.payload = {
                **outbox.payload,
                "daily_thread_id": str(daily_thread.id),
                "target_local_date": local_date.isoformat(),
            }
            return daily_thread.id

    def _thread_request(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        daily_thread_id: uuid.UUID,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            event = session.get(OutboxEvent, event_id)
            daily_thread = session.get(DiscordDailyThread, daily_thread_id)
            if event is None or event.lease_token != lease_token or daily_thread is None:
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            return {
                "request_id": str(event.id),
                "daily_thread_id": str(daily_thread.id),
                "known_thread_id": daily_thread.thread_id,
                "guild_id": daily_thread.guild_id,
                "channel_id": daily_thread.channel_id,
                "local_date": daily_thread.local_date.isoformat(),
                "name": daily_thread.thread_name,
                "thread_type": "public_thread",
                "auto_archive_minutes": 10080,
            }

    def _complete_event(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.next_attempt_at = None
            event.last_error_code = None

    def _accept_thread_ack(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        daily_thread_id: uuid.UUID,
        request: dict[str, Any],
        ack: dict[str, Any],
    ) -> None:
        exact = (
            ack.get("request_id") == request["request_id"]
            and ack.get("daily_thread_id") == request["daily_thread_id"]
            and ack.get("guild_id") == request["guild_id"]
            and ack.get("channel_id") == request["channel_id"]
            and str(ack.get("thread_id", "")).isdigit()
        )
        if not exact:
            raise DiscordProjectionError(
                "invalid_discord_ack", "Thread acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            daily_thread = session.get(DiscordDailyThread, daily_thread_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
                or daily_thread is None
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            daily_thread.thread_id = str(ack["thread_id"])
            daily_thread.status = "active"
            daily_thread.auto_archive_minutes = int(ack["auto_archive_minutes"])
            daily_thread.last_verified_at = _verified_at(ack["verified_at"])
            daily_thread.last_error_code = None

    @staticmethod
    def _schedule_text(schedule: dict[str, Any]) -> str:
        day_labels = {
            "MO": "Mon",
            "TU": "Tue",
            "WE": "Wed",
            "TH": "Thu",
            "FR": "Fri",
            "SA": "Sat",
            "SU": "Sun",
        }
        days = ", ".join(day_labels.get(str(day), str(day)) for day in schedule.get("days", []))
        times = (
            f"{DiscordProjectionRunner._clock_label(schedule.get('start_time'))} to "
            f"{DiscordProjectionRunner._clock_label(schedule.get('end_time'))}"
        )
        dates = (
            f"{DiscordProjectionRunner._date_label(schedule.get('start_date'))} to "
            f"{DiscordProjectionRunner._date_label(schedule.get('end_date'))}"
        )
        timezone = str(schedule.get("timezone", ""))
        location = schedule.get("location")
        parts = [f"{days} · {times}", f"{dates} · {timezone}"]
        if location:
            parts.append(str(location))
        return "\n".join(parts)

    @staticmethod
    def _bounded(value: str, maximum: int) -> str:
        return value if len(value) <= maximum else value[: maximum - 1] + "…"

    @staticmethod
    def _date_label(value: object) -> str:
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            return str(value or "?")
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"

    @staticmethod
    def _clock_label(value: object) -> str:
        try:
            parsed = datetime.strptime(str(value), "%H:%M:%S")
        except ValueError:
            return str(value or "?")
        return parsed.strftime("%I:%M %p").lstrip("0")

    @staticmethod
    def _status_label(value: str) -> str:
        labels = {
            "awaiting_approval": "Awaiting approval",
            "executing": "In progress",
            "completed": "Completed",
            "reconciliation_required": "Needs reconciliation",
            "partial_failed": "Partially completed",
        }
        return labels.get(value, value.replace("_", " ").title())

    @staticmethod
    def _discord_timestamp(value: datetime, style: str = "F") -> str:
        if style not in {"t", "T", "d", "D", "f", "F", "s", "S", "R"}:
            raise ValueError("unsupported Discord timestamp style")
        seconds = int(_aware(value).astimezone(UTC).timestamp())
        return f"<t:{seconds}:{style}>"

    @staticmethod
    def _discord_timestamp_value(value: object, style: str = "F") -> str:
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return str(value or "Unknown")
        return DiscordProjectionRunner._discord_timestamp(parsed, style)

    @staticmethod
    def _discord_timestamp_pair(value: object) -> str:
        return (
            f"{DiscordProjectionRunner._discord_timestamp_value(value, 'F')} · "
            f"{DiscordProjectionRunner._discord_timestamp_value(value, 'R')}"
        )

    def _timestamp_label(self, value: datetime) -> str:
        return self._discord_timestamp_pair(value)

    @staticmethod
    def _action_label(value: str) -> str:
        labels = {
            "calendar_create_meeting": "Create course meeting",
            "calendar_update_meeting": "Update course meeting",
            "calendar_create_event": "Create event",
            "calendar_update_event": "Update event",
            "calendar_update_reminders": "Update reminders",
            "calendar_cancel_event": "Cancel event",
            "calendar_apply_term_schedule": "Apply term schedule",
        }
        return labels.get(value, value.replace("_", " ").title())

    @staticmethod
    def _standalone_timing(event: dict[str, Any]) -> str:
        timing = event.get("timing", {})
        if not isinstance(timing, dict):
            return "Unknown"
        if timing.get("kind") == "all_day":
            return DiscordProjectionRunner._standalone_timing_manual(event)
        bounds = DiscordProjectionRunner._standalone_bounds(event)
        if bounds is None:
            return "Unknown"
        start, end = bounds
        return (
            f"Starts {DiscordProjectionRunner._discord_timestamp(start, 'F')}\n"
            f"Ends {DiscordProjectionRunner._discord_timestamp(end, 'F')}"
        )

    @staticmethod
    def _standalone_bounds(event: dict[str, Any]) -> tuple[datetime, datetime] | None:
        timing = event.get("timing", {})
        if not isinstance(timing, dict) or timing.get("kind") == "all_day":
            return None
        try:
            start = datetime.fromisoformat(str(timing.get("start_local")))
            end = datetime.fromisoformat(str(timing.get("end_local")))
            zone = ZoneInfo(str(timing.get("timezone")))
            fold = int(timing.get("fold") or 0)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return None
        return (
            start.replace(tzinfo=zone, fold=fold).astimezone(UTC),
            end.replace(tzinfo=zone, fold=fold).astimezone(UTC),
        )

    @staticmethod
    def _standalone_timing_manual(event: dict[str, Any]) -> str:
        timing = event.get("timing", {})
        if not isinstance(timing, dict):
            return "Unknown"
        timezone = str(timing.get("timezone", "?"))
        if timing.get("kind") == "all_day":
            return (
                f"All day · {DiscordProjectionRunner._date_label(timing.get('start_date'))} "
                f"through {DiscordProjectionRunner._date_label(timing.get('end_date'))} "
                f"(end exclusive)\nCalendar timezone: {timezone}"
            )
        try:
            start = datetime.fromisoformat(str(timing.get("start_local")))
            end = datetime.fromisoformat(str(timing.get("end_local")))
        except ValueError:
            return "Unknown"
        start_date = f"{start.strftime('%a, %b')} {start.day}, {start.year}"
        start_time = start.strftime("%I:%M %p").lstrip("0")
        end_time = end.strftime("%I:%M %p").lstrip("0")
        if start.date() == end.date():
            value = f"{start_date} · {start_time} to {end_time}"
        else:
            end_date = f"{end.strftime('%a, %b')} {end.day}, {end.year}"
            value = f"{start_date} · {start_time} through {end_date} · {end_time}"
        return f"{value}\n{timezone}"

    @staticmethod
    def _standalone_timing_compact(event: dict[str, Any]) -> str:
        timing = event.get("timing", {})
        if not isinstance(timing, dict) or timing.get("kind") == "all_day":
            return DiscordProjectionRunner._standalone_timing_manual(event).splitlines()[0]
        bounds = DiscordProjectionRunner._standalone_bounds(event)
        if bounds is None:
            return "Unknown"
        start, end = bounds
        return (
            f"{DiscordProjectionRunner._discord_timestamp(start, 'F')} to "
            f"{DiscordProjectionRunner._discord_timestamp(end, 't')}"
        )

    def _provider_timing(self, event: dict[str, Any]) -> str:
        timezone_name = str(event.get("timezone") or self.settings.timezone)
        if bool(event.get("is_all_day")):
            return (
                f"All day · {self._date_label(event.get('start_date'))} through "
                f"{self._date_label(event.get('end_date'))} (end exclusive)"
            )
        try:
            ZoneInfo(timezone_name)
            start = datetime.fromisoformat(str(event.get("start_at")).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(event.get("end_at")).replace("Z", "+00:00"))
        except (ValueError, ZoneInfoNotFoundError):
            return "Unknown"
        start = _aware(start).astimezone(UTC)
        end = _aware(end).astimezone(UTC)
        return f"{self._discord_timestamp(start, 'F')} to {self._discord_timestamp(end, 't')}"

    @staticmethod
    def _reminder_text(plan: object) -> str:
        if not isinstance(plan, dict):
            return "Preserve current provider reminders"
        if "useDefault" in plan or "overrides" in plan:
            if bool(plan.get("useDefault")):
                return "Provider calendar defaults"
            overrides = plan.get("overrides", [])
            if not isinstance(overrides, list):
                overrides = []
            minutes = sorted(
                {
                    int(item["minutes"])
                    for item in overrides
                    if isinstance(item, dict)
                    and item.get("method") == "popup"
                    and isinstance(item.get("minutes"), int)
                }
            )
            return (
                "Disabled"
                if not minutes
                else ", ".join(f"{value} minute{'s' if value != 1 else ''}" for value in minutes)
            )
        leads = plan.get("lead_seconds", [])
        if not isinstance(leads, list):
            leads = []
        lead_text = (
            "Disabled"
            if not leads
            else ", ".join(
                f"{int(value) // 60} minute{'s' if int(value) != 60 else ''}" for value in leads
            )
        )
        return f"{lead_text}\nGoogle Calendar popup + Docket daily thread"

    @staticmethod
    def _reminder_lead_text(plan: object) -> str:
        return DiscordProjectionRunner._reminder_text(plan).splitlines()[0]

    def _calendar_change_text(
        self,
        preview: dict[str, Any],
        action_type: str,
    ) -> str | None:
        if action_type == "calendar_cancel_event":
            return "Remove this event from your configured Docket calendar."
        before = preview.get("before")
        event = preview.get("event")
        if not isinstance(before, dict):
            return None
        changes: list[str] = []

        def add_delta(label: str, old_value: str, new_value: str) -> None:
            changes.append(f"{label}\nBefore: {old_value}\nAfter: {new_value}")

        if isinstance(event, dict):
            old_title = str(before.get("summary") or "Untitled")
            new_title = str(event.get("title") or "Untitled")
            if old_title != new_title:
                add_delta("Title", old_title, new_title)
            old_timing = self._provider_timing(before)
            new_timing = self._standalone_timing_compact(event)
            if old_timing != new_timing:
                add_delta("Time", old_timing, new_timing)
            old_location = str(before.get("location") or "No location")
            new_location = str(event.get("location") or "No location")
            if old_location != new_location:
                add_delta("Location", old_location, new_location)
        reminder_disposition = preview.get("reminder_disposition")
        if action_type == "calendar_update_reminders" or reminder_disposition in {
            "replace",
            "disable",
        }:
            old_reminders = self._reminder_lead_text(before.get("provider_reminders"))
            new_reminders = self._reminder_lead_text(preview.get("reminder_plan"))
            if old_reminders != new_reminders:
                add_delta("Reminders", old_reminders, new_reminders)
        return "\n\n".join(changes) or None

    @staticmethod
    def _calendar_state_title(
        action_type: str,
        action: Action | None,
        approval: Approval | None,
        operation: Operation | None,
    ) -> str:
        labels = {
            "calendar_create_meeting": (
                "new course meeting",
                "Course meeting creation",
                "Course meeting created",
            ),
            "calendar_update_meeting": (
                "course meeting update",
                "Course meeting update",
                "Course meeting updated",
            ),
            "calendar_create_event": ("new event", "Event creation", "Event created"),
            "calendar_update_event": ("event update", "Event update", "Event updated"),
            "calendar_update_reminders": (
                "reminder change",
                "Reminder change",
                "Reminders updated",
            ),
            "calendar_cancel_event": (
                "event cancellation",
                "Event cancellation",
                "Event cancelled",
            ),
            "calendar_apply_term_schedule": (
                "term schedule",
                "Term schedule",
                "Term schedule applied",
            ),
        }
        review_subject, outcome_subject, completed = labels.get(
            action_type,
            ("Calendar change", "Calendar change", "Calendar change completed"),
        )
        if approval is not None and approval.status == ApprovalStatus.PENDING.value:
            return f"Review {review_subject}"
        if action is not None and action.status == "rejected":
            return f"{outcome_subject} rejected"
        if action is not None and action.status == "expired":
            return f"{outcome_subject} expired"
        if action is not None and action.status == "superseded":
            return f"{outcome_subject} superseded"
        if operation is not None and operation.status == "succeeded":
            return completed
        if operation is not None and operation.status in {
            "failed",
            "partial_failed",
            "reconciliation_required",
        }:
            return f"{outcome_subject} needs attention"
        if operation is not None and operation.status in {"pending", "running"}:
            return f"{outcome_subject} in progress"
        return DiscordProjectionRunner._action_label(action_type)

    @staticmethod
    def _reminder_selected(plan: object) -> str:
        if not isinstance(plan, dict):
            return "custom"
        leads = plan.get("lead_seconds")
        presets = {
            (): "none",
            (300,): "5m",
            (600,): "10m",
            (900,): "15m",
            (1800,): "30m",
            (3600,): "1h",
        }
        if not isinstance(leads, list):
            return "custom"
        return presets.get(tuple(int(value) for value in leads), "custom")

    @staticmethod
    def _proposal_selects(
        revision: ActionRevision,
        approval: Approval,
        projection_id: uuid.UUID,
        signing_key: bytes,
    ) -> list[dict[str, Any]]:
        if revision.action_type == "calendar_apply_term_schedule":
            return []
        if revision.action_type not in {
            "calendar_create_event",
            "calendar_update_event",
            "calendar_update_reminders",
        }:
            return []
        controls: list[dict[str, Any]] = []
        classification = revision.preview.get("classification", {})
        if revision.action_type in {
            "calendar_create_event",
            "calendar_update_event",
        }:
            selected_priority = (
                str(classification.get("priority", "normal"))
                if isinstance(classification, dict)
                else "normal"
            )
            token = issue_projection_proposal_control_token(
                revision.id,
                projection_id,
                "priority",
                approval.expires_at,
                signing_key,
            )
            controls.append(
                {
                    "kind": "string_select",
                    "field": "priority",
                    "label": "Priority",
                    "placeholder": "Priority",
                    "row": 1,
                    "min_values": 1,
                    "max_values": 1,
                    "token": token,
                    "options": [
                        {
                            "label": label,
                            "value": value,
                            "description": f"Set Docket priority to {label.lower()}",
                            "default": value == selected_priority,
                        }
                        for label, value in (
                            ("Low", "low"),
                            ("Normal", "normal"),
                            ("High", "high"),
                            ("Urgent", "urgent"),
                        )
                    ],
                }
            )
        selected_reminder = DiscordProjectionRunner._reminder_selected(
            revision.preview.get("reminder_plan")
        )
        token = issue_projection_proposal_control_token(
            revision.id,
            projection_id,
            "reminder_preset",
            approval.expires_at,
            signing_key,
        )
        controls.append(
            {
                "kind": "string_select",
                "field": "reminder_preset",
                "label": "Reminder",
                "placeholder": "Reminder",
                "row": 2,
                "min_values": 1,
                "max_values": 1,
                "token": token,
                "options": [
                    {
                        "label": label,
                        "value": value,
                        "description": description,
                        "default": value == selected_reminder,
                    }
                    for label, value, description in (
                        ("None", "none", "Disable both reminder projections"),
                        ("5 minutes", "5m", "Google popup and Docket thread"),
                        ("10 minutes", "10m", "Google popup and Docket thread"),
                        ("15 minutes", "15m", "Google popup and Docket thread"),
                        ("30 minutes", "30m", "Google popup and Docket thread"),
                        ("1 hour", "1h", "Google popup and Docket thread"),
                        ("Custom…", "custom", "Open the bounded reminder editor"),
                    )
                ],
            }
        )
        return controls

    @staticmethod
    def _schedule_page_count(revision: ActionRevision) -> int:
        raw_count = revision.preview.get("item_count")
        if not isinstance(raw_count, int) or not 1 <= raw_count <= 50:
            raise DiscordProjectionError(
                "invalid_schedule_preview",
                "Schedule preview item count is outside its bound",
            )
        items = revision.preview.get("items")
        if not isinstance(items, list) or len(items) != raw_count:
            raise DiscordProjectionError(
                "invalid_schedule_preview",
                "Schedule preview items do not match the immutable item count",
            )
        return (raw_count + 9) // 10

    def _schedule_item_field(self, item: dict[str, Any], index: int) -> dict[str, Any]:
        event = item.get("event")
        event = event if isinstance(event, dict) else {}
        classification = item.get("classification")
        classification = classification if isinstance(classification, dict) else {}
        conflicts = item.get("conflicts")
        conflict_count = len(conflicts) if isinstance(conflicts, list) else 0
        identity = " · ".join(
            str(value)
            for value in (
                item.get("course_code"),
                item.get("section"),
                item.get("meeting_id"),
            )
            if value
        )
        return {
            "name": f"{index}. {identity or item.get('item_key', 'Schedule item')}",
            "value": (
                f"{item.get('effect', 'unknown')} · "
                f"{classification.get('recurrence', 'timed')}\n"
                f"{event.get('title', 'Untitled')}\n"
                f"{self._standalone_timing_manual(event)}\n"
                f"{event.get('location') or 'No location'} · "
                f"{conflict_count} conflict{'s' if conflict_count != 1 else ''}"
            ),
            "inline": False,
        }

    def _review_navigation_control(
        self,
        *,
        revision: ActionRevision,
        projection: DiscordProjection,
        projection_version: int,
        expires_at: datetime,
        signing_key: bytes,
        source_view: str,
        source_page: int | None,
        target_view: str,
        target_page: int | None,
        label: str,
        row: int = 1,
    ) -> dict[str, Any]:
        return {
            "kind": "review_navigation",
            "transition": "proposal_review_navigate",
            "label": label,
            "row": row,
            "action_revision_id": str(revision.id),
            "source_view": source_view,
            "source_page": source_page,
            "target_view": target_view,
            "target_page": target_page,
            "token": issue_projection_review_navigation_token(
                action_revision_id=revision.id,
                projection_id=projection.id,
                projection_version=projection_version,
                source_view=source_view,
                source_page=source_page,
                target_view=target_view,
                target_page=target_page,
                actor_id=self.settings.operator_discord_user_id,
                expires_at=expires_at,
                signing_key=signing_key,
            ),
        }

    def _render(
        self,
        queue_item: QueueItem,
        action: Action | None,
        revision: ActionRevision | None,
        approval: Approval | None,
        operation: Operation | None,
        local_revisions: list[tuple[Action, ActionRevision]],
        account_label: str | None,
        projection: DiscordProjection,
        projection_version: int,
        projection_date: date,
        latest_date: date,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        calendar_action = revision is not None and revision.action_type.startswith("calendar_")
        fields: list[dict[str, Any]] = []
        if not calendar_action:
            fields.extend(
                [
                    {
                        "name": "Status",
                        "value": self._status_label(queue_item.status),
                        "inline": True,
                    },
                    {
                        "name": "Priority",
                        "value": queue_item.priority.title(),
                        "inline": True,
                    },
                ]
            )
            fields.extend(
                [
                    {
                        "name": "Queue date",
                        "value": projection_date.isoformat(),
                        "inline": True,
                    },
                    {"name": "Category", "value": queue_item.category, "inline": True},
                    {
                        "name": "Source",
                        "value": (
                            "Ingested source"
                            if queue_item.primary_source_item_id is not None
                            else (
                                "Manual Discord request"
                                if queue_item.deduplication_key.startswith("manual_action:")
                                else "Docket system"
                            )
                        ),
                        "inline": True,
                    },
                ]
            )
        if not calendar_action and queue_item.received_at is not None:
            fields.append(
                {
                    "name": "Received",
                    "value": self._timestamp_label(queue_item.received_at),
                    "inline": False,
                }
            )
        if projection_date < latest_date:
            fields.append(
                {
                    "name": "Carried forward",
                    "value": latest_date.isoformat(),
                    "inline": True,
                }
            )
        if not calendar_action and account_label is not None:
            fields.append({"name": "Account", "value": account_label, "inline": False})
        base_fields = list(fields)
        if revision is not None:
            preview = revision.preview
            standalone = preview.get("event")
            if isinstance(standalone, dict):
                fields.append(
                    {
                        "name": "When",
                        "value": self._standalone_timing(standalone),
                        "inline": False,
                    }
                )
                if standalone.get("location"):
                    fields.append(
                        {
                            "name": "Where",
                            "value": str(standalone["location"]),
                            "inline": False,
                        }
                    )
            changes = self._calendar_change_text(preview, revision.action_type)
            if changes is not None:
                fields.append(
                    {
                        "name": "Delta",
                        "value": changes,
                        "inline": False,
                    }
                )
            classification = preview.get("classification")
            if isinstance(classification, dict):
                recurrence = str(
                    classification.get("recurrence_kind")
                    or classification.get("recurrence")
                    or "one_time"
                )
                tags = [str(value) for value in classification.get("operator_tags", [])]
                priority = str(classification.get("priority") or queue_item.priority).title()
                type_text = (
                    f"{'Recurring' if recurrence == 'recurring' else 'One-time'}"
                    f" · {priority} priority"
                )
                if tags:
                    type_text += f"\nTags: {', '.join(tags)}"
                fields.append(
                    {
                        "name": "Details",
                        "value": type_text,
                        "inline": False,
                    }
                )
            if "reminder_plan" in preview:
                fields.append(
                    {
                        "name": "Notifications",
                        "value": self._reminder_text(preview.get("reminder_plan")),
                        "inline": False,
                    }
                )
            if revision.action_type == "calendar_apply_term_schedule":
                term = preview.get("term")
                if isinstance(term, dict):
                    fields.append(
                        {
                            "name": "Term",
                            "value": (
                                f"{term.get('term_name', 'Unnamed term')}\n"
                                f"{term.get('institution', 'Unknown institution')}\n"
                                f"{term.get('start_date', '?')} through "
                                f"{term.get('end_date', '?')} · "
                                f"{term.get('timezone', '?')}"
                            ),
                            "inline": False,
                        }
                    )
                counts = preview.get("counts")
                if isinstance(counts, dict):
                    fields.append(
                        {
                            "name": "Batch",
                            "value": (
                                f"{preview.get('item_count', '?')} calendar changes · "
                                f"{counts.get('create', 0)} create · "
                                f"{counts.get('update', 0)} update · "
                                f"{counts.get('no_op', 0)} already synchronized"
                            ),
                            "inline": False,
                        }
                    )
            if operation is not None:
                result = operation.result
                result_counts = result.get("counts") if isinstance(result, dict) else None
                if isinstance(result_counts, dict) or operation.status != "succeeded":
                    value = self._status_label(operation.status)
                    if isinstance(result_counts, dict):
                        value += (
                            f"\n{result_counts.get('succeeded', 0)} succeeded · "
                            f"{result_counts.get('failed', 0)} failed · "
                            f"{result_counts.get('reconciliation_required', 0)} uncertain · "
                            f"{result_counts.get('pending', 0)} pending"
                        )
                    fields.append(
                        {
                            "name": "Result",
                            "value": value,
                            "inline": False,
                        }
                    )
            conflicts = preview.get("conflicts")
            if isinstance(conflicts, list) and (
                bool(conflicts)
                or (approval is not None and approval.status == ApprovalStatus.PENDING.value)
            ):
                conflict_text = (
                    "None found"
                    if not conflicts
                    else "\n".join(
                        f"{item.get('summary') or 'Untitled'} · "
                        f"{self._discord_timestamp_value(item.get('start_at'), 'F')}"
                        for item in conflicts[:5]
                        if isinstance(item, dict)
                    )
                )
                fields.append(
                    {
                        "name": "Conflicts",
                        "value": conflict_text or "Advisory overlap detected",
                        "inline": False,
                    }
                )
            course = preview.get("course", {})
            course_label = " · ".join(
                str(value)
                for value in (
                    course.get("course_code"),
                    course.get("section"),
                    course.get("course_title"),
                )
                if value
            )
            if course_label:
                fields.append({"name": "Course", "value": course_label, "inline": False})
            schedule = preview.get("schedule")
            if isinstance(schedule, dict):
                fields.append(
                    {
                        "name": "Proposed schedule",
                        "value": self._schedule_text(schedule),
                        "inline": False,
                    }
                )
            if not calendar_action:
                fields.append(
                    {
                        "name": "Effect",
                        "value": self._action_label(revision.action_type),
                        "inline": True,
                    }
                )
            if (
                revision.action_type == "calendar_apply_term_schedule"
                and projection.view_action_revision_id == revision.id
            ):
                page_count = self._schedule_page_count(revision)
                if projection.view_mode == "schedule_review":
                    page = projection.view_page
                    if page is None or page > page_count:
                        raise DiscordProjectionError(
                            "invalid_schedule_review_state",
                            "Stored schedule review page is outside its immutable preview",
                        )
                    raw_items = revision.preview.get("items")
                    assert isinstance(raw_items, list)
                    start = (page - 1) * 10
                    page_items = raw_items[start : start + 10]
                    if any(not isinstance(item, dict) for item in page_items):
                        raise DiscordProjectionError(
                            "invalid_schedule_preview",
                            "Schedule preview contains an invalid review item",
                        )
                    fields = [
                        *base_fields,
                        {
                            "name": "Schedule review",
                            "value": (
                                f"Page {page} of {page_count} · changes "
                                f"{start + 1}-{start + len(page_items)} of "
                                f"{revision.preview.get('item_count')}"
                            ),
                            "inline": False,
                        },
                        *[
                            self._schedule_item_field(item, start + index)
                            for index, item in enumerate(page_items, start=1)
                        ],
                    ]
                elif projection.view_mode == "decision":
                    fields.append(
                        {
                            "name": "Review complete",
                            "value": (
                                f"All {revision.preview.get('item_count')} calendar changes "
                                f"reviewed across {page_count} page"
                                f"{'s' if page_count != 1 else ''}. "
                                "Approve or reject the bound schedule below."
                            ),
                            "inline": False,
                        }
                    )
                elif projection.view_mode == "schedule_failures":
                    failures = (
                        operation.result.get("failures")
                        if operation is not None and isinstance(operation.result, dict)
                        else None
                    )
                    if not isinstance(failures, list) or not failures:
                        raise DiscordProjectionError(
                            "schedule_failures_unavailable",
                            "The stored schedule result has no reviewable failures",
                        )
                    page = projection.view_page
                    failure_page_count = (min(len(failures), 50) + 9) // 10
                    if page is None or page > failure_page_count:
                        raise DiscordProjectionError(
                            "invalid_schedule_review_state",
                            "Stored schedule failure page is outside its result",
                        )
                    preview_items = revision.preview.get("items")
                    preview_by_key = (
                        {
                            str(item.get("item_key")): item
                            for item in preview_items
                            if isinstance(item, dict)
                        }
                        if isinstance(preview_items, list)
                        else {}
                    )
                    start = (page - 1) * 10
                    page_failures = failures[start : start + 10]
                    fields = [
                        *base_fields,
                        {
                            "name": "Schedule failures",
                            "value": (
                                f"Page {page} of {failure_page_count} · items "
                                f"{start + 1}-{start + len(page_failures)} of "
                                f"{min(len(failures), 50)}"
                            ),
                            "inline": False,
                        },
                    ]
                    for index, failure in enumerate(page_failures, start=start + 1):
                        failure = failure if isinstance(failure, dict) else {}
                        item_key = str(failure.get("item_key", "unknown"))
                        preview_item = preview_by_key.get(item_key, {})
                        label = " · ".join(
                            str(value)
                            for value in (
                                preview_item.get("course_code"),
                                preview_item.get("section"),
                                preview_item.get("meeting_id"),
                            )
                            if value
                        )
                        fields.append(
                            {
                                "name": f"{index}. {label or item_key}",
                                "value": (
                                    f"{failure.get('status', 'failed')} · "
                                    f"{failure.get('error_code') or 'unknown_error'}"
                                ),
                                "inline": False,
                            }
                        )
        controls: list[dict[str, Any]] = []
        if (
            approval is not None
            and action is not None
            and approval.status == ApprovalStatus.PENDING.value
            and action.status == "approval_pending"
            and queue_item.status == "awaiting_approval"
            and projection_date == latest_date
        ):
            signing_key = self.settings.read_secret(
                self.settings.interaction_signing_key_file
            ).encode()
            if revision is not None and revision.action_type == "calendar_apply_term_schedule":
                page_count = self._schedule_page_count(revision)
                common: _NavigationCommon = {
                    "revision": revision,
                    "projection": projection,
                    "projection_version": projection_version,
                    "expires_at": approval.expires_at,
                    "signing_key": signing_key,
                }
                if projection.view_mode == "summary":
                    controls = [
                        self._review_navigation_control(
                            **common,
                            source_view="summary",
                            source_page=None,
                            target_view="schedule_review",
                            target_page=1,
                            label="Begin review",
                        ),
                        {
                            "kind": "proposal_action",
                            "transition": "proposal_refresh",
                            "label": "Refresh",
                            "row": 3,
                            "action_revision_id": str(revision.id),
                            "token": issue_projection_proposal_control_token(
                                revision.id,
                                projection.id,
                                "refresh",
                                approval.expires_at,
                                signing_key,
                            ),
                        },
                    ]
                elif projection.view_mode == "schedule_review":
                    assert projection.view_page is not None
                    page = projection.view_page
                    controls = [
                        self._review_navigation_control(
                            **common,
                            source_view="schedule_review",
                            source_page=page,
                            target_view=("summary" if page == 1 else "schedule_review"),
                            target_page=None if page == 1 else page - 1,
                            label="Back to summary" if page == 1 else "Previous",
                        ),
                        self._review_navigation_control(
                            **common,
                            source_view="schedule_review",
                            source_page=page,
                            target_view=("decision" if page == page_count else "schedule_review"),
                            target_page=None if page == page_count else page + 1,
                            label=("Continue to decision" if page == page_count else "Next"),
                        ),
                    ]
                elif projection.view_mode == "decision":
                    token = issue_projection_decision_approval_token(
                        approval.id,
                        projection.id,
                        projection_version,
                        approval.expires_at,
                        signing_key,
                    )
                    controls = [
                        {
                            "kind": "approval",
                            "decision": "approve",
                            "label": "Approve",
                            "approval_id": str(approval.id),
                            "token": token,
                        },
                        {
                            "kind": "approval",
                            "decision": "reject",
                            "label": "Reject",
                            "approval_id": str(approval.id),
                            "token": token,
                        },
                        self._review_navigation_control(
                            **common,
                            source_view="decision",
                            source_page=None,
                            target_view="schedule_review",
                            target_page=page_count,
                            label="Back to review",
                        ),
                        {
                            "kind": "proposal_action",
                            "transition": "proposal_refresh",
                            "label": "Refresh",
                            "row": 3,
                            "action_revision_id": str(revision.id),
                            "token": issue_projection_proposal_control_token(
                                revision.id,
                                projection.id,
                                "refresh",
                                approval.expires_at,
                                signing_key,
                            ),
                        },
                        {
                            "kind": "proposal_action",
                            "transition": "proposal_snooze",
                            "label": "Snooze until tomorrow",
                            "row": 4,
                            "action_revision_id": str(revision.id),
                            "token": issue_projection_proposal_control_token(
                                revision.id,
                                projection.id,
                                "snooze",
                                approval.expires_at,
                                signing_key,
                            ),
                        },
                    ]
                else:
                    raise DiscordProjectionError(
                        "invalid_schedule_review_state",
                        "Pending schedule card is not in an approvable review state",
                    )
            else:
                token = issue_projection_approval_token(
                    approval.id, projection.id, approval.expires_at, signing_key
                )
                controls = [
                    {
                        "kind": "approval",
                        "decision": "approve",
                        "label": "Approve",
                        "approval_id": str(approval.id),
                        "token": token,
                    },
                    {
                        "kind": "approval",
                        "decision": "reject",
                        "label": "Reject",
                        "approval_id": str(approval.id),
                        "token": token,
                    },
                ]
            if revision is not None and revision.action_type != "calendar_apply_term_schedule":
                controls.extend(
                    self._proposal_selects(
                        revision,
                        approval,
                        projection.id,
                        signing_key,
                    )
                )
                if revision.action_type in {
                    "calendar_create_event",
                    "calendar_update_event",
                }:
                    controls.append(
                        {
                            "kind": "proposal_action",
                            "transition": "proposal_edit",
                            "label": "Edit",
                            "row": 3,
                            "action_revision_id": str(revision.id),
                            "token": issue_projection_proposal_control_token(
                                revision.id,
                                projection.id,
                                "edit",
                                approval.expires_at,
                                signing_key,
                            ),
                        }
                    )
                controls.append(
                    {
                        "kind": "proposal_action",
                        "transition": "proposal_snooze",
                        "label": "Snooze until tomorrow",
                        "row": 4,
                        "action_revision_id": str(revision.id),
                        "token": issue_projection_proposal_control_token(
                            revision.id,
                            projection.id,
                            "snooze",
                            approval.expires_at,
                            signing_key,
                        ),
                    }
                )
                if revision.action_type in {
                    "calendar_create_event",
                    "calendar_update_event",
                    "calendar_update_reminders",
                    "calendar_cancel_event",
                }:
                    controls.append(
                        {
                            "kind": "proposal_action",
                            "transition": "proposal_refresh",
                            "label": "Refresh",
                            "row": 3,
                            "action_revision_id": str(revision.id),
                            "token": issue_projection_proposal_control_token(
                                revision.id,
                                projection.id,
                                "refresh",
                                approval.expires_at,
                                signing_key,
                            ),
                        }
                    )
            fields.append(
                {
                    "name": "Approval expires",
                    "value": self._timestamp_label(approval.expires_at),
                    "inline": False,
                }
            )
        elif (
            projection_date == latest_date
            and revision is not None
            and revision.action_type == "calendar_apply_term_schedule"
            and operation is not None
        ):
            signing_key = self.settings.read_secret(
                self.settings.interaction_signing_key_file
            ).encode()
            local_midnight = datetime.combine(
                projection_date,
                datetime.min.time(),
                tzinfo=ZoneInfo(self.settings.timezone),
            )
            expires_at = local_midnight + timedelta(
                hours=self.settings.daily_rollover_hour,
                seconds=self.settings.local_action_ttl_seconds,
            )
            if operation.status in {
                "partial_failed",
                "reconciliation_required",
                "failed",
            }:
                failures = (
                    operation.result.get("failures") if isinstance(operation.result, dict) else None
                )
                if isinstance(failures, list) and failures:
                    failure_page_count = (min(len(failures), 50) + 9) // 10
                    common = {
                        "revision": revision,
                        "projection": projection,
                        "projection_version": projection_version,
                        "expires_at": expires_at,
                        "signing_key": signing_key,
                    }
                    if projection.view_mode == "summary":
                        controls = [
                            self._review_navigation_control(
                                **common,
                                source_view="summary",
                                source_page=None,
                                target_view="schedule_failures",
                                target_page=1,
                                label="View failures",
                            )
                        ]
                    elif projection.view_mode == "schedule_failures":
                        assert projection.view_page is not None
                        page = projection.view_page
                        controls = [
                            self._review_navigation_control(
                                **common,
                                source_view="schedule_failures",
                                source_page=page,
                                target_view=("summary" if page == 1 else "schedule_failures"),
                                target_page=None if page == 1 else page - 1,
                                label="Back to results" if page == 1 else "Previous",
                            )
                        ]
                        if page < failure_page_count:
                            controls.append(
                                self._review_navigation_control(
                                    **common,
                                    source_view="schedule_failures",
                                    source_page=page,
                                    target_view="schedule_failures",
                                    target_page=page + 1,
                                    label="Next",
                                )
                            )
        elif projection_date == latest_date and local_revisions:
            signing_key = self.settings.read_secret(
                self.settings.interaction_signing_key_file
            ).encode()
            local_midnight = datetime.combine(
                projection_date,
                datetime.min.time(),
                tzinfo=ZoneInfo(self.settings.timezone),
            )
            expires_at = local_midnight + timedelta(
                hours=self.settings.daily_rollover_hour,
                seconds=self.settings.local_action_ttl_seconds,
            )
            for local_action, local_revision in local_revisions:
                token = issue_projection_local_action_token(
                    local_revision.id,
                    projection.id,
                    queue_item.version,
                    expires_at,
                    signing_key,
                )
                controls.append(
                    {
                        "kind": "local_action",
                        "action_type": local_revision.action_type,
                        "label": (
                            "Snooze until tomorrow"
                            if local_revision.action_type == "snooze_queue_item"
                            else "Ignore"
                        ),
                        "action_id": str(local_action.id),
                        "action_revision_id": str(local_revision.id),
                        "token": token,
                    }
                )
        if queue_item.status == "snoozed" and queue_item.snoozed_until is not None:
            fields.append(
                {
                    "name": "Snoozed until",
                    "value": self._timestamp_label(queue_item.snoozed_until),
                    "inline": False,
                }
            )
        fields = [
            {
                "name": self._bounded(str(field["name"]), 256),
                "value": self._bounded(str(field["value"]), 1024),
                "inline": bool(field.get("inline", False)),
            }
            for field in fields[:25]
        ]
        display_title = queue_item.title
        subject: str | None = None
        if revision is not None and calendar_action:
            event = revision.preview.get("event")
            if isinstance(event, dict) and event.get("title"):
                subject = str(event["title"])
            before = revision.preview.get("before")
            if subject is None and isinstance(before, dict) and before.get("summary"):
                subject = str(before["summary"])
            course = revision.preview.get("course")
            if subject is None and isinstance(course, dict):
                subject = " · ".join(
                    str(value)
                    for value in (course.get("course_code"), course.get("section"))
                    if value
                )
            term = revision.preview.get("term")
            if subject is None and isinstance(term, dict) and term.get("term_name"):
                subject = str(term["term_name"])
            display_title = self._calendar_state_title(
                revision.action_type,
                action,
                approval,
                operation,
            )
        description = queue_item.summary
        if calendar_action:
            state_description = queue_item.summary
            if approval is not None and approval.status == ApprovalStatus.PENDING.value:
                state_description = "Review the details below. Nothing changes until you approve."
            elif action is not None and action.status == "rejected":
                state_description = "Rejected. No Calendar change was made."
            elif operation is not None and operation.status == "succeeded":
                state_description = "Completed on your configured Docket calendar."
            elif operation is not None and operation.status in {
                "failed",
                "partial_failed",
                "reconciliation_required",
            }:
                state_description = "This Calendar operation needs attention."
            elif operation is not None and operation.status in {"pending", "running"}:
                state_description = "Approved and waiting for Calendar execution."
            description = f"{subject}\n{state_description}" if subject else state_description
        color = 0xD6A756
        if revision is not None and revision.action_type == "calendar_cancel_event":
            color = 0xC0392B
        elif calendar_action and operation is not None and operation.status == "succeeded":
            color = 0x3BA55D
        elif (
            calendar_action
            and operation is not None
            and operation.status
            in {
                "failed",
                "partial_failed",
                "reconciliation_required",
            }
        ):
            color = 0xC0392B
        elif (
            calendar_action
            and operation is not None
            and operation.status
            in {
                "pending",
                "running",
            }
        ):
            color = 0x5B8DEF
        elif (
            calendar_action
            and action is not None
            and action.status
            in {
                "rejected",
                "expired",
                "superseded",
            }
        ):
            color = 0x747F8D
        embed = {
            "title": self._bounded(display_title, 256),
            "description": self._bounded(description, 4096),
            "fields": fields,
            "color": color,
            "timestamp": (
                _aware(revision.created_at).astimezone(UTC).isoformat()
                if revision is not None
                else _aware(queue_item.created_at).astimezone(UTC).isoformat()
            ),
            "footer": self._bounded(
                (
                    f"Docket · Revision {revision.revision}"
                    if revision is not None
                    and approval is not None
                    and approval.status == ApprovalStatus.PENDING.value
                    else "Docket"
                ),
                512,
            ),
        }
        return embed, controls, sha256_json(embed), sha256_json(controls)

    def _projection_request(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        daily_thread_id: uuid.UUID,
        projection_id: uuid.UUID,
    ) -> dict[str, Any]:
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            daily_thread = session.get(DiscordDailyThread, daily_thread_id)
            projection = session.get(DiscordProjection, projection_id)
            if (
                event is None
                or event.lease_token != lease_token
                or daily_thread is None
                or daily_thread.thread_id is None
                or projection is None
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Projection state is missing")
            queue_item = session.get(QueueItem, projection.queue_item_id)
            if queue_item is None:
                raise DiscordProjectionError("queue_item_missing", "Queue item is missing")
            latest_date = session.scalar(
                select(DiscordDailyThread.local_date)
                .join(
                    DiscordProjection,
                    DiscordProjection.daily_thread_id == DiscordDailyThread.id,
                )
                .where(DiscordProjection.queue_item_id == queue_item.id)
                .order_by(DiscordDailyThread.local_date.desc())
                .limit(1)
            )
            if latest_date is None:
                latest_date = daily_thread.local_date
            ensure_local_actions(
                session,
                queue_item,
                projection_date=latest_date,
            )
            actions = session.scalars(
                select(Action)
                .where(Action.queue_item_id == queue_item.id)
                .order_by(Action.display_order, Action.id)
            ).all()
            action: Action | None = None
            revision: ActionRevision | None = None
            approval: Approval | None = None
            operation: Operation | None = None
            local_revisions: list[tuple[Action, ActionRevision]] = []
            account_label: str | None = None
            for candidate in actions:
                candidate_revision = session.scalar(
                    select(ActionRevision).where(
                        ActionRevision.action_id == candidate.id,
                        ActionRevision.revision == candidate.current_revision,
                    )
                )
                if candidate_revision is None:
                    continue
                if candidate_revision.risk_class == RiskClass.LOCAL_WRITE.value:
                    if candidate.status == "available":
                        local_revisions.append((candidate, candidate_revision))
                    continue
                if action is None:
                    action = candidate
                    revision = candidate_revision
                    approval = session.scalar(
                        select(Approval).where(Approval.action_revision_id == candidate_revision.id)
                    )
                    operation = session.scalar(
                        select(Operation)
                        .where(Operation.action_revision_id == candidate_revision.id)
                        .order_by(Operation.created_at.desc())
                        .limit(1)
                    )
                    account = session.get(Account, candidate_revision.account_id)
                    if account is not None:
                        account_label = (
                            account.display_name
                            or account.email_address
                            or account.external_account_id
                        )
            revision_changed = revision is None or projection.view_action_revision_id != revision.id
            if revision is None:
                projection.view_action_revision_id = None
                projection.view_mode = "summary"
                projection.view_page = None
                projection.reviewed_through_page = 0
            elif revision_changed:
                projection.view_action_revision_id = revision.id
                projection.view_mode = (
                    "summary"
                    if revision.action_type == "calendar_apply_term_schedule"
                    else (
                        "decision"
                        if action is not None and action.status == "approval_pending"
                        else "summary"
                    )
                )
                projection.view_page = None
                projection.reviewed_through_page = 0
            elif (
                revision.action_type == "calendar_apply_term_schedule"
                and action is not None
                and action.status != "approval_pending"
                and projection.view_mode in {"schedule_review", "decision"}
            ):
                projection.view_mode = "summary"
                projection.view_page = None
                projection.reviewed_through_page = 0

            def render(
                version: int,
            ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
                return self._render(
                    queue_item,
                    action,
                    revision,
                    approval,
                    operation,
                    local_revisions,
                    account_label,
                    projection,
                    version,
                    daily_thread.local_date,
                    latest_date,
                )

            candidate_version = projection.projection_version
            embed, controls, render_sha256, component_sha256 = render(candidate_version)
            changed = (
                projection.render_sha256 != render_sha256
                or projection.component_sha256 != component_sha256
            )
            if changed and projection.render_sha256 != "0" * 64:
                candidate_version += 1
                embed, controls, render_sha256, component_sha256 = render(candidate_version)
            projection.projection_version = candidate_version
            projection.render_sha256 = render_sha256
            projection.component_sha256 = component_sha256
            projection.status = "pending"
            return {
                "request_id": str(event.id),
                "projection_id": str(projection.id),
                "known_message_id": projection.message_id,
                "guild_id": daily_thread.guild_id,
                "parent_channel_id": daily_thread.channel_id,
                "thread_id": daily_thread.thread_id,
                "projection_version": projection.projection_version,
                "render_schema_version": projection.render_schema_version,
                "render_sha256": render_sha256,
                "component_sha256": component_sha256,
                "embed": embed,
                "controls": controls,
            }

    def _accept_projection_ack(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        projection_id: uuid.UUID,
        request: dict[str, Any],
        ack: dict[str, Any],
    ) -> None:
        exact_fields = (
            "request_id",
            "projection_id",
            "guild_id",
            "parent_channel_id",
            "thread_id",
            "render_sha256",
            "component_sha256",
        )
        if (
            any(ack.get(field) != request[field] for field in exact_fields)
            or not str(ack.get("message_id", "")).isdigit()
        ):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Projection acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            projection = session.get(DiscordProjection, projection_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
                or projection is None
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            projection.message_id = str(ack["message_id"])
            projection.status = "delivered"
            projection.last_error_code = None
            approval = session.scalar(
                select(Approval)
                .join(ActionRevision, ActionRevision.id == Approval.action_revision_id)
                .join(Action, Action.id == ActionRevision.action_id)
                .where(
                    Action.queue_item_id == projection.queue_item_id,
                    ActionRevision.revision == Action.current_revision,
                )
                .order_by(Action.display_order, Approval.created_at.desc())
                .limit(1)
            )
            newest_projection = session.scalar(
                select(DiscordProjection)
                .join(
                    DiscordDailyThread,
                    DiscordDailyThread.id == DiscordProjection.daily_thread_id,
                )
                .where(DiscordProjection.queue_item_id == projection.queue_item_id)
                .order_by(DiscordDailyThread.local_date.desc())
                .limit(1)
            )
            if (
                approval is not None
                and approval.status == ApprovalStatus.PENDING.value
                and newest_projection is not None
                and newest_projection.id == projection.id
            ):
                previous_projection_id = approval.control_projection_id
                approval.control_projection_id = projection.id
                if previous_projection_id is not None and previous_projection_id != projection.id:
                    approval.interaction_token_version += 1
                    previous = session.get(DiscordProjection, previous_projection_id)
                    previous_thread = (
                        session.get(DiscordDailyThread, previous.daily_thread_id)
                        if previous is not None
                        else None
                    )
                    if previous is not None and previous_thread is not None:
                        session.add(
                            OutboxEvent(
                                event_type="discord.projection.refresh_requested",
                                aggregate_type="queue_item",
                                aggregate_id=projection.queue_item_id,
                                deduplication_key=(
                                    f"discord_projection:{projection.queue_item_id}:handoff:"
                                    f"{previous.id}:{projection.id}"
                                ),
                                payload={
                                    "queue_item_id": str(projection.queue_item_id),
                                    "projection_id": str(previous.id),
                                    "target_local_date": previous_thread.local_date.isoformat(),
                                    "reason": "control_handoff",
                                },
                                status=OutboxStatus.PENDING.value,
                            )
                        )
            elif approval is not None and approval.status != ApprovalStatus.PENDING.value:
                approval.control_projection_id = None
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.last_error_code = None

    def _lifecycle_request(
        self, event_id: uuid.UUID, lease_token: uuid.UUID
    ) -> tuple[uuid.UUID, dict[str, Any]]:
        with self.session_factory() as session:
            event = session.get(OutboxEvent, event_id)
            if event is None or event.lease_token != lease_token:
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            daily_thread = session.get(DiscordDailyThread, event.aggregate_id)
            desired_state = event.payload.get("desired_state")
            if daily_thread is None or daily_thread.thread_id is None:
                raise DiscordProjectionError("thread_not_found", "Daily thread is unavailable")
            if desired_state not in {"active", "archived"}:
                raise DiscordProjectionError(
                    "invalid_lifecycle_state", "Thread lifecycle target is invalid"
                )
            return daily_thread.id, {
                "request_id": str(event.id),
                "daily_thread_id": str(daily_thread.id),
                "guild_id": daily_thread.guild_id,
                "parent_channel_id": daily_thread.channel_id,
                "thread_id": daily_thread.thread_id,
                "desired_state": desired_state,
            }

    def _accept_lifecycle_ack(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        daily_thread_id: uuid.UUID,
        request: dict[str, Any],
        ack: dict[str, Any],
    ) -> None:
        desired_archived = request["desired_state"] == "archived"
        if (
            ack.get("request_id") != request["request_id"]
            or ack.get("daily_thread_id") != request["daily_thread_id"]
            or ack.get("thread_id") != request["thread_id"]
            or ack.get("archived") is not desired_archived
        ):
            raise DiscordProjectionError(
                "invalid_discord_ack", "Lifecycle acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            daily_thread = session.get(DiscordDailyThread, daily_thread_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
                or daily_thread is None
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            daily_thread.status = "archived" if desired_archived else "active"
            daily_thread.archived_at = utc_now() if desired_archived else None
            daily_thread.last_verified_at = _verified_at(ack["verified_at"])
            daily_thread.last_error_code = None
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.next_attempt_at = None
            event.last_error_code = None

    def _system_alert_request(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> dict[str, Any]:
        with self.session_factory() as session:
            event = session.get(OutboxEvent, event_id)
            if event is None or event.lease_token != lease_token:
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            title = self._bounded(str(event.payload.get("title", "Docket system alert")), 256)
            summary = self._bounded(str(event.payload.get("summary", "Docket work failed")), 2000)
            error_code = self._bounded(str(event.payload.get("error_code", "unknown")), 128)
            occurred_at = event.payload.get("occurred_at", event.created_at.isoformat())
            render = {
                "title": title,
                "summary": summary,
                "error_code": error_code,
                "occurred_at": self._bounded(
                    self._discord_timestamp_pair(occurred_at),
                    64,
                ),
            }
            return {
                "request_id": str(event.id),
                "alert_id": str(event.aggregate_id),
                "guild_id": self.settings.discord_guild_id,
                "channel_id": self.settings.system_channel_id,
                "render_sha256": sha256_json(render),
                **render,
            }

    def _accept_system_alert_ack(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        request: dict[str, Any],
        ack: dict[str, Any],
    ) -> None:
        exact = all(
            ack.get(field) == request[field]
            for field in (
                "request_id",
                "alert_id",
                "guild_id",
                "channel_id",
                "render_sha256",
            )
        )
        if not exact or not str(ack.get("message_id", "")).isdigit():
            raise DiscordProjectionError(
                "invalid_discord_ack", "System alert acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            event.payload = {**event.payload, "discord_message_id": str(ack["message_id"])}
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.next_attempt_at = None
            event.last_error_code = None

    def _system_log_request(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> dict[str, Any]:
        with self.session_factory() as session:
            event = session.get(OutboxEvent, event_id)
            if event is None or event.lease_token != lease_token:
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            severity = str(event.payload.get("severity", "info"))
            if severity not in {"info", "notice", "success", "warning", "error"}:
                raise DiscordProjectionError("invalid_system_log", "System log severity is invalid")
            status = self._bounded(str(event.payload.get("status", "unknown")), 64)
            subsystem = self._bounded(str(event.payload.get("subsystem", "Docket")), 64)
            occurred_at = event.payload.get("occurred_at", event.created_at.isoformat())
            render = {
                "title": self._bounded(str(event.payload.get("title", "Docket update")), 256),
                "summary": self._bounded(str(event.payload.get("summary", "")), 2000),
                "status": status,
                "severity": severity,
                "subsystem": subsystem,
                "occurred_at": self._bounded(
                    self._discord_timestamp_pair(occurred_at),
                    64,
                ),
            }
            return {
                "request_id": str(event.id),
                "log_id": str(event.aggregate_id),
                "guild_id": self.settings.discord_guild_id,
                "channel_id": self.settings.system_channel_id,
                "render": render,
                "render_sha256": sha256_json(render),
            }

    def _accept_system_log_ack(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        request: dict[str, Any],
        ack: dict[str, Any],
    ) -> None:
        exact = all(
            ack.get(field) == request[field]
            for field in (
                "request_id",
                "log_id",
                "guild_id",
                "channel_id",
                "render_sha256",
            )
        )
        if not exact or not str(ack.get("message_id", "")).isdigit():
            raise DiscordProjectionError(
                "invalid_discord_ack", "System log acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            if (
                event is None
                or event.status != OutboxStatus.DELIVERING.value
                or event.lease_token != lease_token
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            event.payload = {**event.payload, "discord_message_id": str(ack["message_id"])}
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.next_attempt_at = None
            event.last_error_code = None

    def _notification_request(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        daily_thread_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        with self.session_factory() as session:
            outbox = session.get(OutboxEvent, event_id)
            if outbox is None or outbox.lease_token != lease_token:
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            notification = session.get(ScheduledNotification, outbox.aggregate_id)
            if notification is None or notification.status != "delivering":
                return None
            rule = session.get(ReminderRule, notification.reminder_rule_id)
            event = (
                session.get(CalendarEventCache, notification.calendar_event_id)
                if notification.calendar_event_id is not None
                else None
            )
            daily_thread = session.get(DiscordDailyThread, daily_thread_id)
            if rule is None or not rule.enabled or event is None or event.status == "cancelled":
                return None
            if (
                daily_thread is None
                or notification.daily_thread_id != daily_thread.id
                or daily_thread.thread_id is None
            ):
                raise DiscordProjectionError(
                    "reminder_thread_missing", "Reminder daily-thread state is missing"
                )
            if rule.queue_channel_id != self.settings.queue_channel_id:
                raise DiscordProjectionError(
                    "reminder_destination_not_allowed",
                    "Reminder queue parent no longer matches configured policy",
                )
            if event.is_all_day:
                start_value = event.start_date.isoformat() if event.start_date else ""
                end_value = event.end_date.isoformat() if event.end_date else ""
            else:
                start_value = (
                    self._discord_timestamp_pair(event.start_at)
                    if event.start_at is not None
                    else ""
                )
                end_value = (
                    self._discord_timestamp(event.end_at, "F")
                    if event.end_at is not None
                    else ""
                )
            late = notification.last_error_code == "late_calendar_refresh"
            render = {
                "summary": self._bounded(event.summary or "Calendar event", 512),
                "location": self._bounded(event.location, 1000) if event.location else None,
                "start": start_value,
                "end": end_value,
                "is_all_day": event.is_all_day,
                "timezone": event.timezone or self.settings.timezone,
                "late": late,
            }
            return {
                "request_id": str(outbox.id),
                "notification_id": str(notification.id),
                "guild_id": self.settings.discord_guild_id,
                "parent_channel_id": daily_thread.channel_id,
                "thread_id": daily_thread.thread_id,
                "render_sha256": sha256_json(render),
                "render": render,
            }

    def _accept_notification_ack(
        self,
        event_id: uuid.UUID,
        lease_token: uuid.UUID,
        request: dict[str, Any],
        ack: dict[str, Any],
    ) -> None:
        exact = all(
            ack.get(field) == request[field]
            for field in (
                "request_id",
                "notification_id",
                "guild_id",
                "parent_channel_id",
                "thread_id",
                "render_sha256",
            )
        )
        if not exact or not str(ack.get("message_id", "")).isdigit():
            raise DiscordProjectionError(
                "invalid_discord_ack", "Reminder acknowledgement did not echo its binding"
            )
        with self.session_factory.begin() as session:
            outbox = session.get(OutboxEvent, event_id)
            notification = session.get(
                ScheduledNotification, uuid.UUID(str(request["notification_id"]))
            )
            if (
                outbox is None
                or outbox.status != OutboxStatus.DELIVERING.value
                or outbox.lease_token != lease_token
                or notification is None
            ):
                raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
            notification.status = "delivered"
            notification.discord_message_id = str(ack["message_id"])
            notification.attempt_count = outbox.attempt_count
            notification.last_error_code = None
            session.add(
                AuditEvent(
                    event_type="calendar_notification.delivered",
                    entity_type="scheduled_notification",
                    entity_id=notification.id,
                    actor_type="docket",
                    actor_id=None,
                    request_id=None,
                    data={
                        "discord_message_id": str(ack["message_id"]),
                        "parent_channel_id": str(ack["parent_channel_id"]),
                        "thread_id": str(ack["thread_id"]),
                        "attempt_count": outbox.attempt_count,
                    },
                )
            )
            outbox.payload = {
                **outbox.payload,
                "parent_channel_id": str(ack["parent_channel_id"]),
                "thread_id": str(ack["thread_id"]),
                "discord_message_id": str(ack["message_id"]),
            }
            outbox.status = OutboxStatus.DELIVERED.value
            outbox.lease_token = None
            outbox.leased_until = None
            outbox.next_attempt_at = None
            outbox.last_error_code = None

    def _cancel_notification_delivery(self, event_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        with self.session_factory.begin() as session:
            outbox = session.get(OutboxEvent, event_id)
            if outbox is None or outbox.lease_token != lease_token:
                return
            notification = session.get(ScheduledNotification, outbox.aggregate_id)
            if notification is not None and notification.status == "delivering":
                notification.status = "cancelled"
                notification.last_error_code = "notification_no_longer_current"
            outbox.status = OutboxStatus.FAILED.value
            outbox.lease_token = None
            outbox.leased_until = None
            outbox.next_attempt_at = None
            outbox.last_error_code = "notification_no_longer_current"

    def _retry(self, event_id: uuid.UUID, lease_token: uuid.UUID, code: str) -> None:
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            if event is None or event.lease_token != lease_token:
                return
            if (
                event.attempt_count >= self.settings.discord_projection_max_attempts
                and event.event_type != "discord.system_alert.requested"
            ):
                event.status = OutboxStatus.FAILED.value
                event.lease_token = None
                event.leased_until = None
                event.next_attempt_at = None
                event.last_error_code = code[:128]
                if event.event_type in _PROJECTION_EVENTS:
                    projection_id = event.payload.get("projection_id")
                    try:
                        projection = (
                            session.get(DiscordProjection, uuid.UUID(str(projection_id)))
                            if projection_id is not None
                            else None
                        )
                    except ValueError:
                        projection = None
                    if projection is not None:
                        projection.status = "failed"
                        projection.last_error_code = code[:128]
                elif event.aggregate_type == "discord_daily_thread":
                    daily_thread = session.get(DiscordDailyThread, event.aggregate_id)
                    if daily_thread is not None:
                        daily_thread.status = "failed"
                        daily_thread.last_error_code = code[:128]
                elif event.event_type == "discord.calendar_reminder.requested":
                    notification = session.get(ScheduledNotification, event.aggregate_id)
                    if notification is not None:
                        notification.status = "failed"
                        notification.attempt_count = event.attempt_count
                        notification.last_error_code = code[:128]
                        session.add(
                            AuditEvent(
                                event_type="calendar_notification.failed",
                                entity_type="scheduled_notification",
                                entity_id=notification.id,
                                actor_type="docket",
                                actor_id=None,
                                request_id=None,
                                data={"error_code": code[:128]},
                            )
                        )
                alert_key = f"discord_system_alert:projection_failure:{event.id}"
                if (
                    session.scalar(
                        select(OutboxEvent).where(OutboxEvent.deduplication_key == alert_key)
                    )
                    is None
                ):
                    reminder_failure = event.event_type == "discord.calendar_reminder.requested"
                    session.add(
                        OutboxEvent(
                            event_type="discord.system_alert.requested",
                            aggregate_type="outbox_event",
                            aggregate_id=event.id,
                            deduplication_key=alert_key,
                            payload={
                                "title": (
                                    "Docket Calendar reminder delivery failure"
                                    if reminder_failure
                                    else "Docket Discord projection failure"
                                ),
                                "summary": (
                                    "A durable Calendar reminder exhausted its Discord retry "
                                    "budget. Its canonical notification state is preserved."
                                    if reminder_failure
                                    else "A durable Discord queue delivery exhausted its retry "
                                    "budget. Canonical Docket state remains intact."
                                ),
                                "error_code": code[:128],
                                "occurred_at": utc_now().isoformat(),
                            },
                            status=OutboxStatus.PENDING.value,
                        )
                    )
                return
            event.status = OutboxStatus.PENDING.value
            event.lease_token = None
            event.leased_until = None
            event.last_error_code = code[:128]
            delay = min(60, 2 ** min(event.attempt_count, 5))
            event.next_attempt_at = utc_now() + timedelta(seconds=delay)

    def run_due_once(self) -> bool:
        leased = self._lease_one()
        if leased is None:
            return False
        event_id, lease_token = leased
        try:
            with self.session_factory() as session:
                event = session.get(OutboxEvent, event_id)
                if event is None or event.lease_token != lease_token:
                    raise DiscordProjectionError("delivery_lease_lost", "Outbox lease was lost")
                event_type = event.event_type
                aggregate_id = event.aggregate_id
            if event_type in _PROJECTION_EVENTS:
                daily_thread_id, projection_id = self._ensure_local_rows(event_id, lease_token)
                thread_request = self._thread_request(event_id, lease_token, daily_thread_id)
                thread_ack = self.adapter.ensure_thread(thread_request)
                self._accept_thread_ack(
                    event_id, lease_token, daily_thread_id, thread_request, thread_ack
                )
                projection_request = self._projection_request(
                    event_id, lease_token, daily_thread_id, projection_id
                )
                projection_ack = self.adapter.put_projection(projection_id, projection_request)
                self._accept_projection_ack(
                    event_id,
                    lease_token,
                    projection_id,
                    projection_request,
                    projection_ack,
                )
            elif event_type == "discord.thread.ensure_requested":
                thread_request = self._thread_request(event_id, lease_token, aggregate_id)
                thread_ack = self.adapter.ensure_thread(thread_request)
                self._accept_thread_ack(
                    event_id, lease_token, aggregate_id, thread_request, thread_ack
                )
                self._complete_event(event_id, lease_token)
            elif event_type == "discord.thread.lifecycle_requested":
                daily_thread_id, lifecycle_request = self._lifecycle_request(event_id, lease_token)
                lifecycle_ack = self.adapter.set_thread_lifecycle(
                    daily_thread_id, lifecycle_request
                )
                self._accept_lifecycle_ack(
                    event_id,
                    lease_token,
                    daily_thread_id,
                    lifecycle_request,
                    lifecycle_ack,
                )
            elif event_type == "discord.system_alert.requested":
                alert_request = self._system_alert_request(event_id, lease_token)
                alert_ack = self.adapter.post_system_alert(alert_request)
                self._accept_system_alert_ack(event_id, lease_token, alert_request, alert_ack)
            elif event_type == "discord.system_log.requested":
                log_request = self._system_log_request(event_id, lease_token)
                log_ack = self.adapter.post_system_log(log_request)
                self._accept_system_log_ack(event_id, lease_token, log_request, log_ack)
            elif event_type == "discord.calendar_reminder.requested":
                daily_thread_id = self._ensure_notification_thread(event_id, lease_token)
                thread_request = self._thread_request(event_id, lease_token, daily_thread_id)
                thread_ack = self.adapter.ensure_thread(thread_request)
                self._accept_thread_ack(
                    event_id, lease_token, daily_thread_id, thread_request, thread_ack
                )
                notification_request = self._notification_request(
                    event_id, lease_token, daily_thread_id
                )
                if notification_request is None:
                    self._cancel_notification_delivery(event_id, lease_token)
                else:
                    notification_ack = self.adapter.post_calendar_reminder(notification_request)
                    self._accept_notification_ack(
                        event_id, lease_token, notification_request, notification_ack
                    )
            else:
                raise DiscordProjectionError(
                    "unsupported_outbox_event", "Discord outbox event is unsupported"
                )
        except DiscordProjectionError as exc:
            logger.warning(
                "discord_outbox_delivery_failed",
                entity_type="outbox_event",
                entity_id=str(event_id),
                error_code=exc.code,
            )
            self._retry(event_id, lease_token, exc.code)
        except Exception:
            logger.exception(
                "discord_outbox_delivery_failed",
                entity_type="outbox_event",
                entity_id=str(event_id),
                error_code="unexpected_projection_error",
            )
            self._retry(event_id, lease_token, "unexpected_projection_error")
        return True

    def recover_expired_leases(self) -> int:
        now = utc_now()
        with self.session_factory.begin() as session:
            events = session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type.in_(_SUPPORTED_EVENTS),
                    OutboxEvent.status == OutboxStatus.DELIVERING.value,
                    OutboxEvent.leased_until < now,
                )
            ).all()
            for event in events:
                event.status = OutboxStatus.PENDING.value
                event.lease_token = None
                event.leased_until = None
                event.next_attempt_at = now
                event.last_error_code = "projection_lease_expired"
            return len(events)
