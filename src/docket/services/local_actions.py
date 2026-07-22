from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import ActionStatus, CommandStatus, QueueItemStatus, RiskClass
from docket.domain.errors import DocketError
from docket.internal_api.schemas import LocalActionResponse
from docket.models import (
    Action,
    ActionRevision,
    AuditEvent,
    CommandRequest,
    DiscordDailyThread,
    DiscordProjection,
    QueueItem,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.security import (
    decode_projection_local_action_token,
    verify_projection_local_action_token,
)
from docket.services.queue import QueueService, local_date_at_rollover


class LocalActionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def _validate_context(self, request: LocalActionResponse) -> DiscordProjection:
        if (
            request.discord_user_id != self.settings.operator_discord_user_id
            or request.guild_id != self.settings.discord_guild_id
            or request.parent_channel_id != self.settings.queue_channel_id
            or request.channel_id == self.settings.queue_channel_id
            or request.projection_id is None
        ):
            raise DocketError(
                code="invalid_local_action_context",
                message="Local action did not come from the configured queue card context.",
            )
        projection = self.session.get(DiscordProjection, request.projection_id)
        if (
            projection is None
            or projection.status != "delivered"
            or projection.message_id != request.message_id
        ):
            raise DocketError(
                code="invalid_local_action_projection",
                message="Local action is not bound to a delivered Docket card.",
            )
        daily_thread = self.session.get(DiscordDailyThread, projection.daily_thread_id)
        if (
            daily_thread is None
            or daily_thread.guild_id != request.guild_id
            or daily_thread.channel_id != request.parent_channel_id
            or daily_thread.thread_id != request.channel_id
        ):
            raise DocketError(
                code="invalid_local_action_projection",
                message="Local action thread does not match the stored projection.",
            )
        newest = self.session.scalar(
            select(DiscordProjection)
            .join(
                DiscordDailyThread,
                DiscordDailyThread.id == DiscordProjection.daily_thread_id,
            )
            .where(DiscordProjection.queue_item_id == projection.queue_item_id)
            .order_by(DiscordDailyThread.local_date.desc())
            .limit(1)
        )
        if newest is None or newest.id != projection.id:
            raise DocketError(
                code="stale_local_action_projection",
                message="This control belongs to an older queue projection.",
            )
        return projection

    def _start_command(self, request: LocalActionResponse) -> CommandRequest:
        request_key = f"discord-interaction:{request.discord_interaction_id}"
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request_key)
        )
        if existing is not None:
            raise DocketError(
                code="interaction_replay",
                message="This Discord interaction has already been consumed.",
            )
        payload = request.model_dump(mode="json", exclude={"action_token"})
        command = CommandRequest(
            request_key=request_key,
            operation_name="discord_local_action",
            input_sha256=sha256_json(payload),
            actor_type="plugin",
            actor_id=request.discord_user_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command

    def respond(self, request: LocalActionResponse) -> dict[str, object]:
        projection = self._validate_context(request)
        decoded = decode_projection_local_action_token(request.action_token)
        if decoded is None:
            raise DocketError(
                code="invalid_local_action_token", message="Control token is invalid."
            )
        token_revision_id, token_projection_id, token_queue_version, expires_at = decoded
        if token_revision_id != request.action_revision_id or token_projection_id != projection.id:
            raise DocketError(
                code="invalid_local_action_token",
                message="Control token does not match the submitted action and projection.",
            )
        now = utc_now()
        if now > expires_at:
            raise DocketError(
                code="local_action_expired", message="This local control has expired."
            )
        signing_key = self.settings.read_secret(self.settings.interaction_signing_key_file).encode()
        if not verify_projection_local_action_token(
            request.action_token,
            action_revision_id=request.action_revision_id,
            projection_id=projection.id,
            queue_version=token_queue_version,
            expires_at=expires_at,
            signing_key=signing_key,
        ):
            raise DocketError(
                code="invalid_local_action_token", message="Control token is invalid."
            )
        command = self._start_command(request)

        revision = self.session.get(ActionRevision, request.action_revision_id)
        action = self.session.get(Action, revision.action_id) if revision is not None else None
        queue_item = self.session.get(QueueItem, projection.queue_item_id)
        if revision is None or action is None or queue_item is None:
            raise DocketError(
                code="invalid_local_action_state", message="Local action is incomplete."
            )
        definition = get_action_definition(revision.action_type)
        if (
            definition.risk_class is not RiskClass.LOCAL_WRITE
            or action.queue_item_id != queue_item.id
            or action.current_revision != revision.revision
            or action.status != ActionStatus.AVAILABLE.value
            or token_queue_version != queue_item.version
            or revision.target_versions.get("queue_item")
            != {"id": str(queue_item.id), "version": queue_item.version}
            or sha256_json(revision.parameters) != revision.parameters_sha256
            or sha256_json(revision.preview) != revision.preview_sha256
        ):
            raise DocketError(
                code="stale_local_action",
                message="The queue item or local action changed after this card was rendered.",
            )
        if revision.action_type == "snooze_queue_item":
            if queue_item.status != QueueItemStatus.PENDING.value:
                raise DocketError(
                    code="invalid_queue_transition",
                    message="Only a pending queue item can be snoozed.",
                )
            target_date = datetime.fromisoformat(
                str(revision.parameters["snooze_local_date"])
            ).date()
            queue_item.status = QueueItemStatus.SNOOZED.value
            queue_item.snooze_local_date = target_date
            queue_item.snoozed_until = local_date_at_rollover(target_date, self.settings)
            event_type = "queue_item.snoozed"
        elif revision.action_type == "ignore_queue_item":
            if queue_item.status not in {
                QueueItemStatus.PENDING.value,
                QueueItemStatus.FAILED.value,
            }:
                raise DocketError(
                    code="invalid_queue_transition",
                    message="Only a pending or failed queue item can be ignored.",
                )
            queue_item.status = QueueItemStatus.IGNORED.value
            queue_item.resolved_at = now
            queue_item.resolution_code = "operator_ignored"
            queue_item.resolution_note = str(revision.parameters["reason"])
            event_type = "queue_item.ignored"
        else:
            raise DocketError(
                code="invalid_local_action_state",
                message="This local action has no queue transition handler.",
            )
        queue_item.version += 1
        action.status = ActionStatus.SUCCEEDED.value
        siblings = self.session.scalars(
            select(Action).where(
                Action.queue_item_id == queue_item.id,
                Action.id != action.id,
                Action.action_type.in_(("snooze_queue_item", "ignore_queue_item")),
                Action.status == ActionStatus.AVAILABLE.value,
            )
        ).all()
        for sibling in siblings:
            sibling.status = ActionStatus.SUPERSEDED.value
        QueueService(self.session, self.settings).enqueue_refresh(queue_item, revision.action_type)
        self.session.add(
            AuditEvent(
                event_type=event_type,
                entity_type="queue_item",
                entity_id=queue_item.id,
                actor_type="plugin",
                actor_id=request.discord_user_id,
                request_id=command.id,
                data={
                    "action_revision_id": str(revision.id),
                    "discord_interaction_id": request.discord_interaction_id,
                    "version": queue_item.version,
                },
            )
        )
        result: dict[str, object] = {
            "ok": True,
            "action_type": revision.action_type,
            "action_id": str(action.id),
            "action_status": action.status,
            "queue_item_id": str(queue_item.id),
            "queue_status": queue_item.status,
            "queue_version": queue_item.version,
        }
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = now.astimezone(UTC)
        return result
