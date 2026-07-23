from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
    issue_projection_local_action_token,
    issue_projection_proposal_control_token,
)
from docket.services.queue import ensure_local_actions

_SUPPORTED_EVENTS = {
    "discord.projection.requested",
    "discord.projection.refresh_requested",
    "discord.thread.ensure_requested",
    "discord.thread.lifecycle_requested",
    "discord.system_alert.requested",
    "discord.calendar_reminder.requested",
}
_PROJECTION_EVENTS = {
    "discord.projection.requested",
    "discord.projection.refresh_requested",
}
logger = structlog.get_logger(__name__)


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
        days = ", ".join(str(day) for day in schedule.get("days", []))
        times = f"{schedule.get('start_time', '?')}-{schedule.get('end_time', '?')}"
        dates = f"{schedule.get('start_date', '?')} through {schedule.get('end_date', '?')}"
        timezone = str(schedule.get("timezone", ""))
        location = schedule.get("location")
        parts = [f"{days} · {times}", dates, timezone]
        if location:
            parts.append(str(location))
        return "\n".join(parts)

    @staticmethod
    def _bounded(value: str, maximum: int) -> str:
        return value if len(value) <= maximum else value[: maximum - 1] + "…"

    @staticmethod
    def _standalone_timing(event: dict[str, Any]) -> str:
        timing = event.get("timing", {})
        if not isinstance(timing, dict):
            return "Unknown"
        if timing.get("kind") == "all_day":
            return (
                f"All day · {timing.get('start_date', '?')} through "
                f"{timing.get('end_date', '?')} (exclusive)\n"
                f"{timing.get('timezone', '?')}"
            )
        return (
            f"{timing.get('start_local', '?')} through {timing.get('end_local', '?')}\n"
            f"{timing.get('timezone', '?')}"
        )

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
        return f"{lead_text}\nGoogle popup + Docket daily thread"

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
            return [
                DiscordProjectionRunner._schedule_review_select(
                    revision,
                    projection_id,
                    approval.expires_at,
                    signing_key,
                    include_failures=False,
                )
            ]
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
    def _schedule_review_select(
        revision: ActionRevision,
        projection_id: uuid.UUID,
        expires_at: datetime,
        signing_key: bytes,
        *,
        include_failures: bool,
    ) -> dict[str, Any]:
        raw_count = revision.preview.get("item_count")
        if not isinstance(raw_count, int) or not 1 <= raw_count <= 50:
            raise DiscordProjectionError(
                "invalid_schedule_preview",
                "Schedule preview item count is outside its bound",
            )
        page_count = (raw_count + 9) // 10
        options = [
            {
                "label": f"Page {page} of {page_count}",
                "value": str(page),
                "description": (
                    f"Review immutable items {(page - 1) * 10 + 1}-{min(page * 10, raw_count)}"
                ),
                # This select is an action menu, not an editable field. Leaving
                # every option unselected ensures that choosing page 1 emits a
                # Discord interaction even when it is the only page.
                "default": False,
            }
            for page in range(1, page_count + 1)
        ]
        if include_failures:
            options.append(
                {
                    "label": "View failures",
                    "value": "failures",
                    "description": "Review failed or reconciliation-required items",
                    "default": False,
                }
            )
        return {
            "kind": "string_select",
            "field": "review_page",
            "label": "Review items",
            "placeholder": (
                "Review items or view failures" if include_failures else "Review schedule items"
            ),
            "row": 1,
            "min_values": 1,
            "max_values": 1,
            "token": issue_projection_proposal_control_token(
                revision.id,
                projection_id,
                "review_page",
                expires_at,
                signing_key,
            ),
            "options": options,
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
        projection_id: uuid.UUID,
        projection_date: date,
        latest_date: date,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        fields: list[dict[str, Any]] = [
            {"name": "Status", "value": queue_item.status, "inline": True},
            {"name": "Priority", "value": queue_item.priority, "inline": True},
            {"name": "Queue date", "value": projection_date.isoformat(), "inline": True},
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
        if queue_item.received_at is not None:
            fields.append(
                {
                    "name": "Received",
                    "value": _aware(queue_item.received_at).astimezone(UTC).isoformat(),
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
        if account_label is not None:
            fields.append({"name": "Account", "value": account_label, "inline": False})
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
                fields.append(
                    {
                        "name": "Where",
                        "value": str(standalone.get("location") or "No location"),
                        "inline": False,
                    }
                )
            classification = preview.get("classification")
            if isinstance(classification, dict):
                tags = [
                    *classification.get("system_tags", []),
                    *classification.get("operator_tags", []),
                ]
                fields.append(
                    {
                        "name": "Recurrence / tags",
                        "value": ", ".join(str(value) for value in tags) or "None",
                        "inline": False,
                    }
                )
                fields.append(
                    {
                        "name": "Priority basis",
                        "value": (
                            f"{classification.get('priority', 'normal')} · "
                            f"{classification.get('priority_basis', 'default')}"
                        ),
                        "inline": True,
                    }
                )
            if "reminder_plan" in preview:
                fields.append(
                    {
                        "name": "Reminder plan",
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
                                f"{preview.get('item_count', '?')} immutable items · "
                                f"{counts.get('create', 0)} create · "
                                f"{counts.get('update', 0)} update · "
                                f"{counts.get('no_op', 0)} already synchronized"
                            ),
                            "inline": False,
                        }
                    )
                fields.append(
                    {
                        "name": "Classification",
                        "value": (
                            "Per item: recurring or one_time · timed · "
                            "course_meeting · normal/default"
                        ),
                        "inline": False,
                    }
                )
                fields.append(
                    {
                        "name": "Manifest",
                        "value": str(preview.get("manifest_sha256", "unknown"))[:16],
                        "inline": True,
                    }
                )
                freshness = preview.get("freshness")
                if isinstance(freshness, dict):
                    fields.append(
                        {
                            "name": "Conflict freshness",
                            "value": str(freshness.get("last_success_at", "unknown")),
                            "inline": False,
                        }
                    )
            if operation is not None:
                result = operation.result
                result_counts = result.get("counts") if isinstance(result, dict) else None
                value = operation.status
                if isinstance(result_counts, dict):
                    value += (
                        f"\n{result_counts.get('succeeded', 0)} succeeded · "
                        f"{result_counts.get('failed', 0)} failed · "
                        f"{result_counts.get('reconciliation_required', 0)} uncertain · "
                        f"{result_counts.get('pending', 0)} pending"
                    )
                fields.append(
                    {
                        "name": "Execution",
                        "value": value,
                        "inline": False,
                    }
                )
            conflicts = preview.get("conflicts")
            if isinstance(conflicts, list):
                conflict_text = (
                    "Clear — no exact overlaps in the fresh snapshot"
                    if not conflicts
                    else "\n".join(
                        f"{item.get('summary') or 'Untitled'} · {item.get('start_at')}"
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
            before = preview.get("before")
            if isinstance(before, dict):
                fields.append(
                    {
                        "name": "Before",
                        "value": (
                            f"{before.get('summary') or 'Untitled'}\n"
                            f"ETag: {before.get('provider_etag') or 'none'}\n"
                            f"Reminders: {self._reminder_text(before.get('provider_reminders'))}"
                        ),
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
            target = preview.get("target", {})
            if isinstance(target, dict) and target.get("calendar_id"):
                fields.append(
                    {
                        "name": "Calendar",
                        "value": str(target["calendar_id"]),
                        "inline": False,
                    }
                )
            record = preview.get("record", {})
            if isinstance(record, dict) and record.get("version") is not None:
                fields.append(
                    {
                        "name": "Record version",
                        "value": str(record["version"]),
                        "inline": True,
                    }
                )
            fields.append(
                {
                    "name": "Action",
                    "value": revision.action_type,
                    "inline": True,
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
            token = issue_projection_approval_token(
                approval.id, projection_id, approval.expires_at, signing_key
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
            if revision is not None:
                controls.extend(
                    self._proposal_selects(
                        revision,
                        approval,
                        projection_id,
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
                                projection_id,
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
                            projection_id,
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
                                projection_id,
                                "refresh",
                                approval.expires_at,
                                signing_key,
                            ),
                        }
                    )
            fields.append(
                {
                    "name": "Approval expires",
                    "value": _aware(approval.expires_at).astimezone(UTC).isoformat(),
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
            controls = [
                self._schedule_review_select(
                    revision,
                    projection_id,
                    expires_at,
                    signing_key,
                    include_failures=operation.status
                    in {
                        "partial_failed",
                        "reconciliation_required",
                        "failed",
                    },
                )
            ]
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
                    projection_id,
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
                    "value": _aware(queue_item.snoozed_until)
                    .astimezone(ZoneInfo(self.settings.timezone))
                    .isoformat(),
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
        embed = {
            "title": self._bounded(queue_item.title, 256),
            "description": self._bounded(
                (
                    f"{queue_item.summary}\n\n"
                    "Review the immutable proposal below. No provider write occurs "
                    "until approval."
                    if approval is not None and approval.status == ApprovalStatus.PENDING.value
                    else queue_item.summary
                ),
                4096,
            ),
            "fields": fields,
            "color": (
                0xC0392B
                if revision is not None and revision.action_type == "calendar_cancel_event"
                else 0xD6A756
            ),
            "timestamp": (
                _aware(revision.created_at).astimezone(UTC).isoformat()
                if revision is not None
                else _aware(queue_item.created_at).astimezone(UTC).isoformat()
            ),
            "footer": self._bounded(
                (
                    f"revision {revision.revision} · {str(revision.id)[:8]} · "
                    f"expires {_aware(approval.expires_at).astimezone(UTC).isoformat()}"
                    if revision is not None and approval is not None
                    else f"queue {str(queue_item.id)[:8]}"
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
            embed, controls, render_sha256, component_sha256 = self._render(
                queue_item,
                action,
                revision,
                approval,
                operation,
                local_revisions,
                account_label,
                projection.id,
                daily_thread.local_date,
                latest_date,
            )
            changed = (
                projection.render_sha256 != render_sha256
                or projection.component_sha256 != component_sha256
            )
            if changed and projection.render_sha256 != "0" * 64:
                projection.projection_version += 1
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
            render = {
                "title": title,
                "summary": summary,
                "error_code": error_code,
                "occurred_at": str(event.payload.get("occurred_at", event.created_at.isoformat())),
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
                    _aware(event.start_at).astimezone(UTC).isoformat()
                    if event.start_at is not None
                    else ""
                )
                end_value = (
                    _aware(event.end_at).astimezone(UTC).isoformat()
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
