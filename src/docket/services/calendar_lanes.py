from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docket.config import Settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import ActionStatus, CommandStatus, OperationStatus, QueueItemStatus
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
from docket.models import (
    Account,
    Action,
    ActionRevision,
    AuditEvent,
    CalendarLane,
    CommandRequest,
    Operation,
    QueueItem,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.schemas.calendar import (
    CalendarLaneMutationResult,
    CalendarLaneResult,
    ConfigureCalendarLaneInput,
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
        return [existing[name] for name in LANE_DEFINITIONS]

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
        assert lane is not None
        if lane.version != request.expected_version:
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
        parameters = {
            "lane_id": str(lane.id),
            "lane": lane.lane,
            "display_name": lane.display_name,
            "color_hex": lane.color_hex,
            "timezone": self.settings.timezone,
            "calendar_id": lane.calendar_id,
            "lane_version": lane.version,
        }
        preview = {
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
