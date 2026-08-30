from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.models import (
    AuditEvent,
    CanonicalEvent,
    Item,
    OperatorProjection,
    OutboxEvent,
    ProjectionDelivery,
    ReminderPlan,
    ScheduledNotification,
    Task,
    TemporalBinding,
)
from docket.models.base import utc_now
from docket.schemas.calendar import StandaloneCalendarEventInput
from docket.schemas.tracked_context import (
    DateIntervalTemporalValue,
    DateTemporalValue,
    DateTimeIntervalTemporalValue,
    DateTimeTemporalValue,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _local_instant(
    local_date: date,
    local_time: time,
    timezone: str,
    *,
    fold: int = 0,
) -> datetime:
    return datetime.combine(local_date, local_time).replace(
        tzinfo=ZoneInfo(timezone), fold=fold
    ).astimezone(UTC)


def _plan_date_time(plan: ReminderPlan) -> tuple[time, str]:
    if plan.date_trigger_local_time is None or plan.timezone is None:
        raise ValueError("date-only reminder has no explicit local trigger time")
    return time.fromisoformat(plan.date_trigger_local_time), plan.timezone


def _temporal_target(plan: ReminderPlan, binding: TemporalBinding) -> datetime:
    kind = binding.temporal_value.get("kind")
    if kind == "datetime":
        point = DateTimeTemporalValue.model_validate(binding.temporal_value)
        return point.local_datetime.replace(
            tzinfo=ZoneInfo(point.timezone), fold=point.fold or 0
        ).astimezone(UTC)
    if kind == "datetime_interval":
        interval = DateTimeIntervalTemporalValue.model_validate(binding.temporal_value)
        return interval.start_local.replace(
            tzinfo=ZoneInfo(interval.timezone), fold=interval.fold or 0
        ).astimezone(UTC)
    trigger_time, timezone = _plan_date_time(plan)
    if kind == "date":
        target_date = DateTemporalValue.model_validate(binding.temporal_value).date
    else:
        target_date = DateIntervalTemporalValue.model_validate(
            binding.temporal_value
        ).start_date
    return _local_instant(target_date, trigger_time, timezone)


def _event_target(plan: ReminderPlan, event: CanonicalEvent) -> datetime:
    spec = StandaloneCalendarEventInput.model_validate(event.event_spec)
    timing = spec.timing
    if timing.kind == "timed":
        return timing.start_local.replace(
            tzinfo=ZoneInfo(timing.timezone), fold=timing.fold or 0
        ).astimezone(UTC)
    trigger_time, timezone = _plan_date_time(plan)
    return _local_instant(timing.start_date, trigger_time, timezone)


def _subject_title(session: Session, plan: ReminderPlan) -> tuple[str, datetime]:
    if plan.subject_ref.startswith("evt_"):
        event = session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.ref_id == plan.subject_ref)
        )
        if event is None or event.status != "active":
            raise ValueError("reminder Event is not active")
        return event.title, _event_target(plan, event)
    binding = session.scalar(
        select(TemporalBinding).where(TemporalBinding.ref_id == plan.subject_ref)
    )
    if binding is None or binding.canonical_status != "active":
        raise ValueError("reminder TemporalBinding is not active")
    if binding.subject_ref.startswith("item_"):
        title = session.scalar(select(Item.title).where(Item.ref_id == binding.subject_ref))
    else:
        title = session.scalar(select(Task.title).where(Task.ref_id == binding.subject_ref))
    if title is None:
        raise ValueError("reminder temporal subject is unavailable")
    return title, _temporal_target(plan, binding)


class ReminderService:
    """Materialize and deliver clean ReminderPlan queue projections."""

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

    @staticmethod
    def _cancel_unwanted(session: Session, active_plan_refs: set[str]) -> int:
        changed = 0
        pending = session.scalars(
            select(ScheduledNotification).where(
                ScheduledNotification.status.in_(("pending", "delivering"))
            )
        ).all()
        for notification in pending:
            if notification.reminder_plan_ref not in active_plan_refs:
                notification.status = "cancelled"
                notification.last_error_code = "reminder_plan_inactive"
                changed += 1
        return changed

    @staticmethod
    def _reconcile(session: Session) -> int:
        changed = 0
        notifications = session.scalars(
            select(ScheduledNotification).where(
                ScheduledNotification.status == "delivering",
                ScheduledNotification.outbox_event_id.is_not(None),
            )
        ).all()
        for notification in notifications:
            assert notification.outbox_event_id is not None
            outbox = session.get(OutboxEvent, notification.outbox_event_id)
            if outbox is None:
                notification.status = "failed"
                notification.last_error_code = "reminder_outbox_missing"
                changed += 1
            elif outbox.status == "delivered":
                notification.status = "delivered"
                notification.last_error_code = None
                changed += 1
            elif outbox.status == "failed":
                notification.status = "failed"
                notification.last_error_code = outbox.last_error_code or "reminder_delivery_failed"
                changed += 1
        return changed

    def _materialize(self, session: Session, now: datetime) -> int:
        plans = session.scalars(
            select(ReminderPlan).where(ReminderPlan.canonical_status == "active")
        ).all()
        active_queue_refs = {
            plan.ref_id for plan in plans if "docket_queue" in plan.delivery_channels
        }
        changed = self._cancel_unwanted(session, active_queue_refs)
        for plan in plans:
            if "docket_queue" not in plan.delivery_channels:
                continue
            try:
                _title, target = _subject_title(session, plan)
            except ValueError as exc:
                for lead_seconds in plan.lead_seconds:
                    trigger_key = f"v{plan.version}:lead:{lead_seconds}"
                    existing = session.scalar(
                        select(ScheduledNotification).where(
                            ScheduledNotification.reminder_plan_ref == plan.ref_id,
                            ScheduledNotification.trigger_key == trigger_key,
                        )
                    )
                    if existing is None:
                        session.add(
                            ScheduledNotification(
                                reminder_plan_ref=plan.ref_id,
                                trigger_key=trigger_key,
                                scheduled_for=now,
                                status="failed",
                                last_error_code=str(exc)[:128],
                            )
                        )
                        changed += 1
                continue
            for lead_seconds in plan.lead_seconds:
                trigger_key = f"v{plan.version}:lead:{lead_seconds}"
                existing = session.scalar(
                    select(ScheduledNotification).where(
                        ScheduledNotification.reminder_plan_ref == plan.ref_id,
                        ScheduledNotification.trigger_key == trigger_key,
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    ScheduledNotification(
                        reminder_plan_ref=plan.ref_id,
                        trigger_key=trigger_key,
                        scheduled_for=target - timedelta(seconds=lead_seconds),
                        status="failed" if target <= now else "pending",
                        last_error_code="reminder_target_elapsed" if target <= now else None,
                    )
                )
                changed += 1
        return changed

    def _enqueue_one(self, session: Session, now: datetime) -> bool:
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
        plan = session.scalar(
            select(ReminderPlan).where(ReminderPlan.ref_id == notification.reminder_plan_ref)
        )
        if plan is None or plan.canonical_status != "active":
            notification.status = "cancelled"
            notification.last_error_code = "reminder_plan_inactive"
            return True
        try:
            title, target = _subject_title(session, plan)
        except ValueError as exc:
            notification.status = "failed"
            notification.last_error_code = str(exc)[:128]
            return True
        if target <= now:
            notification.status = "failed"
            notification.last_error_code = "reminder_target_elapsed"
            return True
        local_target = target.astimezone(ZoneInfo(self.settings.timezone))
        visible_text = (
            f"Reminder: {title}\n"
            f"Target: {local_target.strftime('%Y-%m-%d %H:%M %Z')}"
        )
        semantic_content = {
            "reminder_plan_ref": plan.ref_id,
            "subject_ref": plan.subject_ref,
            "trigger_key": notification.trigger_key,
            "target_at": target.isoformat(),
        }
        projection = OperatorProjection(
            projection_kind="reminder",
            operator_ref=f"discord_user:{self.settings.operator_discord_user_id}",
            primary_public_ref=plan.ref_id,
            semantic_content=semantic_content,
            visible_text=visible_text,
            render_schema_version=1,
            render_sha256=sha256_json(semantic_content),
            component_sha256=sha256_json({"components": []}),
            basis_refs=list(plan.basis_refs),
        )
        session.add(projection)
        session.flush()
        session.add(
            ProjectionDelivery(
                projection_id=projection.id,
                projection_ref=projection.ref_id,
                transport="discord",
                destination_ref=(
                    f"discord_conversation:{self.settings.discord_guild_id}:"
                    f"{self.settings.queue_channel_id}"
                ),
                status="pending",
            )
        )
        outbox = OutboxEvent(
            event_type="discord.projection.requested",
            aggregate_type="operator_projection",
            aggregate_id=projection.id,
            deduplication_key=f"reminder_projection:{plan.ref_id}:{notification.trigger_key}",
            payload={"projection_ref": projection.ref_id},
            status="pending",
        )
        session.add(outbox)
        session.flush()
        notification.status = "delivering"
        notification.outbox_event_id = outbox.id
        notification.attempt_count += 1
        notification.last_error_code = None
        session.add(
            AuditEvent(
                event_type="reminder.projection_enqueued",
                entity_type="reminder_plan",
                entity_id=plan.id,
                actor_type="docket",
                primary_ref=plan.ref_id,
                affected_refs=[projection.ref_id],
                basis_refs=list(plan.basis_refs),
                data={"trigger_key": notification.trigger_key},
            )
        )
        return True

    def run_due_once(self) -> bool:
        now = _aware(self.clock()).astimezone(UTC)
        with self.session_factory.begin() as session:
            changed = bool(self._reconcile(session))
            changed = bool(self._materialize(session, now)) or changed
            return self._enqueue_one(session, now) or changed
