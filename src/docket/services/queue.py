from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings, get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import ActionStatus, CommandStatus, OutboxStatus, QueueItemStatus
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
from docket.models import (
    Action,
    ActionRevision,
    AuditEvent,
    CommandRequest,
    DiscordDailyThread,
    DiscordProjection,
    OutboxEvent,
    QueueItem,
)
from docket.models.base import utc_now
from docket.schemas.queue import (
    IgnoreQueueItemInput,
    QueueMutationResult,
    SnoozeQueueItemInput,
)
from docket.services.source_context import validate_configured_discord_source

_ACTION_ORDER = {"snooze_queue_item": 100, "ignore_queue_item": 101}


def local_date_at_rollover(local_date: date, settings: Settings) -> datetime:
    return datetime.combine(
        local_date, time(hour=settings.daily_rollover_hour), tzinfo=ZoneInfo(settings.timezone)
    )


def queue_projection_date(queue_item: QueueItem, settings: Settings) -> date:
    received = queue_item.received_at or queue_item.created_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    return received.astimezone(ZoneInfo(settings.timezone)).date()


def serialize_queue_item(session: Session, queue_item: QueueItem) -> dict[str, Any]:
    projection_rows = session.execute(
        select(DiscordProjection, DiscordDailyThread)
        .join(
            DiscordDailyThread,
            DiscordDailyThread.id == DiscordProjection.daily_thread_id,
        )
        .where(DiscordProjection.queue_item_id == queue_item.id)
        .order_by(DiscordDailyThread.local_date)
    ).all()
    return {
        "queue_item_id": str(queue_item.id),
        "primary_source_item_id": (
            str(queue_item.primary_source_item_id)
            if queue_item.primary_source_item_id is not None
            else None
        ),
        "category": queue_item.category,
        "title": queue_item.title,
        "summary": queue_item.summary,
        "status": queue_item.status,
        "priority": queue_item.priority,
        "received_at": queue_item.received_at.isoformat() if queue_item.received_at else None,
        "snoozed_until": (
            queue_item.snoozed_until.isoformat() if queue_item.snoozed_until else None
        ),
        "snooze_local_date": (
            queue_item.snooze_local_date.isoformat() if queue_item.snooze_local_date else None
        ),
        "resolved_at": queue_item.resolved_at.isoformat() if queue_item.resolved_at else None,
        "resolution_code": queue_item.resolution_code,
        "version": queue_item.version,
        "projection_dates": [
            {
                "local_date": daily_thread.local_date.isoformat(),
                "status": projection.status,
                "projection_id": str(projection.id),
            }
            for projection, daily_thread in projection_rows
        ],
    }


def ensure_local_actions(
    session: Session,
    queue_item: QueueItem,
    *,
    projection_date: date,
    actor_type: str = "docket",
) -> list[ActionRevision]:
    """Materialize the server-owned local choices for an actionable queue item."""
    if queue_item.status not in {QueueItemStatus.PENDING.value, QueueItemStatus.FAILED.value}:
        return []
    action_types = (
        ("snooze_queue_item", "ignore_queue_item")
        if queue_item.status == QueueItemStatus.PENDING.value
        else ("ignore_queue_item",)
    )
    unavailable = session.scalars(
        select(Action).where(
            Action.queue_item_id == queue_item.id,
            Action.action_type.not_in(action_types),
            Action.status == ActionStatus.AVAILABLE.value,
        )
    ).all()
    for unavailable_action in unavailable:
        unavailable_action.status = ActionStatus.SUPERSEDED.value
    revisions: list[ActionRevision] = []
    for action_type in action_types:
        action = session.scalar(
            select(Action).where(
                Action.queue_item_id == queue_item.id,
                Action.action_type == action_type,
            )
        )
        if action is None:
            action = Action(
                queue_item_id=queue_item.id,
                action_type=action_type,
                status=ActionStatus.AVAILABLE.value,
                current_revision=1,
                display_order=_ACTION_ORDER[action_type],
            )
            session.add(action)
            session.flush()
            revision_number = 1
        else:
            current = session.scalar(
                select(ActionRevision).where(
                    ActionRevision.action_id == action.id,
                    ActionRevision.revision == action.current_revision,
                )
            )
            if (
                current is not None
                and current.target_versions.get("queue_item", {}).get("version")
                == queue_item.version
                and action.status == ActionStatus.AVAILABLE.value
            ):
                revisions.append(current)
                continue
            revision_number = action.current_revision + 1
            action.current_revision = revision_number
            action.status = ActionStatus.AVAILABLE.value

        parameters: dict[str, Any] = {
            "queue_item_id": str(queue_item.id),
            "expected_queue_version": queue_item.version,
        }
        if action_type == "snooze_queue_item":
            target_date = projection_date + timedelta(days=1)
            parameters.update(
                {
                    "snooze_local_date": target_date.isoformat(),
                    "rollover_hour": get_settings().daily_rollover_hour,
                    "reason": "Snoozed from the Docket queue card",
                }
            )
            preview: dict[str, Any] = {
                "action_type": action_type,
                "wake_local_date": target_date.isoformat(),
                "wake_local_time": f"{get_settings().daily_rollover_hour:02d}:00:00",
                "timezone": get_settings().timezone,
            }
        else:
            parameters["reason"] = "Ignored from the Docket queue card"
            preview = {"action_type": action_type, "effect": "Hide this queue item"}
        revision = ActionRevision(
            action_id=action.id,
            revision=revision_number,
            action_type=action_type,
            account_id=None,
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            preview=preview,
            preview_sha256=sha256_json(preview),
            risk_class="local_write",
            target_versions={
                "queue_item": {"id": str(queue_item.id), "version": queue_item.version}
            },
            created_by_actor_type=actor_type,
        )
        session.add(revision)
        revisions.append(revision)
    session.flush()
    return revisions


class QueueService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def get(self, queue_item_id: uuid.UUID) -> dict[str, Any]:
        item = self.session.get(QueueItem, queue_item_id)
        if item is None:
            raise DocketError(
                code="queue_item_not_found",
                message="The requested queue item does not exist.",
                details={"queue_item_id": str(queue_item_id)},
            )
        return serialize_queue_item(self.session, item)

    def list(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        local_date: date | None = None,
        priority: str | None = None,
        source_item_id: uuid.UUID | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise DocketError(code="invalid_limit", message="Queue limit must be from 1 to 100.")
        statement = select(QueueItem)
        if local_date is not None:
            statement = (
                statement.join(
                    DiscordProjection,
                    DiscordProjection.queue_item_id == QueueItem.id,
                )
                .join(
                    DiscordDailyThread,
                    DiscordDailyThread.id == DiscordProjection.daily_thread_id,
                )
                .where(DiscordDailyThread.local_date == local_date)
            )
        if status is not None:
            statement = statement.where(QueueItem.status == status)
        if category is not None:
            statement = statement.where(QueueItem.category == category)
        if priority is not None:
            statement = statement.where(QueueItem.priority == priority)
        if source_item_id is not None:
            statement = statement.where(QueueItem.primary_source_item_id == source_item_id)
        items = (
            self.session.scalars(
                statement.order_by(QueueItem.created_at.desc(), QueueItem.id).limit(limit)
            )
            .unique()
            .all()
        )
        return [serialize_queue_item(self.session, item) for item in items]

    def _start_command(
        self, *, request_key: str, operation_name: str, payload: dict[str, Any], actor_id: str
    ) -> tuple[CommandRequest, QueueMutationResult | None]:
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request_key)
        )
        if existing is not None:
            if existing.operation_name != operation_name or existing.input_sha256 != input_sha256:
                raise IdempotencyConflict(
                    request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation=operation_name,
                )
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                replay = dict(existing.result)
                replay["disposition"] = "replayed_request"
                return existing, QueueMutationResult.model_validate(replay)
            raise DocketError(
                code="request_in_progress",
                message="The queue request exists but has not completed successfully.",
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

    def _load_expected(self, queue_item_id: uuid.UUID, expected_version: int) -> QueueItem:
        item = self.session.get(QueueItem, queue_item_id)
        if item is None:
            raise DocketError(code="queue_item_not_found", message="Queue item was not found.")
        if item.version != expected_version:
            raise VersionConflict(str(item.id), expected_version, item.version)
        return item

    def enqueue_refresh(self, item: QueueItem, reason: str) -> None:
        newest = self.session.execute(
            select(DiscordProjection, DiscordDailyThread)
            .join(
                DiscordDailyThread,
                DiscordDailyThread.id == DiscordProjection.daily_thread_id,
            )
            .where(DiscordProjection.queue_item_id == item.id)
            .order_by(DiscordDailyThread.local_date.desc())
            .limit(1)
        ).first()
        payload: dict[str, Any] = {"queue_item_id": str(item.id), "reason": reason}
        if newest is not None:
            projection, daily_thread = newest
            payload.update(
                {
                    "projection_id": str(projection.id),
                    "target_local_date": daily_thread.local_date.isoformat(),
                }
            )
        else:
            payload["target_local_date"] = queue_projection_date(item, self.settings).isoformat()
        self.session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=item.id,
                deduplication_key=f"discord_projection:{item.id}:state:{item.version}",
                payload=payload,
                status=OutboxStatus.PENDING.value,
            )
        )

    @staticmethod
    def _finish(command: CommandRequest, result: QueueMutationResult) -> None:
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()

    def snooze(self, request: SnoozeQueueItemInput) -> QueueMutationResult:
        validate_configured_discord_source(request.source, request.actor_id)
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="docket_snooze_queue_item",
            payload=payload,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay
        item = self._load_expected(request.queue_item_id, request.expected_version)
        if item.status != QueueItemStatus.PENDING.value:
            raise DocketError(
                code="invalid_queue_transition",
                message="Only a pending queue item can be snoozed.",
                details={"status": item.status},
            )
        if request.snooze_local_date is not None:
            wake = local_date_at_rollover(request.snooze_local_date, self.settings)
        else:
            assert request.snoozed_until is not None
            wake = request.snoozed_until.astimezone(UTC)
        if wake <= utc_now():
            raise DocketError(code="invalid_snooze_time", message="Snooze time must be future.")
        item.status = QueueItemStatus.SNOOZED.value
        item.snoozed_until = wake
        item.snooze_local_date = request.snooze_local_date
        item.version += 1
        self._supersede_local_actions(item)
        self.enqueue_refresh(item, "snoozed")
        self.session.add(
            AuditEvent(
                event_type="queue_item.snoozed",
                entity_type="queue_item",
                entity_id=item.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={"version": item.version, "snoozed_until": wake.isoformat()},
            )
        )
        result = QueueMutationResult(
            request_id=command.id,
            queue_item_id=item.id,
            version=item.version,
            status="snoozed",
            disposition="updated",
            snoozed_until=wake,
            snooze_local_date=request.snooze_local_date,
        )
        self._finish(command, result)
        return result

    def ignore(self, request: IgnoreQueueItemInput) -> QueueMutationResult:
        validate_configured_discord_source(request.source, request.actor_id)
        payload = request.model_dump(mode="json")
        command, replay = self._start_command(
            request_key=request.request_key,
            operation_name="docket_ignore_queue_item",
            payload=payload,
            actor_id=request.actor_id,
        )
        if replay is not None:
            return replay
        item = self._load_expected(request.queue_item_id, request.expected_version)
        if item.status not in {QueueItemStatus.PENDING.value, QueueItemStatus.FAILED.value}:
            raise DocketError(
                code="invalid_queue_transition",
                message="Only a pending or failed queue item can be ignored.",
                details={"status": item.status},
            )
        item.status = QueueItemStatus.IGNORED.value
        item.resolved_at = utc_now()
        item.resolution_code = "operator_ignored"
        item.resolution_note = request.reason
        item.version += 1
        self._supersede_local_actions(item)
        self.enqueue_refresh(item, "ignored")
        self.session.add(
            AuditEvent(
                event_type="queue_item.ignored",
                entity_type="queue_item",
                entity_id=item.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={"version": item.version, "reason": request.reason},
            )
        )
        result = QueueMutationResult(
            request_id=command.id,
            queue_item_id=item.id,
            version=item.version,
            status="ignored",
            disposition="updated",
        )
        self._finish(command, result)
        return result

    def _supersede_local_actions(self, item: QueueItem) -> None:
        actions = self.session.scalars(
            select(Action).where(
                Action.queue_item_id == item.id,
                Action.action_type.in_(tuple(_ACTION_ORDER)),
                Action.status == ActionStatus.AVAILABLE.value,
            )
        ).all()
        for action in actions:
            action.status = ActionStatus.SUPERSEDED.value
