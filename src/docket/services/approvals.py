import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import (
    ActionStatus,
    ApprovalStatus,
    OperationStatus,
    OutboxStatus,
    QueueItemStatus,
)
from docket.domain.errors import DocketError
from docket.internal_api.schemas import ApprovalResponse
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarEventCache,
    CalendarReminderPlan,
    CalendarScheduleSnapshot,
    CalendarSyncState,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OperationItem,
    OutboxEvent,
    QueueItem,
    Record,
)
from docket.models.base import utc_now
from docket.security import (
    short_code_sha256,
    verify_projection_approval_token,
    verify_projection_decision_approval_token,
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _validate_context(request: ApprovalResponse) -> None:
        settings = get_settings()
        if (
            request.discord_user_id != settings.operator_discord_user_id
            or request.guild_id != settings.discord_guild_id
        ):
            raise DocketError(
                code="invalid_approval_context",
                message="Approval response did not come from the configured Discord context.",
            )
        if request.short_code is not None:
            if request.channel_id != settings.queue_channel_id:
                raise DocketError(
                    code="invalid_approval_context",
                    message="Fallback approval did not come from the configured queue channel.",
                )
            return
        if (
            request.parent_channel_id != settings.queue_channel_id
            or request.channel_id == settings.queue_channel_id
        ):
            raise DocketError(
                code="invalid_approval_context",
                message="Button approval did not come from a configured queue thread.",
            )

    def _resolve(self, request: ApprovalResponse) -> Approval:
        replay = self.session.scalar(
            select(Approval).where(
                Approval.discord_interaction_id == request.discord_interaction_id
            )
        )
        if replay is not None:
            raise DocketError(
                code="interaction_replay",
                message="This Discord interaction has already been consumed.",
            )
        if request.short_code is not None:
            approval = self.session.scalar(
                select(Approval)
                .where(Approval.short_code_sha256 == short_code_sha256(request.short_code))
                .with_for_update()
            )
        else:
            approval = self.session.scalar(
                select(Approval).where(Approval.id == request.approval_id).with_for_update()
            )
        if approval is None:
            raise DocketError(
                code="approval_not_found", message="Approval reference was not found."
            )
        return approval

    def _validate_projection_context(
        self,
        request: ApprovalResponse,
        approval: Approval,
        revision: ActionRevision,
        queue_item: QueueItem,
    ) -> None:
        if request.short_code is not None:
            return
        assert request.projection_id is not None
        assert request.approval_token is not None
        projection = self.session.scalar(
            select(DiscordProjection)
            .where(DiscordProjection.id == request.projection_id)
            .with_for_update()
        )
        if (
            projection is None
            or projection.queue_item_id != queue_item.id
            or projection.status != "delivered"
            or projection.message_id != request.message_id
            or approval.control_projection_id != projection.id
        ):
            raise DocketError(
                code="invalid_approval_projection",
                message="The interaction is not bound to the active delivered approval card.",
            )
        daily_thread = self.session.get(DiscordDailyThread, projection.daily_thread_id)
        settings = get_settings()
        if (
            daily_thread is None
            or daily_thread.guild_id != request.guild_id
            or daily_thread.channel_id != request.parent_channel_id
            or daily_thread.channel_id != settings.queue_channel_id
            or daily_thread.thread_id != request.channel_id
        ):
            raise DocketError(
                code="invalid_approval_projection",
                message="The interaction thread does not match the stored projection context.",
            )
        signing_key = settings.read_secret(settings.interaction_signing_key_file).encode()
        if revision.action_type == "calendar_apply_term_schedule":
            page_count = (int(revision.preview.get("item_count", 0)) + 9) // 10
            valid = (
                projection.view_action_revision_id == revision.id
                and projection.view_mode == "decision"
                and projection.view_page is None
                and page_count >= 1
                and projection.reviewed_through_page == page_count
                and verify_projection_decision_approval_token(
                    request.approval_token,
                    approval_id=approval.id,
                    projection_id=projection.id,
                    projection_version=projection.projection_version,
                    expires_at=approval.expires_at,
                    signing_key=signing_key,
                )
            )
        else:
            valid = verify_projection_approval_token(
                request.approval_token,
                approval_id=approval.id,
                projection_id=projection.id,
                expires_at=approval.expires_at,
                signing_key=signing_key,
            )
        if not valid:
            raise DocketError(
                code="invalid_approval_token",
                message="The approval token is invalid for the current card view.",
            )

    def _load_bound_state(self, approval: Approval) -> tuple[ActionRevision, Action, QueueItem]:
        revision = self.session.get(ActionRevision, approval.action_revision_id)
        if revision is None:
            raise DocketError(code="invalid_approval_state", message="Action revision is missing.")
        action = self.session.get(Action, revision.action_id)
        if action is None or action.queue_item_id is None:
            raise DocketError(code="invalid_approval_state", message="Action state is incomplete.")
        queue_item = self.session.get(QueueItem, action.queue_item_id)
        if queue_item is None:
            raise DocketError(code="invalid_approval_state", message="Queue item is missing.")
        return revision, action, queue_item

    def _validate_binding(
        self,
        approval: Approval,
        revision: ActionRevision,
        action: Action,
        queue_item: QueueItem,
    ) -> None:
        if approval.status != ApprovalStatus.PENDING.value:
            raise DocketError(
                code="approval_not_pending",
                message="This approval is no longer pending.",
                details={"status": approval.status},
            )
        if action.current_revision != revision.revision or action.status != (
            ActionStatus.APPROVAL_PENDING.value
        ):
            raise DocketError(
                code="approval_superseded",
                message="The approval is not for the current pending action revision.",
            )
        if (
            sha256_json(revision.parameters) != revision.parameters_sha256
            or sha256_json(revision.preview) != revision.preview_sha256
        ):
            raise DocketError(
                code="approval_binding_mismatch",
                message="The immutable action hashes no longer match their stored content.",
            )
        account = self.session.get(Account, revision.account_id)
        if (
            account is None
            or not account.enabled
            or account.provider != "google"
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="target_account_changed",
                message="The selected Google Calendar account is no longer enabled.",
            )
        if revision.parameters.get("calendar_id") != get_settings().google_calendar_id:
            raise DocketError(
                code="target_calendar_changed",
                message="The approved target is no longer the configured Docket calendar.",
            )
        queue_target = revision.target_versions.get("queue_item", {})
        record_target = revision.target_versions.get("record")
        if record_target is not None:
            try:
                record_id = uuid.UUID(str(record_target.get("id")))
            except (AttributeError, ValueError) as exc:
                raise DocketError(
                    code="approval_binding_mismatch",
                    message="The action contains an invalid target record binding.",
                ) from exc
            record = self.session.get(Record, record_id)
            if record is None or record.version != record_target.get("version"):
                raise DocketError(
                    code="target_version_changed",
                    message="The target record changed after the approval preview was created.",
                )
        calendar_target = revision.target_versions.get("calendar_snapshot")
        if isinstance(calendar_target, dict):
            sync_state = self.session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.account_id == revision.account_id,
                    CalendarSyncState.calendar_id == revision.parameters.get("calendar_id"),
                )
            )
            if (
                sync_state is None
                or sync_state.status != "current"
                or sync_state.last_success_at is None
                or _as_utc(sync_state.last_success_at).isoformat()
                != calendar_target.get("last_success_at")
                or (utc_now() - _as_utc(sync_state.last_success_at)).total_seconds()
                > get_settings().calendar_stale_seconds
            ):
                raise DocketError(
                    code="target_version_changed",
                    message=(
                        "The complete Calendar snapshot changed or became stale "
                        "after the approval preview was created."
                    ),
                )
        if (
            isinstance(calendar_target, dict)
            and calendar_target.get("provider_event_id") is not None
        ):
            event = self.session.scalar(
                select(CalendarEventCache).where(
                    CalendarEventCache.account_id == revision.account_id,
                    CalendarEventCache.calendar_id == revision.parameters.get("calendar_id"),
                    CalendarEventCache.provider_event_id == calendar_target["provider_event_id"],
                )
            )
            if (
                event is None
                or event.status == "cancelled"
                or event.provider_etag != calendar_target.get("provider_etag")
                or event.has_attendees
                or event.organizer_is_self is False
            ):
                raise DocketError(
                    code="target_version_changed",
                    message="The Calendar event changed after the approval preview was created.",
                )
        schedule_target = revision.target_versions.get("schedule_snapshot")
        if isinstance(schedule_target, dict):
            try:
                snapshot_id = uuid.UUID(str(schedule_target.get("id")))
            except ValueError as exc:
                raise DocketError(
                    code="approval_binding_mismatch",
                    message="The schedule snapshot binding is invalid.",
                ) from exc
            snapshot = self.session.get(CalendarScheduleSnapshot, snapshot_id)
            if (
                snapshot is None
                or snapshot.manifest_sha256 != schedule_target.get("manifest_sha256")
                or sha256_json(snapshot.manifest) != snapshot.manifest_sha256
            ):
                raise DocketError(
                    code="target_version_changed",
                    message="The bound schedule snapshot changed after proposal.",
                )
            term = self.session.get(Record, snapshot.term_record_id)
            if term is None or term.version != snapshot.term_record_version:
                raise DocketError(
                    code="target_version_changed",
                    message="The bound term changed after proposal.",
                )
            for item in snapshot.manifest.get("items", []):
                if not isinstance(item, dict):
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The schedule manifest contains an invalid item.",
                    )
                try:
                    record_id = uuid.UUID(str(item["course_record_id"]))
                    record_version = int(item["course_record_version"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The schedule manifest contains an invalid record binding.",
                    ) from exc
                record = self.session.get(Record, record_id)
                if record is None or record.version != record_version:
                    raise DocketError(
                        code="target_version_changed",
                        message="A bound course changed after proposal.",
                    )
        if str(queue_item.id) != queue_target.get("id") or queue_item.version != queue_target.get(
            "version"
        ):
            raise DocketError(
                code="target_version_changed",
                message="The queue item changed after the approval preview was created.",
            )
        projection = self.session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_type == "queue_item",
                OutboxEvent.aggregate_id == queue_item.id,
                OutboxEvent.event_type.in_(
                    (
                        "discord.projection.requested",
                        "discord.projection.refresh_requested",
                    )
                ),
            )
        )
        if projection is None:
            raise DocketError(
                code="approval_projection_missing",
                message="The approval does not have a matching projection request.",
            )

    @staticmethod
    def _idempotency_key(revision: ActionRevision) -> str:
        parameters = revision.parameters
        if revision.action_type == "calendar_create_meeting":
            return (
                f"calendar:create:{revision.account_id}:{parameters['record_id']}:"
                f"{parameters['meeting_id']}:{parameters['record_version']}"
            )
        if revision.action_type == "calendar_update_meeting":
            return (
                f"calendar:update:{revision.account_id}:{parameters['external_event_id']}:"
                f"{parameters['record_version']}:{revision.preview_sha256}"
            )
        if revision.action_type == "calendar_create_event":
            return (
                f"calendar:create-event:{revision.account_id}:"
                f"{parameters['logical_key']}:{revision.parameters_sha256}"
            )
        if revision.action_type == "calendar_update_event":
            return (
                f"calendar:update-event:{revision.account_id}:"
                f"{parameters['external_event_id']}:{parameters.get('provider_etag')}:"
                f"{revision.preview_sha256}"
            )
        if revision.action_type == "calendar_update_reminders":
            return (
                f"calendar:update-reminders:{revision.account_id}:"
                f"{parameters['external_event_id']}:{parameters.get('provider_etag')}:"
                f"{parameters['reminder_plan_sha256']}"
            )
        if revision.action_type == "calendar_cancel_event":
            return (
                f"calendar:cancel-event:{revision.account_id}:"
                f"{parameters['external_event_id']}:{parameters.get('provider_etag')}"
            )
        if revision.action_type == "calendar_apply_term_schedule":
            return (
                f"calendar:apply-schedule:{revision.account_id}:"
                f"{parameters['schedule_snapshot_id']}:"
                f"{parameters['manifest_sha256']}"
            )
        raise DocketError(
            code="invalid_approval_state",
            message="The approved action has no external operation handler.",
        )

    def respond(self, request: ApprovalResponse) -> dict[str, Any]:
        self._validate_context(request)
        approval = self._resolve(request)
        revision, action, queue_item = self._load_bound_state(approval)
        self._validate_projection_context(request, approval, revision, queue_item)
        if approval.authorized_user_id != request.discord_user_id:
            raise DocketError(
                code="unauthorized_approval_actor",
                message="The Discord actor is not authorized for this approval.",
            )
        now = utc_now()
        if now > _as_utc(approval.expires_at):
            approval.status = ApprovalStatus.EXPIRED.value
            action.status = ActionStatus.EXPIRED.value
            queue_item.status = QueueItemStatus.PENDING.value
            queue_item.version += 1
            for plan in self.session.scalars(
                select(CalendarReminderPlan).where(
                    CalendarReminderPlan.action_revision_id == revision.id,
                    CalendarReminderPlan.status.in_(("planned", "reconciliation_required")),
                )
            ):
                plan.status = "cancelled"
            self.session.add(
                AuditEvent(
                    event_type="approval.expired",
                    entity_type="approval",
                    entity_id=approval.id,
                    actor_type="plugin",
                    actor_id=request.discord_user_id,
                    request_id=request.request_id,
                    data={"action_revision_id": str(revision.id)},
                )
            )
            self.session.add(
                OutboxEvent(
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=queue_item.id,
                    deduplication_key=f"discord_projection:{queue_item.id}:expired:{approval.id}",
                    payload={
                        "queue_item_id": str(queue_item.id),
                        "action_id": str(action.id),
                        "approval_id": str(approval.id),
                        "status": "expired",
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
            raise DocketError(code="approval_expired", message="The approval has expired.")
        self._validate_binding(approval, revision, action, queue_item)
        approval.responded_at = request.responded_at
        approval.response_user_id = request.discord_user_id
        approval.response_guild_id = request.guild_id
        approval.response_channel_id = request.channel_id
        approval.response_parent_channel_id = request.parent_channel_id
        approval.response_projection_id = request.projection_id
        approval.response_message_id = request.message_id
        approval.discord_interaction_id = request.discord_interaction_id

        operation: Operation | None = None
        batch_all_no_op = False
        if request.decision == "reject":
            approval.status = ApprovalStatus.REJECTED.value
            action.status = ActionStatus.REJECTED.value
            queue_item.status = QueueItemStatus.COMPLETED.value
            queue_item.resolved_at = now
            queue_item.resolution_code = "approval_rejected"
            queue_item.version += 1
            for plan in self.session.scalars(
                select(CalendarReminderPlan).where(
                    CalendarReminderPlan.action_revision_id == revision.id,
                    CalendarReminderPlan.status.in_(("planned", "reconciliation_required")),
                )
            ):
                plan.status = "cancelled"
            event_type = "approval.rejected"
        else:
            idempotency_key = self._idempotency_key(revision)
            operation = self.session.scalar(
                select(Operation).where(Operation.idempotency_key == idempotency_key)
            )
            if operation is None:
                operation_id = uuid.uuid4()
                operation = Operation(
                    id=operation_id,
                    action_revision_id=revision.id,
                    approval_id=approval.id,
                    idempotency_key=idempotency_key,
                    operation_type=revision.action_type,
                    account_id=revision.account_id,
                    status=OperationStatus.PENDING.value,
                    provider_correlation=str(operation_id),
                    next_attempt_at=now,
                )
                self.session.add(operation)
                self.session.flush()
                if revision.action_type == "calendar_apply_term_schedule":
                    batch_items = list(revision.parameters["items"])
                    for manifest_item in revision.parameters["items"]:
                        item_key = str(manifest_item["item_key"])
                        parameters = dict(manifest_item["parameters"])
                        parameters["operation_type"] = manifest_item["operation_type"]
                        parameters_sha256 = sha256_json(parameters)
                        no_op = manifest_item["operation_type"] == "calendar_no_op"
                        self.session.add(
                            OperationItem(
                                operation_id=operation.id,
                                item_key=item_key,
                                item_type=str(manifest_item["operation_type"]),
                                idempotency_key=(
                                    f"calendar:schedule-item:{operation.id}:"
                                    f"{item_key}:{parameters_sha256}"
                                ),
                                parameters=parameters,
                                parameters_sha256=parameters_sha256,
                                status="succeeded" if no_op else "pending",
                                next_attempt_at=None if no_op else now,
                                result={"disposition": "no_op"} if no_op else None,
                            )
                        )
                    batch_all_no_op = all(
                        item["operation_type"] == "calendar_no_op" for item in batch_items
                    )
                    if batch_all_no_op:
                        operation.status = OperationStatus.SUCCEEDED.value
                        operation.next_attempt_at = None
                        operation.result = {
                            "item_count": len(batch_items),
                            "counts": {
                                "pending": 0,
                                "running": 0,
                                "succeeded": len(batch_items),
                                "failed": 0,
                                "reconciliation_required": 0,
                            },
                            "failures": [],
                        }
            approval.status = ApprovalStatus.CONSUMED.value
            approval.consumed_operation_id = operation.id
            action.status = (
                ActionStatus.SUCCEEDED.value if batch_all_no_op else ActionStatus.READY.value
            )
            queue_item.status = (
                QueueItemStatus.COMPLETED.value
                if batch_all_no_op
                else QueueItemStatus.EXECUTING.value
            )
            if batch_all_no_op:
                queue_item.resolved_at = now
                queue_item.resolution_code = "calendar_schedule_synchronized"
            queue_item.version += 1
            event_type = "approval.consumed"

        self.session.add(
            AuditEvent(
                event_type=event_type,
                entity_type="approval",
                entity_id=approval.id,
                actor_type="plugin",
                actor_id=request.discord_user_id,
                request_id=request.request_id,
                data={
                    "action_revision_id": str(revision.id),
                    "decision": request.decision,
                    "discord_interaction_id": request.discord_interaction_id,
                    "operation_id": str(operation.id) if operation else None,
                    "parameters_sha256": revision.parameters_sha256,
                    "preview_sha256": revision.preview_sha256,
                },
            )
        )
        self.session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=(
                    f"discord_projection:{queue_item.id}:approval:{approval.id}:{request.decision}"
                ),
                payload={
                    "queue_item_id": str(queue_item.id),
                    "action_id": str(action.id),
                    "approval_id": str(approval.id),
                    "decision": request.decision,
                    "operation_id": str(operation.id) if operation else None,
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        return {
            "ok": True,
            "decision": request.decision,
            "approval_id": str(approval.id),
            "approval_status": approval.status,
            "action_id": str(action.id),
            "action_status": action.status,
            "operation_id": str(operation.id) if operation else None,
            "operation_status": operation.status if operation else None,
        }
