import hmac
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import (
    ActionStatus,
    ApprovalStatus,
    CommandStatus,
    OutboxStatus,
    QueueItemStatus,
    RecordStatus,
)
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarLink,
    CommandRequest,
    Operation,
    OutboxEvent,
    QueueItem,
    Record,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.schemas.actions import ProposalResult, ProposeActionInput
from docket.schemas.records import CourseData, CourseMeeting
from docket.security import issue_approval_token, issue_short_code, short_code_sha256
from docket.services.source_context import validate_configured_discord_source

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _first_occurrence(start: date, end: date, days: list[str]) -> date:
    allowed = {_WEEKDAYS[day] for day in days}
    candidate = start
    while candidate <= end:
        if candidate.weekday() in allowed:
            return candidate
        candidate += timedelta(days=1)
    raise DocketError(
        code="action_unavailable",
        message="The meeting date range contains no selected weekdays.",
    )


def _complete_meeting(meeting_id: str, meeting: CourseMeeting) -> dict[str, Any]:
    required = ("start_time", "end_time", "start_date", "end_date", "timezone")
    missing = [name for name in required if getattr(meeting, name) is None]
    if missing:
        raise DocketError(
            code="action_unavailable",
            message="Calendar creation requires a complete meeting schedule.",
            details={"meeting_id": meeting_id, "missing_fields": missing},
        )
    schedule = meeting.model_dump(mode="json")
    start_date = meeting.start_date
    end_date = meeting.end_date
    assert start_date is not None and end_date is not None
    schedule["first_occurrence_date"] = _first_occurrence(
        start_date, end_date, list(meeting.days)
    ).isoformat()
    return schedule


def _replayed_proposal(result: dict[str, Any]) -> ProposalResult:
    replayed = dict(result)
    replayed["disposition"] = "replayed_request"
    return ProposalResult.model_validate(replayed)


class ActionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _start_command(
        self, request: ProposeActionInput
    ) -> tuple[CommandRequest, ProposalResult | None]:
        payload = request.model_dump(mode="json")
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing is not None:
            if (
                existing.operation_name != "docket_propose_action"
                or existing.input_sha256 != input_sha256
            ):
                raise IdempotencyConflict(request.request_key)
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                return existing, _replayed_proposal(existing.result)
            raise DocketError(
                code="request_in_progress",
                message="The request exists but has not completed successfully.",
                details={"request_key": request.request_key, "status": existing.status},
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name="docket_propose_action",
            input_sha256=input_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    @staticmethod
    def _finish_command(command: CommandRequest, result: ProposalResult) -> None:
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()

    def _load_target(
        self, request: ProposeActionInput
    ) -> tuple[Record, CourseData, Account, dict[str, Any], CalendarLink | None]:
        settings = get_settings()
        definition = get_action_definition(request.action_type)
        if definition.parameter_schema is None:
            raise DocketError(
                code="action_unavailable",
                message="This action does not yet have a proposal contract.",
            )
        definition.parameter_schema.model_validate(request.parameters.model_dump(mode="json"))

        record = self.session.get(Record, request.record_id)
        if (
            record is None
            or record.record_type != "course"
            or record.status != RecordStatus.ACTIVE.value
        ):
            raise DocketError(
                code="invalid_action_target",
                message="Calendar meeting actions require an active course record.",
                details={"record_id": str(request.record_id)},
            )
        if record.version != request.expected_record_version:
            raise VersionConflict(str(record.id), request.expected_record_version, record.version)
        course = CourseData.model_validate(record.data)
        meeting = course.meetings.get(request.parameters.meeting_id)
        if meeting is None:
            raise DocketError(
                code="meeting_not_found",
                message="The requested stable meeting ID does not exist on the course.",
                details={"meeting_id": request.parameters.meeting_id},
            )
        schedule = _complete_meeting(request.parameters.meeting_id, meeting)

        account = self.session.get(Account, request.account_id)
        if (
            account is None
            or account.provider != "google"
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="invalid_account",
                message="The action requires an enabled Google Calendar account.",
                details={"account_id": str(request.account_id)},
            )
        if not hmac.compare_digest(request.parameters.calendar_id, settings.google_calendar_id):
            raise DocketError(
                code="calendar_not_allowed",
                message="The requested calendar is not the configured Docket calendar.",
            )
        link = self.session.scalar(
            select(CalendarLink).where(
                CalendarLink.record_id == record.id,
                CalendarLink.meeting_id == request.parameters.meeting_id,
                CalendarLink.account_id == account.id,
                CalendarLink.calendar_id == request.parameters.calendar_id,
            )
        )
        if request.action_type == "calendar_create_meeting" and link is not None:
            raise DocketError(
                code="calendar_link_exists",
                message="This meeting is already linked; propose an update instead.",
                details={"calendar_link_id": str(link.id)},
            )
        if request.action_type == "calendar_update_meeting":
            if link is None:
                raise DocketError(
                    code="calendar_link_not_found",
                    message="Calendar update requires an existing linked event.",
                )
            if link.last_synced_version >= record.version:
                raise DocketError(
                    code="calendar_already_synced",
                    message="The linked event is already synchronized to this record version.",
                )
        return record, course, account, schedule, link

    def propose(self, request: ProposeActionInput) -> ProposalResult:
        validate_configured_discord_source(request.source, request.actor_id)
        command, replay = self._start_command(request)
        if replay is not None:
            return replay
        definition = get_action_definition(request.action_type)
        record, course, account, schedule, link = self._load_target(request)
        parameters: dict[str, Any] = {
            "record_id": str(record.id),
            "record_version": record.version,
            "meeting_id": request.parameters.meeting_id,
            "calendar_id": request.parameters.calendar_id,
            "summary": " - ".join(
                value
                for value in (course.course_code, course.course_title)
                if value is not None
            ),
            "course_code": course.course_code,
            "course_title": course.course_title,
            "section": course.section,
            "schedule": schedule,
        }
        if link is not None:
            parameters["calendar_link_id"] = str(link.id)
            parameters["external_event_id"] = link.external_event_id
            parameters["provider_etag"] = link.provider_etag
        parameters_sha256 = sha256_json(parameters)
        preview: dict[str, Any] = {
            "action_type": request.action_type,
            "course": {
                "course_code": course.course_code,
                "course_title": course.course_title,
                "section": course.section,
            },
            "meeting_id": request.parameters.meeting_id,
            "schedule": schedule,
            "target": {
                "account_id": str(account.id),
                "calendar_id": request.parameters.calendar_id,
            },
            "record": {"record_id": str(record.id), "version": record.version},
        }
        if link is not None:
            preview["before"] = {
                "external_event_id": link.external_event_id,
                "last_synced_version": link.last_synced_version,
                "schedule": link.synced_snapshot,
            }
            preview["after"] = schedule
        preview_sha256 = sha256_json(preview)

        queue_item = QueueItem(
            deduplication_key=f"manual_action:{request.request_key}",
            material_fingerprint=parameters_sha256,
            category="calendar_change",
            title=f"{request.action_type}: {parameters['summary']}",
            summary=(
                f"{','.join(schedule['days'])} {schedule['start_time']}-"
                f"{schedule['end_time']} {schedule['timezone']}"
            ),
            status=QueueItemStatus.AWAITING_APPROVAL.value,
            priority="normal",
            received_at=utc_now(),
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            record_id=record.id,
            action_type=request.action_type,
            status=ActionStatus.APPROVAL_PENDING.value,
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        target_versions = {
            "record": {"id": str(record.id), "version": record.version},
            "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
        }
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type=request.action_type,
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=parameters_sha256,
            preview=preview,
            preview_sha256=preview_sha256,
            risk_class=definition.risk_class.value,
            target_versions=target_versions,
            created_by_actor_type=request.actor_type,
            created_by_actor_id=request.actor_id,
        )
        self.session.add(revision)
        self.session.flush()

        now = utc_now()
        expires_at = now + timedelta(seconds=definition.approval_ttl_seconds)
        approval_id = uuid.uuid4()
        signing_key = get_settings().read_secret(
            get_settings().interaction_signing_key_file
        ).encode()
        short_code = issue_short_code(approval_id, expires_at, signing_key)
        approval_token = issue_approval_token(approval_id, expires_at, signing_key)
        approval = Approval(
            id=approval_id,
            action_revision_id=revision.id,
            status=ApprovalStatus.PENDING.value,
            short_code_sha256=short_code_sha256(short_code),
            authorized_user_id=get_settings().operator_discord_user_id,
            requested_at=now,
            expires_at=expires_at,
        )
        self.session.add(approval)
        self.session.add(
            OutboxEvent(
                event_type="discord.projection.requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=f"discord_projection:{queue_item.id}:1",
                payload={
                    "queue_item_id": str(queue_item.id),
                    "action_id": str(action.id),
                    "action_revision_id": str(revision.id),
                    "approval_id": str(approval.id),
                    "approval_token": approval_token,
                    "short_code": short_code,
                    "expires_at": expires_at.isoformat(),
                    "preview": preview,
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        self.session.add(
            AuditEvent(
                event_type="action.proposed",
                entity_type="action",
                entity_id=action.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "action_type": request.action_type,
                    "revision": 1,
                    "risk_class": definition.risk_class.value,
                    "parameters_sha256": parameters_sha256,
                    "preview_sha256": preview_sha256,
                    "target_versions": target_versions,
                },
            )
        )
        result = ProposalResult(
            request_id=command.id,
            disposition="proposed",
            queue_item_id=queue_item.id,
            action_id=action.id,
            action_revision_id=revision.id,
            approval_id=approval.id,
            short_code=short_code,
            expires_at=expires_at,
            preview=preview,
        )
        self._finish_command(command, result)
        return result

    def get(self, action_id: uuid.UUID) -> dict[str, Any]:
        action = self.session.get(Action, action_id)
        if action is None:
            raise DocketError(
                code="action_not_found",
                message="The requested action does not exist.",
                details={"action_id": str(action_id)},
            )
        revision = self.session.scalar(
            select(ActionRevision).where(
                ActionRevision.action_id == action.id,
                ActionRevision.revision == action.current_revision,
            )
        )
        if revision is None:
            raise DocketError(code="invalid_action_state", message="Current revision is missing.")
        approval = self.session.scalar(
            select(Approval).where(Approval.action_revision_id == revision.id)
        )
        operation = self.session.scalar(
            select(Operation).where(Operation.action_revision_id == revision.id)
        )
        return {
            "action_id": str(action.id),
            "action_type": action.action_type,
            "status": action.status,
            "queue_item_id": str(action.queue_item_id) if action.queue_item_id else None,
            "record_id": str(action.record_id) if action.record_id else None,
            "current_revision": action.current_revision,
            "revision": {
                "action_revision_id": str(revision.id),
                "parameters_sha256": revision.parameters_sha256,
                "preview": revision.preview,
                "preview_sha256": revision.preview_sha256,
                "risk_class": revision.risk_class,
                "target_versions": revision.target_versions,
            },
            "approval": (
                {
                    "approval_id": str(approval.id),
                    "status": approval.status,
                    "expires_at": approval.expires_at.isoformat(),
                }
                if approval
                else None
            ),
            "operation": (
                {
                    "operation_id": str(operation.id),
                    "status": operation.status,
                    "attempt_count": operation.attempt_count,
                    "last_error_code": operation.last_error_code,
                }
                if operation
                else None
            ),
        }
