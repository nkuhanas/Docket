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
    CalendarLane,
    CalendarLink,
    CalendarReminderPlan,
    CalendarSyncState,
    CanonicalEvent,
    DiscordDailyThread,
    DiscordProjection,
    Entity,
    Operation,
    OperationBundle,
    OperationItem,
    OutboxEvent,
    QueueItem,
    Record,
    SourceItem,
)
from docket.models.base import utc_now
from docket.policy import (
    BATCH_CALENDAR_ACTION_TYPES,
    GMAIL_MUTATION_ACTION_TYPES,
    get_action_definition,
)
from docket.security import (
    short_code_sha256,
    verify_projection_approval_token,
    verify_projection_decision_approval_token,
)
from docket.services.brief_projection import (
    morning_brief_contains_queue_item,
    morning_brief_projection_for_queue_item,
    projection_refresh_target,
)
from docket.services.calendar_lanes import CalendarLaneService
from docket.services.course_reconciliation import (
    CourseReconciliationService,
    course_reconciliation_dependency_sha256,
)
from docket.services.operation_materialization import operation_idempotency_key
from docket.services.operational_logs import enqueue_action_system_log
from docket.services.operations import OperationRunner


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _conflict_resolution(revision: ActionRevision) -> str | None:
        conflicts = revision.preview.get("conflicts")
        if not isinstance(conflicts, list) or not conflicts:
            return None
        resolution = revision.parameters.get("conflict_resolution")
        if resolution not in {"keep_both", "new_wins", "keep_existing"}:
            raise DocketError(
                code="conflict_resolution_required",
                message="Choose how the conflicting events should coexist before approving.",
            )
        if resolution == "new_wins" and any(
            not isinstance(conflict, dict) or conflict.get("can_cancel") is not True
            for conflict in conflicts
        ):
            raise DocketError(
                code="unsafe_conflict_resolution",
                message=(
                    "The proposed event cannot automatically replace an attendee-bearing "
                    "or externally organized event."
                ),
            )
        return str(resolution)

    def _retire_unadopted_canonical_event(
        self,
        revision: ActionRevision,
        *,
        reason: str,
    ) -> None:
        raw_id = revision.parameters.get("canonical_event_id")
        if raw_id is None:
            return
        try:
            event_id = uuid.UUID(str(raw_id))
        except ValueError:
            return
        event = self.session.get(CanonicalEvent, event_id)
        if event is not None and event.status == "proposed":
            event.status = "archived"
            event.version += 1
            self.session.add(
                AuditEvent(
                    event_type="canonical_event.formulation_retired",
                    entity_type="canonical_event",
                    entity_id=event.id,
                    actor_type="docket",
                    actor_id=None,
                    request_id=None,
                    data={"reason": reason, "action_revision_id": str(revision.id)},
                )
            )

    def _add_conflict_cancellation(
        self,
        *,
        bundle: OperationBundle,
        parent_revision: ActionRevision,
        conflict: dict[str, Any],
        actor_id: str,
        now: datetime,
        predecessor_operation_id: uuid.UUID,
    ) -> Operation:
        provider_event_id = str(conflict["provider_event_id"])
        provider_before = conflict.get("provider_before")
        if not isinstance(provider_before, dict):
            raise DocketError(
                code="approval_binding_mismatch",
                message="A conflict lost its immutable provider snapshot.",
            )
        link = self.session.scalar(
            select(CalendarLink).where(
                CalendarLink.account_id == parent_revision.account_id,
                CalendarLink.calendar_id == parent_revision.parameters["calendar_id"],
                CalendarLink.external_event_id == provider_event_id,
            )
        )
        queue_item = QueueItem(
            deduplication_key=f"conflict_bundle:{bundle.id}:{provider_event_id}",
            material_fingerprint=sha256_json(
                {
                    "bundle_id": str(bundle.id),
                    "provider_event_id": provider_event_id,
                    "provider_etag": conflict.get("provider_etag"),
                }
            ),
            category="calendar_change",
            title=f"Cancel conflicting event · {conflict.get('summary') or 'Calendar event'}",
            summary="Apply the selected proposed-event-wins resolution.",
            status=QueueItemStatus.EXECUTING.value,
            priority="normal",
            presentation="suppressed",
            received_at=now,
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            record_id=link.record_id if link is not None else None,
            action_type="calendar_cancel_event",
            status=ActionStatus.READY.value,
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        parameters: dict[str, Any] = {
            "calendar_id": parent_revision.parameters["calendar_id"],
            "logical_key": (
                link.logical_key if link is not None else f"provider:{provider_event_id}"
            ),
            "event": None,
            "reminder_plan": None,
            "reminder_plan_sha256": None,
            "priority": str(provider_before.get("priority") or "normal"),
            "priority_basis": str(provider_before.get("priority_basis") or "default"),
            "target_scope": "event",
            "external_event_id": provider_event_id,
            "provider_etag": conflict.get("provider_etag"),
            "provider_before": dict(provider_before),
            "reason": "Conflict resolution: proposed event wins",
            "conflict_resolution": "new_wins",
        }
        preview = {
            "action_type": "calendar_cancel_event",
            "target": {
                "account_id": str(parent_revision.account_id),
                "calendar_id": parent_revision.parameters["calendar_id"],
                "logical_key": parameters["logical_key"],
                "scope": "event",
            },
            "event": None,
            "before": dict(provider_before),
            "reminder_plan": None,
            "classification": {
                "recurrence_kind": str(provider_before.get("recurrence_kind") or "one_time"),
                "system_tags": list(provider_before.get("system_tags") or []),
                "operator_tags": list(provider_before.get("operator_tags") or []),
                "priority": parameters["priority"],
                "priority_basis": parameters["priority_basis"],
            },
            "conflicts": [],
            "reason": parameters["reason"],
            "conflict_resolution": "new_wins",
        }
        definition = get_action_definition("calendar_cancel_event")
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type="calendar_cancel_event",
            account_id=parent_revision.account_id,
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            preview=preview,
            preview_sha256=sha256_json(preview),
            risk_class=definition.risk_class.value,
            authority="explicit_user",
            target_versions={
                "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
                "calendar_snapshot": {
                    "provider_event_id": provider_event_id,
                    "provider_etag": conflict.get("provider_etag"),
                    "target_scope": "event",
                },
            },
            created_by_actor_type="plugin",
            created_by_actor_id=actor_id,
        )
        self.session.add(revision)
        self.session.flush()
        operation_id = uuid.uuid4()
        operation = Operation(
            id=operation_id,
            action_revision_id=revision.id,
            bundle_id=bundle.id,
            predecessor_operation_id=predecessor_operation_id,
            approval_id=bundle.approval_id,
            idempotency_key=f"calendar:conflict-bundle:{bundle.id}:cancel:{provider_event_id}",
            operation_type="calendar_cancel_event",
            account_id=parent_revision.account_id,
            status=OperationStatus.PENDING.value,
            provider_correlation=str(operation_id),
            next_attempt_at=now,
        )
        self.session.add(operation)
        return operation

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
    ) -> DiscordProjection | None:
        if request.short_code is not None:
            return None
        assert request.projection_id is not None
        assert request.approval_token is not None
        projection = self.session.scalar(
            select(DiscordProjection)
            .where(DiscordProjection.id == request.projection_id)
            .with_for_update()
        )
        aggregate_brief_binding = (
            projection is not None
            and projection.view_mode == "brief_review"
            and projection.view_action_revision_id == revision.id
            and morning_brief_contains_queue_item(
                self.session,
                brief_queue_item_id=projection.queue_item_id,
                child_queue_item_id=queue_item.id,
            )
        )
        if (
            projection is None
            or (projection.queue_item_id != queue_item.id and not aggregate_brief_binding)
            or projection.status != "delivered"
            or projection.message_id != request.message_id
            or (
                approval.control_projection_id != projection.id
                and not (
                    approval.status != ApprovalStatus.PENDING.value
                    and approval.response_projection_id == projection.id
                )
            )
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
        return projection

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
        response_projection: DiscordProjection | None,
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
        aggregate_brief_binding = (
            response_projection is not None
            and response_projection.queue_item_id != queue_item.id
            and morning_brief_contains_queue_item(
                self.session,
                brief_queue_item_id=response_projection.queue_item_id,
                child_queue_item_id=queue_item.id,
            )
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
        if projection is None and not aggregate_brief_binding:
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
        CalendarLaneService(self.session, get_settings()).require_active(
            account.id,
            calendar_id=str(revision.parameters.get("calendar_id") or ""),
        )
        queue_target = revision.target_versions.get("queue_item", {})
        lane_targets = revision.target_versions.get("calendar_lanes", [])
        if isinstance(lane_targets, list):
            for target in lane_targets:
                try:
                    lane_id = uuid.UUID(str(target.get("id")))
                except (AttributeError, ValueError) as exc:
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The action contains an invalid Calendar lane binding.",
                    ) from exc
                lane = self.session.get(CalendarLane, lane_id)
                if (
                    lane is None
                    or lane.account_id != account.id
                    or lane.version != target.get("version")
                    or lane.status != "active"
                ):
                    raise DocketError(
                        code="target_version_changed",
                        message="A Calendar lane changed after the proposal was created.",
                    )
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
        entity_targets = revision.target_versions.get("entity_registrations", [])
        if isinstance(entity_targets, list):
            for target in entity_targets:
                try:
                    entity_id = uuid.UUID(str(target.get("id")))
                except (AttributeError, ValueError) as exc:
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The action contains an invalid entity registration binding.",
                    ) from exc
                entity = self.session.get(Entity, entity_id)
                if (
                    entity is None
                    or entity.version != target.get("version")
                    or entity.status != target.get("status")
                ):
                    raise DocketError(
                        code="target_version_changed",
                        message=(
                            "A bundled entity registration changed after the proposal was created."
                        ),
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
            if (
                sync_state is None
                or sync_state.status != "current"
                or sync_state.last_success_at is None
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
            if revision.action_type in {
                "calendar_create_event",
                "calendar_update_event",
            }:
                from docket.schemas.calendar import StandaloneCalendarEventInput
                from docket.services.calendar_actions import CalendarActionService

                if revision.account_id is None:
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The Calendar formulation has no account binding.",
                    )
                event_data = revision.parameters.get("event")
                if not isinstance(event_data, dict):
                    raise DocketError(
                        code="approval_binding_mismatch",
                        message="The Calendar formulation has no complete event binding.",
                    )
                event = StandaloneCalendarEventInput.model_validate(
                    event_data,
                    context={"allow_explicit_priority": True},
                )
                current_conflicts = CalendarActionService(self.session)._conflicts(
                    account_id=revision.account_id,
                    calendar_id=str(revision.parameters["calendar_id"]),
                    event=event,
                    exclude_provider_event_id=revision.parameters.get("external_event_id"),
                )
                expected_conflict_fingerprint = calendar_target.get("conflict_fingerprint")
                if expected_conflict_fingerprint is None:
                    preview_conflicts = revision.preview.get("conflicts")
                    if not isinstance(preview_conflicts, list):
                        raise DocketError(
                            code="approval_binding_mismatch",
                            message="The immutable Calendar preview has no conflict binding.",
                        )
                    expected_conflict_fingerprint = sha256_json(preview_conflicts)
                if sha256_json(current_conflicts) != expected_conflict_fingerprint:
                    raise DocketError(
                        code="target_version_changed",
                        message=(
                            "Relevant Calendar conflicts changed after the proposal was created."
                        ),
                    )
            if course_scoped:
                assert account is not None and record is not None
                expected_dependency = course_reconciliation_dependency_sha256(revision.parameters)
                stored_dependency = calendar_target.get("course_dependency_sha256")
                if stored_dependency is not None and stored_dependency != expected_dependency:
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
                message=("The Gmail source changed after the approval preview was created."),
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
                message=("A newer Gmail message version was staged after this preview."),
            )

    def respond(self, request: ApprovalResponse) -> dict[str, Any]:
        self._validate_context(request)
        approval = self._resolve(request)
        revision, action, queue_item = self._load_bound_state(approval)
        response_projection = self._validate_projection_context(
            request, approval, revision, queue_item
        )
        visible_projection = response_projection or morning_brief_projection_for_queue_item(
            self.session,
            child_queue_item_id=queue_item.id,
        )
        refresh_target = projection_refresh_target(
            self.session,
            child_queue_item_id=queue_item.id,
            projection=visible_projection,
        )
        projection_target = refresh_target.payload()
        if approval.authorized_user_id != request.discord_user_id:
            raise DocketError(
                code="unauthorized_approval_actor",
                message="The Discord actor is not authorized for this approval.",
            )
        if visible_projection is not None and approval.status in {
            ApprovalStatus.CONSUMED.value,
            ApprovalStatus.REJECTED.value,
        }:
            recorded_decision = (
                "approve" if approval.status == ApprovalStatus.CONSUMED.value else "reject"
            )
            self.session.add(
                OutboxEvent(
                    event_type="discord.projection.refresh_requested",
                    aggregate_type="queue_item",
                    aggregate_id=refresh_target.queue_item_id,
                    deduplication_key=(
                        f"discord_projection:{refresh_target.queue_item_id}:repair:"
                        f"{visible_projection.id}:{request.discord_interaction_id}"
                    ),
                    payload={
                        "action_id": str(action.id),
                        "approval_id": str(approval.id),
                        "status": "already_recorded",
                        **projection_target,
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
            self.session.add(
                AuditEvent(
                    event_type="approval.projection_repair_requested",
                    entity_type="approval",
                    entity_id=approval.id,
                    actor_type="plugin",
                    actor_id=request.discord_user_id,
                    request_id=request.request_id,
                    data={
                        "projection_id": str(visible_projection.id),
                        "recorded_decision": recorded_decision,
                    },
                )
            )
            return {
                "ok": True,
                "decision": recorded_decision,
                "approval_id": str(approval.id),
                "approval_status": approval.status,
                "operation_id": (
                    str(approval.consumed_operation_id)
                    if approval.consumed_operation_id is not None
                    else None
                ),
                "already_recorded": True,
            }
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
                    aggregate_id=refresh_target.queue_item_id,
                    deduplication_key=(
                        f"discord_projection:{refresh_target.queue_item_id}:expired:{approval.id}"
                    ),
                    payload={
                        "action_id": str(action.id),
                        "approval_id": str(approval.id),
                        "status": "expired",
                        **projection_target,
                    },
                    status=OutboxStatus.PENDING.value,
                )
            )
            raise DocketError(code="approval_expired", message="The approval has expired.")
        self._validate_invariant_binding(
            approval,
            revision,
            action,
            queue_item,
            visible_projection,
        )
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
                                aggregate_id=refresh_target.queue_item_id,
                                deduplication_key=(
                                    f"discord_projection:{refresh_target.queue_item_id}:"
                                    f"refresh-required:{approval.id}"
                                ),
                                payload={
                                    "action_id": str(action.id),
                                    "approval_id": str(approval.id),
                                    "status": "refresh_required",
                                    **projection_target,
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

        conflict_resolution = (
            self._conflict_resolution(revision) if request.decision == "approve" else None
        )
        operation: Operation | None = None
        bundle: OperationBundle | None = None
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
            if revision.action_type in GMAIL_MUTATION_ACTION_TYPES and pending_sibling is not None:
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
            self._retire_unadopted_canonical_event(
                revision,
                reason="formulation_rejected",
            )
            event_type = "approval.rejected"
        else:
            if conflict_resolution == "keep_existing":
                bundle = OperationBundle(
                    action_revision_id=revision.id,
                    approval_id=approval.id,
                    resolution=conflict_resolution,
                    status="succeeded",
                    result={
                        "operation_count": 0,
                        "counts": {
                            "pending": 0,
                            "running": 0,
                            "succeeded": 0,
                            "failed": 0,
                            "reconciliation_required": 0,
                        },
                        "disposition": "kept_existing",
                    },
                )
                self.session.add(bundle)
                self._retire_unadopted_canonical_event(
                    revision,
                    reason="conflict_resolution_kept_existing",
                )
                batch_all_no_op = True
            else:
                if conflict_resolution is not None:
                    bundle = OperationBundle(
                        action_revision_id=revision.id,
                        approval_id=approval.id,
                        resolution=conflict_resolution,
                        status="pending",
                    )
                    self.session.add(bundle)
                    self.session.flush()
                idempotency_key = operation_idempotency_key(revision)
                operation = self.session.scalar(
                    select(Operation).where(Operation.idempotency_key == idempotency_key)
                )
                if operation is None:
                    operation_id = uuid.uuid4()
                    operation = Operation(
                        id=operation_id,
                        action_revision_id=revision.id,
                        bundle_id=bundle.id if bundle is not None else None,
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
                    cancelled_provider_ids: set[str] = set()
                    if conflict_resolution == "new_wins":
                        assert bundle is not None
                        conflicts = revision.preview.get("conflicts")
                        assert isinstance(conflicts, list)
                        for conflict in conflicts:
                            assert isinstance(conflict, dict)
                            provider_event_id = str(conflict["provider_event_id"])
                            if provider_event_id in cancelled_provider_ids:
                                continue
                            cancelled_provider_ids.add(provider_event_id)
                            self._add_conflict_cancellation(
                                bundle=bundle,
                                parent_revision=revision,
                                conflict=conflict,
                                actor_id=request.discord_user_id,
                                now=now,
                                predecessor_operation_id=operation.id,
                            )
                    if bundle is not None:
                        bundle.status = "running"
                        bundle.result = {
                            "operation_count": 1
                            + (
                                len(cancelled_provider_ids)
                                if conflict_resolution == "new_wins"
                                else 0
                            ),
                            "counts": {
                                "pending": 1
                                + (
                                    len(cancelled_provider_ids)
                                    if conflict_resolution == "new_wins"
                                    else 0
                                ),
                                "running": 0,
                                "succeeded": 0,
                                "failed": 0,
                                "reconciliation_required": 0,
                            },
                        }
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
                                    message=("The course changed before its archive transition."),
                                )
            approval.status = ApprovalStatus.CONSUMED.value
            approval.consumed_operation_id = operation.id if operation is not None else None
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
                    "calendar_conflict_kept_existing"
                    if conflict_resolution == "keep_existing"
                    else "calendar_course_dropped"
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
                    "operation_bundle_id": str(bundle.id) if bundle else None,
                    "conflict_resolution": conflict_resolution,
                    "parameters_sha256": revision.parameters_sha256,
                    "preview_sha256": revision.preview_sha256,
                },
            )
        )
        self.session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=refresh_target.queue_item_id,
                deduplication_key=(
                    f"discord_projection:{refresh_target.queue_item_id}:approval:"
                    f"{approval.id}:{request.decision}"
                ),
                payload={
                    "action_id": str(action.id),
                    "approval_id": str(approval.id),
                    "decision": request.decision,
                    "operation_id": str(operation.id) if operation else None,
                    "operation_bundle_id": str(bundle.id) if bundle else None,
                    **projection_target,
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
            result=(
                bundle.result
                if bundle is not None and operation is None
                else operation.result
                if operation is not None
                else None
            ),
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
            "operation_bundle_id": str(bundle.id) if bundle else None,
            "operation_bundle_status": bundle.status if bundle else None,
            "conflict_resolution": conflict_resolution,
        }
