from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.enums import CommandStatus
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
from docket.models import AuditEvent, CalendarProfile, CommandRequest
from docket.models.base import utc_now
from docket.schemas.calendar import CalendarProfileResult, SetCalendarProfileInput
from docket.services.source_context import validate_configured_discord_source


def _profile_result(profile: CalendarProfile) -> CalendarProfileResult:
    return CalendarProfileResult(
        operator_user_id=profile.operator_user_id,
        proposal_mode=profile.proposal_mode,
        default_reminder_lead_seconds=list(profile.default_reminder_lead_seconds),
        default_reminder_delivery_channels=list(
            profile.default_reminder_delivery_channels
        ),
        conflict_policy=profile.conflict_policy,
        version=profile.version,
    )


class CalendarProfileService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self) -> CalendarProfileResult:
        operator_user_id = get_settings().operator_discord_user_id
        profile = self.session.scalar(
            select(CalendarProfile).where(
                CalendarProfile.operator_user_id == operator_user_id
            )
        )
        if profile is None:
            profile = CalendarProfile(operator_user_id=operator_user_id)
            self.session.add(profile)
            self.session.flush()
            self.session.add(
                AuditEvent(
                    event_type="calendar_profile.initialized",
                    entity_type="calendar_profile",
                    entity_id=profile.id,
                    actor_type="docket",
                    actor_id=None,
                    request_id=None,
                    data={
                        "proposal_mode": profile.proposal_mode,
                        "default_reminder_lead_seconds": (
                            profile.default_reminder_lead_seconds
                        ),
                        "conflict_policy": profile.conflict_policy,
                        "version": profile.version,
                    },
                )
            )
        return _profile_result(profile)

    def set(self, request: SetCalendarProfileInput) -> CalendarProfileResult:
        validate_configured_discord_source(request.source, request.actor_id)
        payload = request.model_dump(mode="json")
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(
                CommandRequest.request_key == request.request_key
            )
        )
        if existing is not None:
            if (
                existing.operation_name != "docket_set_calendar_profile"
                or existing.input_sha256 != input_sha256
            ):
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation="docket_set_calendar_profile",
                )
            if (
                existing.status == CommandStatus.SUCCEEDED.value
                and existing.result is not None
            ):
                return CalendarProfileResult.model_validate(existing.result)
            raise DocketError(
                code="request_in_progress",
                message="The Calendar profile request has not completed successfully.",
                details={"request_key": request.request_key, "status": existing.status},
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name="docket_set_calendar_profile",
            input_sha256=input_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()

        current = self.get()
        profile = self.session.scalar(
            select(CalendarProfile).where(
                CalendarProfile.operator_user_id == current.operator_user_id
            )
        )
        assert profile is not None
        if profile.version != request.expected_version:
            raise VersionConflict(
                str(profile.id), request.expected_version, profile.version
            )
        before: dict[str, Any] = _profile_result(profile).model_dump(mode="json")
        profile.proposal_mode = request.proposal_mode
        profile.default_reminder_lead_seconds = list(
            request.default_reminder_lead_seconds
        )
        profile.default_reminder_delivery_channels = list(
            request.default_reminder_delivery_channels
        )
        profile.conflict_policy = request.conflict_policy
        profile.version += 1
        result = _profile_result(profile)
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()
        self.session.add(
            AuditEvent(
                event_type="calendar_profile.updated",
                entity_type="calendar_profile",
                entity_id=profile.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "before": before,
                    "after": result.model_dump(mode="json"),
                },
            )
        )
        return result
