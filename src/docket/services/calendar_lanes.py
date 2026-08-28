from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import (
    ActionStatus,
    CommandStatus,
    OperationStatus,
    QueueItemStatus,
)
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
from docket.models import (
    Account,
    Action,
    ActionRevision,
    AuditEvent,
    CalendarEventCache,
    CalendarLane,
    CalendarLink,
    CommandRequest,
    Operation,
    OperationItem,
    ProviderEventBinding,
    QueueItem,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.schemas.calendar import (
    CalendarLaneMutationResult,
    CalendarLaneResult,
    ConfigureCalendarLaneInput,
    DeleteCalendarLaneInput,
    MigrateCalendarLaneEventsInput,
)
from docket.services.operation_materialization import operation_idempotency_key
from docket.services.operational_logs import enqueue_action_system_log
from docket.services.source_context import validate_configured_discord_source


@dataclass(frozen=True, slots=True)
class LaneDefinition:
    display_name: str
    color_hex: str


LANE_DEFINITIONS: Final[dict[str, LaneDefinition]] = {
    "academic": LaneDefinition("Docket · Academic", "#3F51B5"),
    "work": LaneDefinition("Docket · Work", "#D50000"),
    "organizations": LaneDefinition("Docket · Organizations", "#0B8043"),
    "personal": LaneDefinition("Docket · Personal", "#8E24AA"),
    "unsorted": LaneDefinition("Docket", "#F6BF26"),
}


def lane_result(lane: CalendarLane) -> CalendarLaneResult:
    return CalendarLaneResult(
        lane_id=lane.id,
        lane=lane.lane,
        display_name=lane.display_name,
        color_hex=lane.color_hex,
        status=lane.status,
        account_id=lane.account_id,
        calendar_id=lane.calendar_id,
        version=lane.version,
    )


class CalendarLaneService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def ensure_for_account(self, account: Account) -> list[CalendarLane]:
        existing = {
            lane.lane: lane
            for lane in self.session.scalars(
                select(CalendarLane).where(CalendarLane.account_id == account.id)
            )
        }
        for lane_name, definition in LANE_DEFINITIONS.items():
            if lane_name in existing:
                continue
            lane = CalendarLane(
                account_id=account.id,
                lane=lane_name,
                display_name=definition.display_name,
                color_hex=definition.color_hex,
                calendar_id=(self.settings.google_calendar_id if lane_name == "unsorted" else None),
                status="active" if lane_name == "unsorted" else "unprovisioned",
            )
            try:
                with self.session.begin_nested():
                    self.session.add(lane)
                    self.session.flush()
            except IntegrityError:
                concurrent_lane = self.session.scalar(
                    select(CalendarLane).where(
                        CalendarLane.account_id == account.id,
                        CalendarLane.lane == lane_name,
                    )
                )
                if concurrent_lane is None:
                    raise
                lane = concurrent_lane
            existing[lane_name] = lane
        defaults = [existing[name] for name in LANE_DEFINITIONS]
        custom = sorted(
            (lane for name, lane in existing.items() if name not in LANE_DEFINITIONS),
            key=lambda lane: lane.lane,
        )
        return [*defaults, *custom]

    def list_lanes(self, account_id: uuid.UUID) -> list[CalendarLaneResult]:
        account = self._account(account_id)
        return [lane_result(lane) for lane in self.ensure_for_account(account)]

    def calendar_ids(self, account_id: uuid.UUID) -> list[str]:
        account = self._account(account_id)
        return [
            lane.calendar_id
            for lane in self.ensure_for_account(account)
            if lane.status == "active" and lane.calendar_id is not None
        ]

    def require_active(
        self,
        account_id: uuid.UUID,
        *,
        lane_name: str | None = None,
        calendar_id: str | None = None,
    ) -> CalendarLane:
        account = self._account(account_id)
        lanes = self.ensure_for_account(account)
        matches = [
            lane
            for lane in lanes
            if (lane_name is None or lane.lane == lane_name)
            and (calendar_id is None or lane.calendar_id == calendar_id)
        ]
        if len(matches) != 1 or matches[0].status != "active" or not matches[0].calendar_id:
            raise DocketError(
                code="calendar_lane_unavailable",
                message="The selected Calendar lane is not provisioned and active.",
                details={"lane": lane_name, "calendar_id": calendar_id},
            )
        return matches[0]

    def configure(
        self,
        request: ConfigureCalendarLaneInput,
    ) -> CalendarLaneMutationResult:
        validate_configured_discord_source(self.session, request.source, request.actor_id)
        payload_sha256 = sha256_json(request.model_dump(mode="json"))
        existing_command = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing_command is not None:
            if (
                existing_command.operation_name != "docket_configure_calendar_lane"
                or existing_command.input_sha256 != payload_sha256
            ):
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing_command.operation_name,
                    attempted_operation="docket_configure_calendar_lane",
                )
            if (
                existing_command.status == CommandStatus.SUCCEEDED.value
                and existing_command.result is not None
            ):
                replay = CalendarLaneMutationResult.model_validate(existing_command.result)
                return replay.model_copy(update={"disposition": "replayed_request"})
            raise DocketError(
                code="request_in_progress",
                message="The Calendar lane request has not completed successfully.",
            )

        command = CommandRequest(
            request_key=request.request_key,
            operation_name="docket_configure_calendar_lane",
            input_sha256=payload_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        account = self._account(request.account_id)
        self.ensure_for_account(account)
        lane = self.session.scalar(
            select(CalendarLane)
            .where(
                CalendarLane.account_id == account.id,
                CalendarLane.lane == request.lane,
            )
            .with_for_update()
        )
        if lane is None:
            if request.expected_version is not None:
                raise DocketError(
                    code="calendar_lane_not_found",
                    message="The Calendar lane does not exist at the supplied version.",
                    details={"lane": request.lane},
                )
            lane = CalendarLane(
                account_id=account.id,
                lane=request.lane,
                display_name=request.display_name,
                color_hex=request.color_hex,
                calendar_id=None,
                status="unprovisioned",
            )
            self.session.add(lane)
            self.session.flush()
        elif request.expected_version is None:
            raise DocketError(
                code="calendar_lane_already_exists",
                message="The Calendar lane already exists; read it and supply its version.",
                details={"lane": lane.lane, "version": lane.version},
            )
        if request.expected_version is not None and lane.version != request.expected_version:
            raise VersionConflict(str(lane.id), request.expected_version, lane.version)
        if lane.status == "provisioning":
            raise DocketError(
                code="calendar_lane_configuration_in_progress",
                message="This Calendar lane already has a configuration operation in progress.",
                details={"lane": lane.lane, "version": lane.version},
            )

        before = lane_result(lane).model_dump(mode="json")
        lane.display_name = request.display_name
        lane.color_hex = request.color_hex
        lane.status = "provisioning"
        lane.last_error_code = None
        lane.version += 1
        self.session.flush()
        parameters: dict[str, object] = {
            "lane_id": str(lane.id),
            "lane": lane.lane,
            "display_name": lane.display_name,
            "color_hex": lane.color_hex,
            "timezone": self.settings.timezone,
            "calendar_id": lane.calendar_id,
            "lane_version": lane.version,
        }
        preview: dict[str, object] = {
            "action_type": "calendar_configure_lane",
            "lane": {
                "lane": lane.lane,
                "display_name": lane.display_name,
                "color_hex": lane.color_hex,
                "calendar_id": lane.calendar_id,
            },
            "before": before,
        }
        queue_item = QueueItem(
            deduplication_key=f"calendar_lane:{request.request_key}",
            material_fingerprint=sha256_json(parameters),
            category="calendar_configuration",
            title=f"Configure Calendar lane · {lane.display_name}",
            summary="Apply the explicitly authorized Calendar lane configuration.",
            status=QueueItemStatus.EXECUTING.value,
            priority="normal",
            presentation="suppressed",
            received_at=utc_now(),
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            action_type="calendar_configure_lane",
            status=ActionStatus.READY.value,
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        definition = get_action_definition("calendar_configure_lane")
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type="calendar_configure_lane",
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            preview=preview,
            preview_sha256=sha256_json(preview),
            risk_class=definition.risk_class.value,
            authority="explicit_user",
            target_versions={
                "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
                "calendar_lane": {"id": str(lane.id), "version": lane.version},
            },
            created_by_actor_type=request.actor_type,
            created_by_actor_id=request.actor_id,
        )
        self.session.add(revision)
        self.session.flush()
        operation_id = uuid.uuid4()
        operation = Operation(
            id=operation_id,
            action_revision_id=revision.id,
            approval_id=None,
            idempotency_key=operation_idempotency_key(revision),
            operation_type="calendar_configure_lane",
            account_id=account.id,
            status=OperationStatus.PENDING.value,
            provider_correlation=str(operation_id),
            next_attempt_at=utc_now(),
        )
        self.session.add(operation)
        result = CalendarLaneMutationResult(
            request_id=command.id,
            disposition="execution_queued",
            lane=lane_result(lane),
            queue_item_id=queue_item.id,
            action_id=action.id,
            action_revision_id=revision.id,
            operation_id=operation.id,
            operation_status="pending",
        )
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()
        self.session.add(
            AuditEvent(
                event_type="action.execution_queued",
                entity_type="action",
                entity_id=action.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "before": before,
                    "calendar_lane_id": str(lane.id),
                    "operation_id": str(operation.id),
                    "parameters_sha256": revision.parameters_sha256,
                },
            )
        )
        enqueue_action_system_log(self.session, action=action, revision=revision, state="queued")
        return result

    def migrate_events(self, request: MigrateCalendarLaneEventsInput) -> dict[str, object]:
        operation_name = "docket_migrate_calendar_events"
        command, replay = self._begin_command(request, operation_name)
        if replay is not None:
            return replay
        account = self._account(request.account_id)
        source_lane = self.require_active(account.id, lane_name=request.source_lane)
        destination_lane = self.require_active(account.id, lane_name=request.destination_lane)
        if source_lane.version != request.expected_source_version:
            raise VersionConflict(
                str(source_lane.id), request.expected_source_version, source_lane.version
            )
        if destination_lane.version != request.expected_destination_version:
            raise VersionConflict(
                str(destination_lane.id),
                request.expected_destination_version,
                destination_lane.version,
            )
        assert source_lane.calendar_id is not None
        assert destination_lane.calendar_id is not None

        items: list[dict[str, object]] = []
        previews: list[dict[str, object]] = []
        selected_provider_ids: set[str] = set()
        for selection in request.events:
            cache = self.session.scalar(
                select(CalendarEventCache).where(
                    CalendarEventCache.account_id == account.id,
                    CalendarEventCache.calendar_id == source_lane.calendar_id,
                    CalendarEventCache.provider_event_id == selection.provider_event_id,
                )
            )
            external_event_id = selection.provider_event_id
            if selection.scope == "series" and cache is not None and cache.recurring_event_id:
                external_event_id = cache.recurring_event_id
            elif selection.scope == "event" and cache is not None and cache.recurring_event_id:
                raise DocketError(
                    code="calendar_lane_series_scope_required",
                    message="A recurring occurrence must be moved with its complete series.",
                    details={
                        "provider_event_id": selection.provider_event_id,
                        "recurring_event_id": cache.recurring_event_id,
                    },
                )
            if external_event_id in selected_provider_ids:
                continue
            selected_provider_ids.add(external_event_id)
            link = self.session.scalar(
                select(CalendarLink).where(
                    CalendarLink.account_id == account.id,
                    CalendarLink.calendar_id == source_lane.calendar_id,
                    CalendarLink.external_event_id == external_event_id,
                )
            )
            if cache is None and selection.scope == "series":
                cache = self.session.scalar(
                    select(CalendarEventCache).where(
                        CalendarEventCache.account_id == account.id,
                        CalendarEventCache.calendar_id == source_lane.calendar_id,
                        CalendarEventCache.recurring_event_id == external_event_id,
                    )
                )
            if link is None and cache is None:
                raise DocketError(
                    code="calendar_event_not_found",
                    message=(
                        "The selected event is not present in the current source lane snapshot."
                    ),
                    details={"provider_event_id": selection.provider_event_id},
                )
            if link is not None and link.synced_snapshot.get("status") == "cancelled":
                raise DocketError(
                    code="calendar_event_cancelled",
                    message="A cancelled event cannot be moved to another lane.",
                    details={"provider_event_id": external_event_id},
                )
            event_type = (
                cache.event_type
                if cache is not None
                else str(link.synced_snapshot.get("event_type") or "default")
                if link is not None
                else "default"
            )
            if event_type != "default":
                raise DocketError(
                    code="calendar_event_type_not_movable",
                    message=(
                        "Google Calendar does not allow this event type to move between calendars."
                    ),
                    details={
                        "provider_event_id": external_event_id,
                        "event_type": event_type,
                    },
                )
            if cache is not None and cache.organizer_is_self is False:
                raise DocketError(
                    code="calendar_event_not_owned",
                    message="Only events organized by this Google account can move between lanes.",
                    details={"provider_event_id": external_event_id},
                )
            destination_link = self.session.scalar(
                select(CalendarLink.id).where(
                    CalendarLink.account_id == account.id,
                    CalendarLink.calendar_id == destination_lane.calendar_id,
                    or_(
                        CalendarLink.external_event_id == external_event_id,
                        *(
                            [CalendarLink.logical_key == link.logical_key]
                            if link is not None
                            else []
                        ),
                    ),
                )
            )
            destination_binding = self.session.scalar(
                select(ProviderEventBinding.id).where(
                    ProviderEventBinding.account_id == account.id,
                    ProviderEventBinding.calendar_id == destination_lane.calendar_id,
                    or_(
                        ProviderEventBinding.provider_event_id == external_event_id,
                        *(
                            [ProviderEventBinding.canonical_event_id == link.canonical_event_id]
                            if link is not None and link.canonical_event_id is not None
                            else []
                        ),
                    ),
                )
            )
            destination_cache = self.session.scalar(
                select(CalendarEventCache.id).where(
                    CalendarEventCache.account_id == account.id,
                    CalendarEventCache.calendar_id == destination_lane.calendar_id,
                    or_(
                        CalendarEventCache.provider_event_id == external_event_id,
                        CalendarEventCache.recurring_event_id == external_event_id,
                    ),
                )
            )
            if (
                destination_link is not None
                or destination_binding is not None
                or destination_cache is not None
            ):
                raise DocketError(
                    code="calendar_lane_destination_conflict",
                    message=(
                        "Docket already has this provider identity bound in the destination lane."
                    ),
                    details={"provider_event_id": external_event_id},
                )
            snapshot = (
                self._cache_preview(cache)
                if cache is not None
                else dict(link.synced_snapshot) if link is not None else {}
            )
            title = str(snapshot.get("summary") or "Untitled event")
            item_parameters = {
                "calendar_id": destination_lane.calendar_id,
                "source_calendar_id": source_lane.calendar_id,
                "destination_calendar_id": destination_lane.calendar_id,
                "source_lane": source_lane.lane,
                "destination_lane": destination_lane.lane,
                "external_event_id": external_event_id,
                "provider_before": snapshot,
                "scope": selection.scope,
                "logical_key": link.logical_key if link is not None else None,
                "canonical_event_id": (
                    str(link.canonical_event_id)
                    if link is not None and link.canonical_event_id is not None
                    else None
                ),
                "operation_type": "calendar_move_event",
            }
            item_key = f"move:{external_event_id}"
            items.append(
                {
                    "item_key": item_key,
                    "operation_type": "calendar_move_event",
                    "parameters": item_parameters,
                }
            )
            previews.append(
                {
                    "item_key": item_key,
                    "effect": "move",
                    "title": title,
                    "before": snapshot,
                    "source_lane": source_lane.lane,
                    "destination_lane": destination_lane.lane,
                    "scope": selection.scope,
                    "has_attendees": bool(cache.has_attendees) if cache is not None else False,
                    "classification": {
                        "recurrence_kind": (
                            link.recurrence_kind
                            if link is not None
                            else cache.recurrence_kind if cache is not None else "one_time"
                        )
                    },
                }
            )
        parameters: dict[str, object] = {
            "mode": "move",
            "calendar_id": destination_lane.calendar_id,
            "source_lane": source_lane.lane,
            "destination_lane": destination_lane.lane,
            "source_calendar_id": source_lane.calendar_id,
            "destination_calendar_id": destination_lane.calendar_id,
            "reason": request.reason,
            "items": items,
        }
        preview: dict[str, object] = {
            "action_type": "calendar_move_events",
            "source_lane": {
                "lane": source_lane.lane,
                "display_name": source_lane.display_name,
            },
            "destination_lane": {
                "lane": destination_lane.lane,
                "display_name": destination_lane.display_name,
            },
            "item_count": len(items),
            "counts": {"move": len(items)},
            "items": previews,
            "reason": request.reason,
        }
        return self._queue_explicit_execution(
            command=command,
            request=request,
            account=account,
            action_type="calendar_move_events",
            category="calendar_lane_migration",
            title=(
                f"Move {len(items)} event{'s' if len(items) != 1 else ''} · "
                f"{source_lane.display_name} → {destination_lane.display_name}"
            ),
            summary=request.reason,
            parameters=parameters,
            preview=preview,
            target_versions={
                "calendar_lanes": [
                    {"id": str(source_lane.id), "version": source_lane.version},
                    {"id": str(destination_lane.id), "version": destination_lane.version},
                ]
            },
        )

    def delete_lane(self, request: DeleteCalendarLaneInput) -> dict[str, object]:
        operation_name = "docket_delete_calendar_lane"
        command, replay = self._begin_command(request, operation_name)
        if replay is not None:
            return replay
        account = self._account(request.account_id)
        lane = self.require_active(account.id, lane_name=request.lane)
        if lane.version != request.expected_version:
            raise VersionConflict(str(lane.id), request.expected_version, lane.version)
        assert lane.calendar_id is not None
        active_links = [
            link
            for link in self.session.scalars(
                select(CalendarLink).where(
                    CalendarLink.account_id == account.id,
                    CalendarLink.calendar_id == lane.calendar_id,
                )
            )
            if link.synced_snapshot.get("status") != "cancelled"
        ]
        active_bindings = list(
            self.session.scalars(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.account_id == account.id,
                    ProviderEventBinding.calendar_id == lane.calendar_id,
                    ProviderEventBinding.status == "active",
                )
            )
        )
        active_cache_ids = set(
            self.session.scalars(
                select(CalendarEventCache.provider_event_id).where(
                    CalendarEventCache.account_id == account.id,
                    CalendarEventCache.calendar_id == lane.calendar_id,
                    CalendarEventCache.status != "cancelled",
                )
            )
        )
        known_active_ids = {
            *(link.external_event_id for link in active_links),
            *(binding.provider_event_id for binding in active_bindings),
            *active_cache_ids,
        }
        if known_active_ids:
            raise DocketError(
                code="calendar_lane_not_empty",
                message=(
                    "Move or cancel the lane's events before proposing deletion. "
                    "The provider will also verify that the calendar is empty."
                ),
                details={
                    "lane": lane.lane,
                    "known_active_event_count": len(known_active_ids),
                },
            )
        parameters: dict[str, object] = {
            "calendar_id": lane.calendar_id,
            "lane_id": str(lane.id),
            "lane": lane.lane,
            "display_name": lane.display_name,
            "color_hex": lane.color_hex,
            "timezone": self.settings.timezone,
            "lane_version": lane.version,
            "reason": request.reason,
        }
        preview: dict[str, object] = {
            "action_type": "calendar_delete_lane",
            "lane": {
                "lane": lane.lane,
                "display_name": lane.display_name,
                "color_hex": lane.color_hex,
            },
            "effect": "Permanently delete this empty Google Calendar lane.",
            "reason": request.reason,
        }
        return self._queue_explicit_execution(
            command=command,
            request=request,
            account=account,
            action_type="calendar_delete_lane",
            category="calendar_configuration",
            title=f"Delete Calendar lane · {lane.display_name}",
            summary=request.reason,
            parameters=parameters,
            preview=preview,
            target_versions={"calendar_lanes": [{"id": str(lane.id), "version": lane.version}]},
        )

    def _begin_command(
        self,
        request: MigrateCalendarLaneEventsInput | DeleteCalendarLaneInput,
        operation_name: str,
    ) -> tuple[CommandRequest, dict[str, object] | None]:
        validate_configured_discord_source(self.session, request.source, request.actor_id)
        payload_sha256 = sha256_json(request.model_dump(mode="json"))
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing is not None:
            if existing.operation_name != operation_name or existing.input_sha256 != payload_sha256:
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation=operation_name,
                )
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                return existing, {**existing.result, "disposition": "replayed_request"}
            raise DocketError(
                code="request_in_progress",
                message="The request is still in progress.",
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name=operation_name,
            input_sha256=payload_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    def _queue_explicit_execution(
        self,
        *,
        command: CommandRequest,
        request: MigrateCalendarLaneEventsInput | DeleteCalendarLaneInput,
        account: Account,
        action_type: str,
        category: str,
        title: str,
        summary: str,
        parameters: dict[str, object],
        preview: dict[str, object],
        target_versions: dict[str, object],
    ) -> dict[str, object]:
        now = utc_now()
        queue_item = QueueItem(
            deduplication_key=f"manual_action:{request.request_key}",
            material_fingerprint=sha256_json(parameters),
            category=category,
            title=title[:512],
            summary=summary[:2000],
            status=QueueItemStatus.EXECUTING.value,
            priority="normal",
            presentation="suppressed",
            received_at=now,
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            action_type=action_type,
            status=ActionStatus.READY.value,
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        target_versions = {
            "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
            **target_versions,
        }
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type=action_type,
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=sha256_json(parameters),
            preview=preview,
            preview_sha256=sha256_json(preview),
            risk_class=get_action_definition(action_type).risk_class.value,
            authority="explicit_user",
            target_versions=target_versions,
            created_by_actor_type=request.actor_type,
            created_by_actor_id=request.actor_id,
        )
        self.session.add(revision)
        self.session.flush()
        operation_id = uuid.uuid4()
        operation = Operation(
            id=operation_id,
            action_revision_id=revision.id,
            approval_id=None,
            idempotency_key=operation_idempotency_key(revision),
            operation_type=revision.action_type,
            account_id=account.id,
            status=OperationStatus.PENDING.value,
            provider_correlation=str(operation_id),
            next_attempt_at=now,
        )
        self.session.add(operation)
        self.session.flush()
        manifest = parameters.get("items")
        if isinstance(manifest, list):
            for raw_item in manifest:
                if not isinstance(raw_item, dict):
                    raise DocketError(
                        code="invalid_action_state",
                        message="The Calendar lane action contains an invalid operation item.",
                    )
                item_key = str(raw_item["item_key"])
                item_type = str(raw_item["operation_type"])
                item_parameters = dict(raw_item["parameters"])
                item_parameters["operation_type"] = item_type
                item_parameters_sha256 = sha256_json(item_parameters)
                self.session.add(
                    OperationItem(
                        operation_id=operation.id,
                        item_key=item_key,
                        item_type=item_type,
                        idempotency_key=(
                            f"calendar:batch-item:{operation.id}:"
                            f"{item_key}:{item_parameters_sha256}"
                        ),
                        parameters=item_parameters,
                        parameters_sha256=item_parameters_sha256,
                        status="pending",
                        next_attempt_at=now,
                    )
                )
        self.session.add(
            AuditEvent(
                event_type="action.execution_queued",
                entity_type="action",
                entity_id=action.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "action_type": action_type,
                    "authority": "explicit_user",
                    "revision": 1,
                    "operation_id": str(operation.id),
                    "risk_class": revision.risk_class,
                    "parameters_sha256": revision.parameters_sha256,
                    "preview_sha256": revision.preview_sha256,
                    "target_versions": target_versions,
                },
            )
        )
        enqueue_action_system_log(
            self.session,
            action=action,
            revision=revision,
            state="queued",
        )
        result: dict[str, object] = {
            "request_id": str(command.id),
            "disposition": "execution_queued",
            "queue_item_id": str(queue_item.id),
            "action_id": str(action.id),
            "action_revision_id": str(revision.id),
            "operation_id": str(operation.id),
            "operation_status": "pending",
            "preview": preview,
        }
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = now
        return result

    @staticmethod
    def _cache_preview(cache: CalendarEventCache | None) -> dict[str, object]:
        if cache is None:
            return {}
        return {
            "summary": cache.summary,
            "event_type": cache.event_type,
            "location": cache.location,
            "is_all_day": cache.is_all_day,
            "start_at": cache.start_at.isoformat() if cache.start_at is not None else None,
            "end_at": cache.end_at.isoformat() if cache.end_at is not None else None,
            "start_date": cache.start_date.isoformat() if cache.start_date is not None else None,
            "end_date": cache.end_date.isoformat() if cache.end_date is not None else None,
            "timezone": cache.timezone,
            "has_attendees": cache.has_attendees,
            "organizer_is_self": cache.organizer_is_self,
            "recurrence_kind": cache.recurrence_kind,
            "status": cache.status,
        }

    def _account(self, account_id: uuid.UUID) -> Account:
        account = self.session.get(Account, account_id)
        if (
            account is None
            or account.provider != "google"
            or not account.enabled
        ):
            raise DocketError(
                code="invalid_account",
                message="Calendar lanes require an enabled Google Calendar account.",
                details={"account_id": str(account_id)},
            )
        return account
