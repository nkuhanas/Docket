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
    CalendarLink,
    CalendarReminderPlan,
    CalendarSyncState,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OperationItem,
    OutboxEvent,
    QueueItem,
    Record,
    SourceItem,
)
from docket.models.base import utc_now
from docket.policy import BATCH_CALENDAR_ACTION_TYPES, GMAIL_MUTATION_ACTION_TYPES
from docket.security import (
    short_code_sha256,
    verify_projection_approval_token,
    verify_projection_decision_approval_token,
)
from docket.services.course_reconciliation import (
    CourseReconciliationService,
    course_reconciliation_dependency_sha256,
)
from docket.services.operational_logs import enqueue_action_system_log
from docket.services.operations import OperationRunner


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
        if revision.action_type in BATCH_CALENDAR_ACTION_TYPES and not (
            approval.refresh_required_at is not None and request.decision == "reject"
        ):
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

    def _validate_invariant_binding(
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
        queue_target = revision.target_versions.get("queue_item", {})
        if str(queue_item.id) != queue_target.get("id"):
            raise DocketError(
                code="approval_binding_mismatch",
                message="The approval is not bound to this queue item.",
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

    def _validate_current_targets(
        self,
        revision: ActionRevision,
        queue_item: QueueItem,
    ) -> None:
        if revision.action_type in GMAIL_MUTATION_ACTION_TYPES:
            self._validate_current_gmail_target(revision, queue_item)
            return
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
        record: Record | None = None
        if record_target is not None:
            try:
                record_id = uuid.UUID(str(record_target.get("id")))
            except (AttributeError, ValueError) as exc:
                raise DocketError(
                    code="approval_binding_mismatch",
                    message="The action contains an invalid target record binding.",
                ) from exc
            record = self.session.get(Record, record_id)
            if (
                record is None
                or record.version != record_target.get("version")
                or (
                    record_target.get("status") is not None
                    and record.status != record_target.get("status")
                )
            ):
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
            course_scoped = revision.action_type in {
                "calendar_reconcile_course",
                "calendar_drop_course",
            }
            binds_complete_snapshot = revision.action_type not in {
                "calendar_cancel_event",
                "calendar_update_reminders",
                "calendar_reconcile_course",
                "calendar_drop_course",
            }
            if (
                sync_state is None
                or sync_state.status != "current"
                or sync_state.last_success_at is None
                or (
                    binds_complete_snapshot
                    and _as_utc(sync_state.last_success_at).isoformat()
                    != calendar_target.get("last_success_at")
                )
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
            if course_scoped:
                assert account is not None and record is not None
                expected_dependency = course_reconciliation_dependency_sha256(
                    revision.parameters
                )
                stored_dependency = calendar_target.get("course_dependency_sha256")
                if (
                    stored_dependency is not None
                    and stored_dependency != expected_dependency
                ):
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The immutable course dependency hash no longer matches.",
                    )
                try:
                    current_dependency = CourseReconciliationService(
                        self.session
                    ).current_approval_dependency_sha256(
                        revision_parameters=revision.parameters,
                        record=record,
                        account=account,
                        state=sync_state,
                    )
                except DocketError as exc:
                    if exc.code == "approval_binding_mismatch":
                        raise
                    raise DocketError(
                        code="target_version_changed",
                        message=(
                            "The course's Calendar targets or conflicts changed "
                            "after the approval preview was created."
                        ),
                    ) from exc
                if current_dependency != expected_dependency:
                    raise DocketError(
                        code="target_version_changed",
                        message=(
                            "The course's Calendar targets or conflicts changed "
                            "after the approval preview was created."
                        ),
                    )
        if (
            isinstance(calendar_target, dict)
            and calendar_target.get("provider_event_id") is not None
        ):
            if calendar_target.get("target_scope") == "series":
                link = self.session.scalar(
                    select(CalendarLink).where(
                        CalendarLink.account_id == revision.account_id,
                        CalendarLink.calendar_id == revision.parameters.get("calendar_id"),
                        CalendarLink.external_event_id == calendar_target["provider_event_id"],
                    )
                )
                instances = list(
                    self.session.scalars(
                        select(CalendarEventCache).where(
                            CalendarEventCache.account_id == revision.account_id,
                            CalendarEventCache.calendar_id
                            == revision.parameters.get("calendar_id"),
                            CalendarEventCache.recurring_event_id
                            == calendar_target["provider_event_id"],
                            CalendarEventCache.status != "cancelled",
                        )
                    )
                )
                changed = (
                    link is None
                    or link.recurrence_kind != "recurring"
                    or link.provider_etag != calendar_target.get("provider_etag")
                    or link.synced_snapshot.get("status") == "cancelled"
                    or not instances
                    or any(
                        event.has_attendees or event.organizer_is_self is False
                        for event in instances
                    )
                )
            else:
                event = self.session.scalar(
                    select(CalendarEventCache).where(
                        CalendarEventCache.account_id == revision.account_id,
                        CalendarEventCache.calendar_id == revision.parameters.get("calendar_id"),
                        CalendarEventCache.provider_event_id
                        == calendar_target["provider_event_id"],
                    )
                )
                changed = (
                    event is None
                    or event.status == "cancelled"
                    or event.provider_etag != calendar_target.get("provider_etag")
                    or event.has_attendees
                    or event.organizer_is_self is False
                )
            if changed:
                raise DocketError(
                    code="target_version_changed",
                    message="The Calendar event changed after the approval preview was created.",
                )
        if queue_item.version != queue_target.get("version"):
            raise DocketError(
                code="target_version_changed",
                message="The queue item changed after the approval preview was created.",
            )

    def _validate_current_gmail_target(
        self,
        revision: ActionRevision,
        queue_item: QueueItem,
    ) -> None:
        account = self.session.get(Account, revision.account_id)
        if (
            account is None
            or not account.enabled
            or account.provider != "google"
            or "gmail" not in account.capabilities
        ):
            raise DocketError(
                code="target_account_changed",
                message="The selected Gmail account is no longer enabled.",
            )
        target = revision.target_versions.get("source_item")
        if not isinstance(target, dict):
            raise DocketError(
                code="approval_binding_mismatch",
                message="The Gmail action has no immutable source binding.",
            )
        try:
            source_id = uuid.UUID(str(target.get("id")))
        except ValueError as exc:
            raise DocketError(
                code="approval_binding_mismatch",
                message="The Gmail action has an invalid source binding.",
            ) from exc
        source = self.session.get(SourceItem, source_id)
        classification = source.classification if source is not None else None
        if (
            source is None
            or source.account_id != revision.account_id
            or source.external_object_id != target.get("message_id")
            or source.source_version != target.get("source_version")
            or str(source.account_id) != target.get("account_id")
            or source.status != "classified"
            or not isinstance(classification, dict)
            or classification.get("queue_item_id") != str(queue_item.id)
            or revision.parameters.get("source_item_id") != str(source.id)
            or revision.parameters.get("message_id") != source.external_object_id
            or revision.parameters.get("source_version") != source.source_version
        ):
            raise DocketError(
                code="target_version_changed",
                message=(
                    "The Gmail source changed after the approval preview was created."
                ),
            )
        newer = self.session.scalar(
            select(SourceItem.id)
            .where(
                SourceItem.account_id == source.account_id,
                SourceItem.provider == "gmail",
                SourceItem.external_object_id == source.external_object_id,
                SourceItem.id != source.id,
                SourceItem.created_at > source.created_at,
            )
            .limit(1)
        )
        if newer is not None:
            raise DocketError(
                code="target_version_changed",
                message=(
                    "A newer Gmail message version was staged after this preview."
                ),
            )

    @staticmethod
    def _idempotency_key(revision: ActionRevision) -> str:
        parameters = revision.parameters
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
        if revision.action_type in {
            "calendar_reconcile_course",
            "calendar_drop_course",
        }:
            return (
                f"calendar:{parameters['mode']}-course:{revision.account_id}:"
                f"{parameters['record_id']}:{parameters['record_version']}:"
                f"{revision.parameters_sha256}"
            )
        if revision.action_type == "gmail_archive_message":
            return (
                f"gmail:archive:{revision.account_id}:"
                f"{parameters['message_id']}:{parameters['source_version']}"
            )
        if revision.action_type == "gmail_mark_read":
            return (
                f"gmail:mark_read:{revision.account_id}:"
                f"{parameters['message_id']}:{parameters['source_version']}"
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
        self._validate_invariant_binding(approval, revision, action, queue_item)
        if request.decision == "approve":
            settings = get_settings()
            if (
                revision.action_type in GMAIL_MUTATION_ACTION_TYPES
                and not settings.gmail_writes_enabled
            ):
                raise DocketError(
                    code="external_writes_disabled",
                    message="Gmail writes are disabled. The approval remains pending.",
                )
            if (
                revision.action_type not in GMAIL_MUTATION_ACTION_TYPES
                and settings.calendar_write_mode() == "disabled"
            ):
                raise DocketError(
                    code="external_writes_disabled",
                    message=(
                        "External Calendar writes are disabled. The approval remains pending."
                    ),
                )
            try:
                self._validate_current_targets(revision, queue_item)
            except DocketError as exc:
                if exc.code == "target_version_changed":
                    if approval.refresh_required_at is None:
                        approval.refresh_required_at = now
                        approval.refresh_reason_code = exc.code
                        self.session.add(
                            AuditEvent(
                                event_type="approval.refresh_required",
                                entity_type="approval",
                                entity_id=approval.id,
                                actor_type="plugin",
                                actor_id=request.discord_user_id,
                                request_id=request.request_id,
                                data={
                                    "action_revision_id": str(revision.id),
                                    "reason_code": exc.code,
                                },
                            )
                        )
                        self.session.add(
                            OutboxEvent(
                                event_type="discord.projection.refresh_requested",
                                aggregate_type="queue_item",
                                aggregate_id=queue_item.id,
                                deduplication_key=(
                                    f"discord_projection:{queue_item.id}:"
                                    f"refresh-required:{approval.id}"
                                ),
                                payload={
                                    "queue_item_id": str(queue_item.id),
                                    "action_id": str(action.id),
                                    "approval_id": str(approval.id),
                                    "status": "refresh_required",
                                },
                                status=OutboxStatus.PENDING.value,
                            )
                        )
                    raise
                raise
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
            pending_sibling = self.session.scalar(
                select(Action.id).where(
                    Action.queue_item_id == queue_item.id,
                    Action.id != action.id,
                    Action.status == ActionStatus.APPROVAL_PENDING.value,
                )
            )
            if (
                revision.action_type in GMAIL_MUTATION_ACTION_TYPES
                and pending_sibling is not None
            ):
                queue_item.status = QueueItemStatus.AWAITING_APPROVAL.value
                queue_item.resolved_at = None
                queue_item.resolution_code = None
            else:
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
                if revision.action_type in BATCH_CALENDAR_ACTION_TYPES:
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
                                    f"calendar:batch-item:{operation.id}:"
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
                        if not OperationRunner._apply_course_transition(
                            self.session,
                            operation,
                            revision,
                            action,
                            queue_item,
                        ):
                            raise DocketError(
                                code="course_archive_transition_conflict",
                                message="The course changed before its archive transition.",
                            )
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
                queue_item.resolution_code = (
                    "calendar_course_dropped"
                    if revision.action_type == "calendar_drop_course"
                    else "calendar_course_synchronized"
                    if revision.action_type == "calendar_reconcile_course"
                    else "calendar_schedule_synchronized"
                )
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
        enqueue_action_system_log(
            self.session,
            action=action,
            revision=revision,
            state=(
                "rejected"
                if request.decision == "reject"
                else "succeeded"
                if batch_all_no_op
                else "queued"
            ),
            occurred_at=now,
            result=operation.result if operation is not None else None,
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
