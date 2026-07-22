from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import ApprovalStatus, OutboxStatus
from docket.models import (
    Action,
    ActionRevision,
    Approval,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
)
from docket.models.base import utc_now
from docket.providers.discord import DiscordProjectionAdapter, DiscordProjectionError
from docket.security import issue_projection_approval_token

_SUPPORTED_EVENTS = {
    "discord.projection.requested",
    "discord.projection.refresh_requested",
}


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
            local_date = self._local_date(queue_item, self.settings)
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
            return daily_thread.id, projection.id

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

    def _render(
        self,
        queue_item: QueueItem,
        action: Action | None,
        revision: ActionRevision | None,
        approval: Approval | None,
        projection_id: uuid.UUID,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        fields: list[dict[str, Any]] = [
            {"name": "Status", "value": queue_item.status, "inline": True},
            {"name": "Priority", "value": queue_item.priority, "inline": True},
        ]
        if revision is not None:
            preview = revision.preview
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
            fields.append(
                {
                    "name": "Approval expires",
                    "value": _aware(approval.expires_at).astimezone(UTC).isoformat(),
                    "inline": False,
                }
            )
        embed = {
            "title": self._bounded(queue_item.title, 256),
            "description": queue_item.summary,
            "fields": fields,
            "color": 0xD6A756,
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
            action = session.scalar(select(Action).where(Action.queue_item_id == queue_item.id))
            revision = None
            approval = None
            if action is not None:
                revision = session.scalar(
                    select(ActionRevision).where(
                        ActionRevision.action_id == action.id,
                        ActionRevision.revision == action.current_revision,
                    )
                )
                if revision is not None:
                    approval = session.scalar(
                        select(Approval).where(Approval.action_revision_id == revision.id)
                    )
            embed, controls, render_sha256, component_sha256 = self._render(
                queue_item, action, revision, approval, projection.id
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
            action = session.scalar(
                select(Action).where(Action.queue_item_id == projection.queue_item_id)
            )
            revision = (
                session.scalar(
                    select(ActionRevision).where(
                        ActionRevision.action_id == action.id,
                        ActionRevision.revision == action.current_revision,
                    )
                )
                if action is not None
                else None
            )
            approval = (
                session.scalar(
                    select(Approval).where(Approval.action_revision_id == revision.id)
                )
                if revision is not None
                else None
            )
            if approval is not None and approval.status == ApprovalStatus.PENDING.value:
                approval.control_projection_id = projection.id
            elif approval is not None:
                approval.control_projection_id = None
            event.status = OutboxStatus.DELIVERED.value
            event.lease_token = None
            event.leased_until = None
            event.last_error_code = None

    def _retry(self, event_id: uuid.UUID, lease_token: uuid.UUID, code: str) -> None:
        with self.session_factory.begin() as session:
            event = session.get(OutboxEvent, event_id)
            if event is None or event.lease_token != lease_token:
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
                event_id, lease_token, projection_id, projection_request, projection_ack
            )
        except DiscordProjectionError as exc:
            self._retry(event_id, lease_token, exc.code)
        except Exception:
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
