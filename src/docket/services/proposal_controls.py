import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

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
)
from docket.domain.errors import DocketError
from docket.internal_api.schemas import LocalActionResponse
from docket.models import (
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarEventCache,
    CalendarReminderPlan,
    CalendarScheduleSnapshot,
    CalendarSyncState,
    CommandRequest,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OperationItem,
    OutboxEvent,
    QueueItem,
)
from docket.models.base import utc_now
from docket.security import (
    decode_projection_proposal_control_token,
    issue_short_code,
    short_code_sha256,
    verify_projection_proposal_control_token,
)

_PRIORITIES = {"low", "normal", "high", "urgent"}
_REMINDER_PRESETS: dict[str, list[int]] = {
    "none": [],
    "5m": [300],
    "10m": [600],
    "15m": [900],
    "30m": [1800],
    "1h": [3600],
}
_EDITABLE_ACTIONS = {
    "calendar_create_event",
    "calendar_update_event",
    "calendar_update_reminders",
}
_CUSTOM_REMINDER_FIELD = "reminder_leads_minutes"
_EDIT_FIELDS = {
    "title",
    "location",
    "operator_tags",
    _CUSTOM_REMINDER_FIELD,
}
_OPERATOR_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ProposalControlService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def _projection(self, request: LocalActionResponse) -> DiscordProjection:
        if (
            request.discord_user_id != self.settings.operator_discord_user_id
            or request.guild_id != self.settings.discord_guild_id
            or request.parent_channel_id != self.settings.queue_channel_id
            or request.channel_id == self.settings.queue_channel_id
            or request.projection_id is None
        ):
            raise DocketError(
                code="invalid_proposal_control_context",
                message="Proposal control did not come from the configured queue card.",
            )
        projection = self.session.get(DiscordProjection, request.projection_id)
        if (
            projection is None
            or projection.status != "delivered"
            or projection.message_id != request.message_id
        ):
            raise DocketError(
                code="invalid_proposal_control_projection",
                message="Proposal control is not bound to a delivered Docket card.",
            )
        daily_thread = self.session.get(DiscordDailyThread, projection.daily_thread_id)
        if (
            daily_thread is None
            or daily_thread.guild_id != request.guild_id
            or daily_thread.channel_id != request.parent_channel_id
            or daily_thread.thread_id != request.channel_id
        ):
            raise DocketError(
                code="invalid_proposal_control_projection",
                message="Proposal control thread does not match the stored projection.",
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
                code="stale_proposal_control_projection",
                message="This control belongs to an older queue projection.",
            )
        return projection

    def _bound_state(
        self,
        request: LocalActionResponse,
        projection: DiscordProjection,
    ) -> tuple[ActionRevision, Action, Approval, QueueItem, str]:
        revision = self.session.get(ActionRevision, request.action_revision_id)
        action = self.session.get(Action, revision.action_id) if revision else None
        queue_item = self.session.get(QueueItem, projection.queue_item_id)
        approval = (
            self.session.scalar(
                select(Approval).where(Approval.action_revision_id == request.action_revision_id)
            )
            if revision is not None
            else None
        )
        invalid_base = (
            revision is None
            or action is None
            or approval is None
            or queue_item is None
            or action.queue_item_id != queue_item.id
            or action.current_revision != revision.revision
            or sha256_json(revision.parameters) != revision.parameters_sha256
            or sha256_json(revision.preview) != revision.preview_sha256
        )
        if invalid_base:
            raise DocketError(
                code="stale_proposal_control",
                message="The proposal changed after this control was rendered.",
            )
        assert revision is not None
        assert action is not None
        assert approval is not None
        assert queue_item is not None
        if request.transition == "proposal_review_page":
            if (
                revision.action_type != "calendar_apply_term_schedule"
                or (
                    action.status == ActionStatus.APPROVAL_PENDING.value
                    and (
                        approval.status != ApprovalStatus.PENDING.value
                        or approval.control_projection_id != projection.id
                    )
                )
                or action.status
                not in {
                    ActionStatus.APPROVAL_PENDING.value,
                    ActionStatus.READY.value,
                    ActionStatus.EXECUTING.value,
                    ActionStatus.SUCCEEDED.value,
                    ActionStatus.PARTIAL_FAILED.value,
                    ActionStatus.FAILED.value,
                    ActionStatus.RECONCILIATION_REQUIRED.value,
                }
            ):
                raise DocketError(
                    code="stale_proposal_control",
                    message="The schedule review is no longer available.",
                )
        elif (
            action.status != ActionStatus.APPROVAL_PENDING.value
            or approval.status != ApprovalStatus.PENDING.value
            or approval.control_projection_id != projection.id
        ):
            raise DocketError(
                code="stale_proposal_control",
                message="The proposal changed after this control was rendered.",
            )
        decoded = decode_projection_proposal_control_token(request.action_token)
        if decoded is None:
            raise DocketError(
                code="invalid_proposal_control_token",
                message="Proposal control token is invalid.",
            )
        token_revision, token_projection, token_field, expires_at = decoded
        expected_field = (
            str(request.field)
            if request.transition in {"proposal_field_change", "proposal_edit"}
            and request.field is not None
            else request.transition.removeprefix("proposal_")
        )
        if (
            token_revision != revision.id
            or token_projection != projection.id
            or token_field != expected_field
            or utc_now() > expires_at
        ):
            raise DocketError(
                code="invalid_proposal_control_token",
                message="Proposal control token does not match the current card.",
            )
        signing_key = self.settings.read_secret(self.settings.interaction_signing_key_file).encode()
        if not verify_projection_proposal_control_token(
            request.action_token,
            action_revision_id=revision.id,
            projection_id=projection.id,
            field=expected_field,
            expires_at=expires_at,
            signing_key=signing_key,
        ):
            raise DocketError(
                code="invalid_proposal_control_token",
                message="Proposal control token is invalid.",
            )
        return revision, action, approval, queue_item, expected_field

    def _start_command(self, request: LocalActionResponse) -> CommandRequest:
        request_key = f"discord-interaction:{request.discord_interaction_id}"
        if (
            self.session.scalar(
                select(CommandRequest).where(CommandRequest.request_key == request_key)
            )
            is not None
        ):
            raise DocketError(
                code="interaction_replay",
                message="This Discord interaction has already been consumed.",
            )
        payload = request.model_dump(mode="json", exclude={"action_token"})
        command = CommandRequest(
            request_key=request_key,
            operation_name=request.transition,
            input_sha256=sha256_json(payload),
            actor_type="plugin",
            actor_id=request.discord_user_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command

    @staticmethod
    def _parse_reminder_leads(raw: str) -> list[int]:
        parts = [part.strip() for part in raw.split(",")]
        if not parts or any(
            not part or not part.isascii() or not part.isdecimal() for part in parts
        ):
            raise DocketError(
                code="invalid_proposal_edit",
                message="Reminder leads must be comma-separated whole minutes.",
            )
        minutes = [int(part) for part in parts]
        if (
            len(minutes) > 5
            or len(minutes) != len(set(minutes))
            or any(value < 0 or value > 40_320 for value in minutes)
        ):
            raise DocketError(
                code="invalid_proposal_edit",
                message=(
                    "Use at most five unique whole-minute reminder leads from zero through 40320."
                ),
            )
        return [value * 60 for value in sorted(minutes)]

    def prepare_refresh(self, request: LocalActionResponse) -> tuple[uuid.UUID, str]:
        if request.transition != "proposal_refresh":
            raise DocketError(
                code="invalid_proposal_refresh",
                message="Only a signed Refresh control may request a Calendar refresh.",
            )
        projection = self._projection(request)
        revision, _action, _approval, _queue_item, _field = self._bound_state(request, projection)
        calendar_id = revision.parameters.get("calendar_id")
        if revision.account_id is None or not isinstance(calendar_id, str):
            raise DocketError(
                code="proposal_refresh_unavailable",
                message="This proposal does not bind a refreshable Calendar target.",
            )
        return revision.account_id, calendar_id

    def _replace_revision(
        self,
        request: LocalActionResponse,
        revision: ActionRevision,
        action: Action,
        approval: Approval,
        queue_item: QueueItem,
        command: CommandRequest,
        *,
        reminder_leads: list[int] | None = None,
        replacement_parameters: dict[str, object] | None = None,
        replacement_preview: dict[str, object] | None = None,
        replacement_target_versions: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if replacement_parameters is None and revision.action_type not in _EDITABLE_ACTIONS:
            raise DocketError(
                code="proposal_field_not_editable",
                message="This proposal does not expose editable Calendar fields.",
            )
        parameters = (
            deepcopy(replacement_parameters)
            if replacement_parameters is not None
            else deepcopy(revision.parameters)
        )
        preview = (
            deepcopy(replacement_preview)
            if replacement_preview is not None
            else deepcopy(revision.preview)
        )
        if replacement_parameters is not None:
            pass
        elif request.field == "priority":
            assert request.value is not None
            if (
                revision.action_type
                not in {
                    "calendar_create_event",
                    "calendar_update_event",
                }
                or request.value not in _PRIORITIES
            ):
                raise DocketError(
                    code="invalid_proposal_field_value",
                    message="Priority selection is not valid for this proposal.",
                )
            parameters["priority"] = request.value
            parameters["priority_basis"] = "explicit_operator"
            event = parameters.get("event")
            if isinstance(event, dict):
                event["priority"] = request.value
            classification = preview.get("classification")
            if isinstance(classification, dict):
                classification["priority"] = request.value
                classification["priority_basis"] = "explicit_operator"
            queue_item.priority = request.value
        elif request.field == "reminder_preset":
            if reminder_leads is None:
                assert request.value is not None
                reminder_leads = _REMINDER_PRESETS.get(request.value)
            if reminder_leads is None:
                if request.value == "custom":
                    raise DocketError(
                        code="proposal_modal_required",
                        message="Custom reminders require the bounded Edit modal.",
                    )
                raise DocketError(
                    code="invalid_proposal_field_value",
                    message="Reminder preset is not recognized.",
                )
            plan: dict[str, object] = {
                "delivery_channels": ["google_popup", "docket_queue"],
                "lead_seconds": reminder_leads,
            }
            parameters["reminder_plan"] = plan
            parameters["reminder_plan_sha256"] = sha256_json(plan)
            if revision.action_type == "calendar_update_event":
                parameters["reminder_disposition"] = "replace"
            preview["reminder_plan"] = plan
            preview["reminder_disposition"] = (
                "replace"
                if revision.action_type == "calendar_update_event"
                else preview.get("reminder_disposition")
            )
        else:
            raise DocketError(
                code="invalid_proposal_field",
                message="The proposal edit is not bound to an allowlisted field.",
            )

        queue_item.version += 1
        parameters_sha256 = sha256_json(parameters)
        preview_sha256 = sha256_json(preview)
        target_versions = (
            deepcopy(replacement_target_versions)
            if replacement_target_versions is not None
            else deepcopy(revision.target_versions)
        )
        target_versions["queue_item"] = {
            "id": str(queue_item.id),
            "version": queue_item.version,
        }
        next_revision = ActionRevision(
            action_id=action.id,
            revision=revision.revision + 1,
            action_type=revision.action_type,
            account_id=revision.account_id,
            parameters=parameters,
            parameters_sha256=parameters_sha256,
            preview=preview,
            preview_sha256=preview_sha256,
            risk_class=revision.risk_class,
            target_versions=target_versions,
            created_by_actor_type="plugin",
            created_by_actor_id=request.discord_user_id,
        )
        self.session.add(next_revision)
        self.session.flush()
        for old_plan in self.session.scalars(
            select(CalendarReminderPlan).where(
                CalendarReminderPlan.action_revision_id == revision.id,
                CalendarReminderPlan.status == "planned",
            )
        ):
            old_plan.status = "cancelled"
        revision_plan = parameters.get("reminder_plan")
        if isinstance(revision_plan, dict):
            leads = revision_plan.get("lead_seconds")
            channels = revision_plan.get("delivery_channels")
            assert isinstance(leads, list) and isinstance(channels, list)
            for lead_seconds in leads:
                self.session.add(
                    CalendarReminderPlan(
                        action_revision_id=next_revision.id,
                        lead_seconds=int(lead_seconds),
                        delivery_channels=list(channels),
                        status="planned",
                    )
                )
        approval.status = ApprovalStatus.SUPERSEDED.value
        action.current_revision = next_revision.revision
        now = utc_now()
        expires_at = now + timedelta(seconds=self.settings.approval_ttl_seconds)
        approval_id = uuid.uuid4()
        signing_key = self.settings.read_secret(self.settings.interaction_signing_key_file).encode()
        short_code = issue_short_code(approval_id, expires_at, signing_key)
        next_approval = Approval(
            id=approval_id,
            action_revision_id=next_revision.id,
            status=ApprovalStatus.PENDING.value,
            short_code_sha256=short_code_sha256(short_code),
            authorized_user_id=self.settings.operator_discord_user_id,
            requested_at=now,
            expires_at=expires_at,
        )
        self.session.add(next_approval)
        self.session.add(
            OutboxEvent(
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=(
                    f"discord_projection:{queue_item.id}:revision:{next_revision.revision}"
                ),
                payload={
                    "queue_item_id": str(queue_item.id),
                    "action_id": str(action.id),
                    "action_revision_id": str(next_revision.id),
                    "approval_id": str(next_approval.id),
                    "status": "approval_pending",
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        self.session.add(
            AuditEvent(
                event_type="proposal.field_changed",
                entity_type="action",
                entity_id=action.id,
                actor_type="plugin",
                actor_id=request.discord_user_id,
                request_id=command.id,
                data={
                    "field": request.field or request.transition.removeprefix("proposal_"),
                    "value": request.value or request.modal_values,
                    "superseded_revision": revision.revision,
                    "revision": next_revision.revision,
                    "parameters_sha256": parameters_sha256,
                    "preview_sha256": preview_sha256,
                },
            )
        )
        result: dict[str, object] = {
            "ok": True,
            "transition": request.transition,
            "field": request.field or request.transition.removeprefix("proposal_"),
            "value": request.value or request.modal_values,
            "action_id": str(action.id),
            "action_revision_id": str(next_revision.id),
            "revision": next_revision.revision,
            "approval_id": str(next_approval.id),
            "queue_item_id": str(queue_item.id),
            "queue_version": queue_item.version,
        }
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = now.astimezone(UTC)
        return result

    def _edit(
        self,
        request: LocalActionResponse,
        revision: ActionRevision,
        action: Action,
        approval: Approval,
        queue_item: QueueItem,
        command: CommandRequest,
    ) -> dict[str, object]:
        if request.modal_values is None or not request.modal_values:
            raise DocketError(
                code="invalid_proposal_edit",
                message="The proposal editor did not return any values.",
            )
        keys = set(request.modal_values)
        if not keys.issubset(_EDIT_FIELDS):
            raise DocketError(
                code="invalid_proposal_edit",
                message="The proposal editor returned an unexpected field.",
            )
        if request.field == "reminder_preset" and keys != {_CUSTOM_REMINDER_FIELD}:
            raise DocketError(
                code="invalid_proposal_edit",
                message="The custom reminder editor returned an unexpected field.",
            )
        parameters = deepcopy(revision.parameters)
        preview = deepcopy(revision.preview)
        event = parameters.get("event")
        if keys - {_CUSTOM_REMINDER_FIELD} and not isinstance(event, dict):
            raise DocketError(
                code="proposal_edit_unavailable",
                message="This proposal does not contain an editable event specification.",
            )
        assert not keys - {_CUSTOM_REMINDER_FIELD} or isinstance(event, dict)
        if "title" in keys:
            title = request.modal_values["title"].strip()
            if not 1 <= len(title) <= 512:
                raise DocketError(
                    code="invalid_proposal_edit",
                    message="Event title must contain from 1 through 512 characters.",
                )
            assert isinstance(event, dict)
            event["title"] = title
            queue_item.title = f"{revision.action_type}: {title}"[:512]
        if "location" in keys:
            location = request.modal_values["location"].strip()
            assert isinstance(event, dict)
            event["location"] = None if location.casefold() == "[clear]" else location
        if "operator_tags" in keys:
            raw_tags = request.modal_values["operator_tags"].strip()
            tags = (
                []
                if raw_tags.casefold() == "[clear]"
                else sorted(
                    {item.strip().casefold() for item in raw_tags.split(",") if item.strip()}
                )
            )
            if len(tags) > 8 or any(_OPERATOR_TAG.fullmatch(tag) is None for tag in tags):
                raise DocketError(
                    code="invalid_proposal_edit",
                    message=(
                        "Use at most eight comma-separated lowercase tags containing "
                        "letters, digits, underscores, or hyphens."
                    ),
                )
            assert isinstance(event, dict)
            event["operator_tags"] = tags
            classification = preview.get("classification")
            if isinstance(classification, dict):
                classification["operator_tags"] = tags
        if isinstance(event, dict):
            preview["event"] = deepcopy(event)
        reminder_leads = None
        if _CUSTOM_REMINDER_FIELD in keys:
            reminder_leads = self._parse_reminder_leads(
                request.modal_values[_CUSTOM_REMINDER_FIELD]
            )
            plan: dict[str, object] = {
                "delivery_channels": ["google_popup", "docket_queue"],
                "lead_seconds": reminder_leads,
            }
            parameters["reminder_plan"] = plan
            parameters["reminder_plan_sha256"] = sha256_json(plan)
            if revision.action_type == "calendar_update_event":
                parameters["reminder_disposition"] = "replace"
                preview["reminder_disposition"] = "replace"
            preview["reminder_plan"] = plan
        if (
            sha256_json(parameters) == revision.parameters_sha256
            and sha256_json(preview) == revision.preview_sha256
        ):
            raise DocketError(
                code="proposal_edit_no_change",
                message="The submitted values do not change this proposal.",
            )
        return self._replace_revision(
            request,
            revision,
            action,
            approval,
            queue_item,
            command,
            reminder_leads=reminder_leads,
            replacement_parameters=parameters,
            replacement_preview=preview,
        )

    def _refresh(
        self,
        request: LocalActionResponse,
        revision: ActionRevision,
        action: Action,
        approval: Approval,
        queue_item: QueueItem,
        command: CommandRequest,
        *,
        refresh_started_at: datetime | None,
    ) -> dict[str, object]:
        if refresh_started_at is None or revision.account_id is None:
            raise DocketError(
                code="proposal_refresh_required",
                message="The Calendar refresh must complete before rebinding the proposal.",
            )
        calendar_id = revision.parameters.get("calendar_id")
        if not isinstance(calendar_id, str):
            raise DocketError(
                code="proposal_refresh_unavailable",
                message="This proposal does not bind a Calendar target.",
            )
        state = self.session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == revision.account_id,
                CalendarSyncState.calendar_id == calendar_id,
            )
        )
        if (
            state is None
            or state.status != "current"
            or state.last_success_at is None
            or _aware(state.last_success_at) < _aware(refresh_started_at)
        ):
            raise DocketError(
                code="calendar_refresh_failed",
                message="Docket could not obtain a fresh complete Calendar snapshot.",
            )
        parameters = deepcopy(revision.parameters)
        preview = deepcopy(revision.preview)
        event_payload = parameters.get("event")
        target: CalendarEventCache | None = None
        external_event_id = parameters.get("external_event_id")
        if isinstance(external_event_id, str):
            matches = list(
                self.session.scalars(
                    select(CalendarEventCache).where(
                        CalendarEventCache.account_id == revision.account_id,
                        CalendarEventCache.calendar_id == calendar_id,
                        CalendarEventCache.provider_event_id == external_event_id,
                        CalendarEventCache.status != "cancelled",
                    )
                )
            )
            if len(matches) != 1:
                raise DocketError(
                    code="calendar_event_changed",
                    message="The proposal target no longer resolves to one current event.",
                )
            target = matches[0]
            if target.has_attendees or target.organizer_is_self is False:
                raise DocketError(
                    code="calendar_event_not_private",
                    message="The refreshed event is no longer safe for private control.",
                )
            from docket.services.calendar_actions import _provider_snapshot

            before = _provider_snapshot(target)
            parameters["provider_etag"] = target.provider_etag
            parameters["provider_before"] = before
            preview["before"] = before
        conflicts: list[dict[str, object]] = []
        if isinstance(event_payload, dict) and revision.action_type in {
            "calendar_create_event",
            "calendar_update_event",
        }:
            from docket.schemas.calendar import StandaloneCalendarEventInput
            from docket.services.calendar_actions import CalendarActionService

            safe_payload = deepcopy(event_payload)
            safe_payload["priority"] = "normal"
            event = StandaloneCalendarEventInput.model_validate(safe_payload)
            conflicts = CalendarActionService(self.session)._conflicts(
                account_id=revision.account_id,
                calendar_id=calendar_id,
                event=event,
                exclude_provider_event_id=(
                    target.provider_event_id if target is not None else None
                ),
            )
        preview["conflicts"] = conflicts
        preview["freshness"] = {
            "last_success_at": _aware(state.last_success_at).isoformat(),
            "window_start": _aware(state.window_start).isoformat(),
            "window_end": _aware(state.window_end).isoformat(),
        }
        target_versions = deepcopy(revision.target_versions)
        target_versions["calendar_snapshot"] = {
            "last_success_at": _aware(state.last_success_at).isoformat(),
            "provider_event_id": target.provider_event_id if target else None,
            "provider_etag": target.provider_etag if target else None,
        }
        if (
            sha256_json(parameters) == revision.parameters_sha256
            and sha256_json(preview) == revision.preview_sha256
            and target_versions == revision.target_versions
        ):
            now = utc_now()
            result: dict[str, object] = {
                "ok": True,
                "transition": request.transition,
                "no_change": True,
                "action_id": str(action.id),
                "action_revision_id": str(revision.id),
                "revision": revision.revision,
            }
            command.status = CommandStatus.SUCCEEDED.value
            command.result = result
            command.completed_at = now
            self.session.add(
                AuditEvent(
                    event_type="proposal.refreshed_no_change",
                    entity_type="action",
                    entity_id=action.id,
                    actor_type="plugin",
                    actor_id=request.discord_user_id,
                    request_id=command.id,
                    data={"revision": revision.revision},
                )
            )
            return result
        return self._replace_revision(
            request,
            revision,
            action,
            approval,
            queue_item,
            command,
            replacement_parameters=parameters,
            replacement_preview=preview,
            replacement_target_versions=target_versions,
        )

    def _snooze(
        self,
        request: LocalActionResponse,
        revision: ActionRevision,
        action: Action,
        approval: Approval,
        queue_item: QueueItem,
        command: CommandRequest,
    ) -> dict[str, object]:
        from zoneinfo import ZoneInfo

        from docket.services.queue import local_date_at_rollover

        target_date = utc_now().astimezone(ZoneInfo(self.settings.timezone)).date() + timedelta(
            days=1
        )
        queue_item.status = QueueItemStatus.SNOOZED.value
        queue_item.snooze_local_date = target_date
        queue_item.snoozed_until = local_date_at_rollover(target_date, self.settings)
        result = self._replace_revision(
            request,
            revision,
            action,
            approval,
            queue_item,
            command,
            replacement_parameters=deepcopy(revision.parameters),
            replacement_preview=deepcopy(revision.preview),
        )
        result["queue_status"] = queue_item.status
        result["snooze_local_date"] = target_date.isoformat()
        result["snoozed_until"] = queue_item.snoozed_until.isoformat()
        command.result = result
        return result

    @staticmethod
    def _schedule_review_row(
        item: dict[str, object],
        effect: str | None,
        status: str | None = None,
        error_code: str | None = None,
    ) -> str:
        course = " ".join(
            str(value) for value in (item.get("course_code"), item.get("section")) if value
        )
        meeting = str(item.get("meeting_id") or "meeting")
        exception_id = item.get("exception_id")
        if exception_id:
            meeting = f"{meeting} / {exception_id}"
        event = item.get("event")
        timing = event.get("timing", {}) if isinstance(event, dict) else {}
        when = ""
        if isinstance(timing, dict):
            if timing.get("kind") == "timed":
                when = f"{timing.get('start_local', '?')} through {timing.get('end_local', '?')}"
            elif timing.get("kind") == "all_day":
                when = f"{timing.get('start_date', '?')} through {timing.get('end_date', '?')}"
        labels = [value for value in (effect, status, error_code) if value]
        suffix = f" · {', '.join(labels)}" if labels else ""
        row = f"• {course or 'Course'} · {meeting} · {when or 'time unavailable'}{suffix}"
        return row[:350]

    def _review_page(
        self,
        request: LocalActionResponse,
        revision: ActionRevision,
        action: Action,
        command: CommandRequest,
    ) -> dict[str, object]:
        try:
            snapshot_id = uuid.UUID(str(revision.parameters["schedule_snapshot_id"]))
        except (KeyError, ValueError) as exc:
            raise DocketError(
                code="schedule_review_unavailable",
                message="The schedule snapshot binding is invalid.",
            ) from exc
        snapshot = self.session.get(CalendarScheduleSnapshot, snapshot_id)
        if (
            snapshot is None
            or snapshot.manifest_sha256 != revision.parameters.get("manifest_sha256")
            or sha256_json(snapshot.manifest) != snapshot.manifest_sha256
        ):
            raise DocketError(
                code="schedule_review_unavailable",
                message="The immutable schedule snapshot could not be verified.",
            )
        raw_items = snapshot.manifest.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise DocketError(
                code="schedule_review_unavailable",
                message="The immutable schedule snapshot has no reviewable items.",
            )
        items = [item for item in raw_items if isinstance(item, dict)]
        if len(items) != len(raw_items):
            raise DocketError(
                code="schedule_review_unavailable",
                message="The immutable schedule snapshot contains an invalid item.",
            )
        preview_items = revision.preview.get("items", [])
        effects = {
            str(item.get("item_key")): str(item.get("effect"))
            for item in preview_items
            if isinstance(item, dict)
            and item.get("item_key") is not None
            and item.get("effect") is not None
        }
        value = request.value
        assert value is not None
        result_items: list[dict[str, object]]
        if value == "failures":
            operation = self.session.scalar(
                select(Operation)
                .where(
                    Operation.action_revision_id == revision.id,
                    Operation.operation_type == "calendar_apply_term_schedule",
                )
                .order_by(Operation.created_at.desc())
                .limit(1)
            )
            if operation is None:
                raise DocketError(
                    code="schedule_failures_unavailable",
                    message="The schedule batch has no execution ledger.",
                )
            ledger = list(
                self.session.scalars(
                    select(OperationItem)
                    .where(
                        OperationItem.operation_id == operation.id,
                        OperationItem.status.in_(("failed", "reconciliation_required")),
                    )
                    .order_by(OperationItem.item_key)
                )
            )
            if not ledger:
                raise DocketError(
                    code="schedule_failures_unavailable",
                    message="The schedule batch has no failed or uncertain items.",
                )
            by_key = {str(item["item_key"]): item for item in items}
            result_items = [
                {
                    "item_key": row.item_key,
                    "status": row.status,
                    "error_code": row.last_error_code,
                }
                for row in ledger[:10]
            ]
            lines = [
                self._schedule_review_row(
                    by_key.get(row.item_key, {"item_key": row.item_key}),
                    effects.get(row.item_key),
                    row.status,
                    row.last_error_code,
                )
                for row in ledger[:10]
            ]
            heading = f"Schedule failures · {len(ledger)} item{'s' if len(ledger) != 1 else ''}"
            page_value: int | str = "failures"
            page_count = 1
        else:
            if not value.isascii() or not value.isdecimal():
                raise DocketError(
                    code="invalid_schedule_review_page",
                    message="The schedule review page is invalid.",
                )
            page = int(value)
            page_count = (len(items) + 9) // 10
            if page < 1 or page > page_count or page_count > 5:
                raise DocketError(
                    code="invalid_schedule_review_page",
                    message="The schedule review page is outside the manifest.",
                )
            selected = items[(page - 1) * 10 : page * 10]
            result_items = [
                {
                    "item_key": str(item["item_key"]),
                    "effect": effects.get(str(item["item_key"])),
                }
                for item in selected
            ]
            lines = [
                self._schedule_review_row(
                    item,
                    effects.get(str(item["item_key"])),
                )
                for item in selected
            ]
            heading = f"Schedule items · page {page}/{page_count} · {len(items)} total"
            page_value = page
        content = f"**{heading}**\n" + "\n".join(lines)
        content = content[:2000]
        now = utc_now()
        result: dict[str, object] = {
            "ok": True,
            "transition": request.transition,
            "field": "review_page",
            "value": page_value,
            "page_count": page_count,
            "item_count": len(items),
            "items": result_items,
            "content": content,
            "action_id": str(action.id),
            "action_revision_id": str(revision.id),
        }
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = now
        self.session.add(
            AuditEvent(
                event_type="proposal.schedule_reviewed",
                entity_type="action",
                entity_id=action.id,
                actor_type="plugin",
                actor_id=request.discord_user_id,
                request_id=command.id,
                data={
                    "page": page_value,
                    "returned_items": len(result_items),
                    "manifest_sha256": snapshot.manifest_sha256,
                },
            )
        )
        return result

    def respond(
        self,
        request: LocalActionResponse,
        *,
        refresh_started_at: datetime | None = None,
    ) -> dict[str, object]:
        projection = self._projection(request)
        revision, action, approval, queue_item, _field = self._bound_state(request, projection)
        command = self._start_command(request)
        if request.transition not in {
            "proposal_field_change",
            "proposal_edit",
            "proposal_refresh",
            "proposal_snooze",
            "proposal_review_page",
        }:
            raise DocketError(
                code="proposal_control_unavailable",
                message="This proposal control transition is not implemented.",
            )
        if (
            request.transition != "proposal_review_page"
            and queue_item.status != QueueItemStatus.AWAITING_APPROVAL.value
        ):
            raise DocketError(
                code="stale_proposal_control",
                message="The proposal is no longer awaiting approval.",
            )
        if request.transition == "proposal_edit":
            return self._edit(
                request,
                revision,
                action,
                approval,
                queue_item,
                command,
            )
        if request.transition == "proposal_refresh":
            return self._refresh(
                request,
                revision,
                action,
                approval,
                queue_item,
                command,
                refresh_started_at=refresh_started_at,
            )
        if request.transition == "proposal_snooze":
            return self._snooze(
                request,
                revision,
                action,
                approval,
                queue_item,
                command,
            )
        if request.transition == "proposal_review_page":
            return self._review_page(
                request,
                revision,
                action,
                command,
            )
        return self._replace_revision(
            request,
            revision,
            action,
            approval,
            queue_item,
            command,
        )
