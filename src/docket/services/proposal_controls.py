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
    OutboxEvent,
    QueueItem,
)
from docket.models.base import utc_now
from docket.security import (
    ReviewNavigationReference,
    decode_projection_proposal_control_token,
    decode_projection_review_navigation_token,
    issue_short_code,
    short_code_sha256,
    verify_projection_proposal_control_token,
    verify_projection_review_navigation_token,
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
        projection = self.session.scalar(
            select(DiscordProjection)
            .where(DiscordProjection.id == request.projection_id)
            .with_for_update()
        )
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
        if request.transition == "proposal_review_navigate":
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
        if request.transition == "proposal_review_navigate":
            decoded_navigation = decode_projection_review_navigation_token(request.action_token)
            expected_reference = ReviewNavigationReference(
                action_revision_id=revision.id,
                projection_id=projection.id,
                projection_version=projection.projection_version,
                source_view=str(request.source_view),
                source_page=request.source_page,
                target_view=str(request.target_view),
                target_page=request.target_page,
                actor_id=str(int(request.discord_user_id)),
                expires_at=(
                    decoded_navigation.expires_at if decoded_navigation is not None else utc_now()
                ),
            )
            signing_key = self.settings.read_secret(
                self.settings.interaction_signing_key_file
            ).encode()
            if (
                decoded_navigation is None
                or decoded_navigation != expected_reference
                or utc_now() > decoded_navigation.expires_at
                or not verify_projection_review_navigation_token(
                    request.action_token,
                    reference=expected_reference,
                    signing_key=signing_key,
                )
            ):
                raise DocketError(
                    code="invalid_proposal_control_token",
                    message="Review navigation token does not match the current card.",
                )
            return revision, action, approval, queue_item, "review_navigation"

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

    @staticmethod
    def _command_payload(request: LocalActionResponse) -> dict[str, object]:
        return request.model_dump(
            mode="json",
            exclude={"action_token", "request_id", "responded_at"},
        )

    def _replay_result(self, request: LocalActionResponse) -> dict[str, object] | None:
        request_key = f"discord-interaction:{request.discord_interaction_id}"
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request_key)
        )
        if existing is None:
            return None
        if existing.input_sha256 != sha256_json(self._command_payload(request)):
            raise DocketError(
                code="idempotency_conflict",
                message="This Discord interaction ID was reused with different input.",
            )
        if existing.status == CommandStatus.SUCCEEDED.value and isinstance(existing.result, dict):
            return dict(existing.result)
        raise DocketError(
            code="interaction_replay",
            message="This Discord interaction is already in progress.",
        )

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
        payload = self._command_payload(request)
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

    def _schedule_review_state(
        self,
        revision: ActionRevision,
    ) -> tuple[CalendarScheduleSnapshot, int]:
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
        if len(items) > 50 or revision.preview.get("item_count") != len(items):
            raise DocketError(
                code="schedule_review_unavailable",
                message="The schedule review count does not match its immutable preview.",
            )
        return snapshot, (len(items) + 9) // 10

    def _failure_page_count(self, revision: ActionRevision) -> int:
        operation = self.session.scalar(
            select(Operation)
            .where(
                Operation.action_revision_id == revision.id,
                Operation.operation_type == "calendar_apply_term_schedule",
            )
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
        failures = (
            operation.result.get("failures")
            if operation is not None and isinstance(operation.result, dict)
            else None
        )
        if not isinstance(failures, list) or not failures:
            raise DocketError(
                code="schedule_failures_unavailable",
                message="The schedule batch has no failed or uncertain items.",
            )
        return (min(len(failures), 50) + 9) // 10

    def _navigate_review(
        self,
        request: LocalActionResponse,
        revision: ActionRevision,
        action: Action,
        queue_item: QueueItem,
        projection: DiscordProjection,
        command: CommandRequest,
    ) -> dict[str, object]:
        snapshot, review_page_count = self._schedule_review_state(revision)
        source_view = request.source_view
        target_view = request.target_view
        assert source_view is not None and target_view is not None
        if (
            projection.view_action_revision_id != revision.id
            or projection.view_mode != source_view
            or projection.view_page != request.source_page
        ):
            raise DocketError(
                code="stale_review_navigation",
                message="The schedule card moved after this navigation control was rendered.",
            )

        pending = action.status == ActionStatus.APPROVAL_PENDING.value
        failure_page_count: int | None = None
        if source_view == "schedule_failures" or target_view == "schedule_failures":
            failure_page_count = self._failure_page_count(revision)

        legal = False
        reviewed_through = projection.reviewed_through_page
        if target_view == "summary" and source_view in {
            "schedule_review",
            "decision",
            "schedule_failures",
        }:
            legal = request.target_page is None
        elif target_view == "schedule_review" and pending:
            if source_view == "summary":
                legal = request.target_page == 1
            elif source_view == "schedule_review":
                assert request.source_page is not None
                legal = (
                    request.target_page is not None
                    and abs(request.target_page - request.source_page) == 1
                    and request.target_page <= review_page_count
                )
            elif source_view == "decision":
                legal = request.target_page == review_page_count
            if legal and request.target_page is not None:
                if source_view == "summary" and request.target_page == 1:
                    reviewed_through = max(reviewed_through, 1)
                elif (
                    source_view == "schedule_review"
                    and request.source_page is not None
                    and request.target_page == request.source_page + 1
                    and request.source_page <= reviewed_through
                ):
                    reviewed_through = max(reviewed_through, request.target_page)
        elif target_view == "decision" and pending:
            legal = (
                source_view == "schedule_review"
                and request.source_page == review_page_count
                and request.target_page is None
                and reviewed_through == review_page_count
            )
        elif target_view == "schedule_failures" and not pending:
            assert failure_page_count is not None
            if source_view == "summary":
                legal = request.target_page == 1
            elif source_view == "schedule_failures":
                assert request.source_page is not None
                legal = (
                    request.target_page is not None
                    and abs(request.target_page - request.source_page) == 1
                    and request.target_page <= failure_page_count
                )
        if not legal:
            raise DocketError(
                code="invalid_review_navigation",
                message="That review transition is not adjacent to the current card view.",
            )

        projection.view_mode = target_view
        projection.view_page = request.target_page
        projection.reviewed_through_page = reviewed_through
        projection.status = "pending"
        outbox_id = uuid.uuid4()
        daily_thread = self.session.get(DiscordDailyThread, projection.daily_thread_id)
        if daily_thread is None:
            raise DocketError(
                code="invalid_proposal_control_projection",
                message="The schedule projection thread is unavailable.",
            )
        self.session.add(
            OutboxEvent(
                id=outbox_id,
                event_type="discord.projection.refresh_requested",
                aggregate_type="queue_item",
                aggregate_id=queue_item.id,
                deduplication_key=(
                    f"discord_projection:{queue_item.id}:review:{request.discord_interaction_id}"
                ),
                payload={
                    "queue_item_id": str(queue_item.id),
                    "projection_id": str(projection.id),
                    "target_local_date": daily_thread.local_date.isoformat(),
                    "reason": "schedule_review_navigation",
                },
                status=OutboxStatus.PENDING.value,
            )
        )
        now = utc_now()
        result: dict[str, object] = {
            "ok": True,
            "transition": request.transition,
            "source_view": source_view,
            "source_page": request.source_page,
            "target_view": target_view,
            "target_page": request.target_page,
            "review_page_count": review_page_count,
            "reviewed_through_page": reviewed_through,
            "action_id": str(action.id),
            "action_revision_id": str(revision.id),
            "projection_id": str(projection.id),
            "source_projection_version": projection.projection_version,
            "projection_outbox_id": str(outbox_id),
        }
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = now
        self.session.add(
            AuditEvent(
                event_type="proposal.schedule_review_navigated",
                entity_type="action",
                entity_id=action.id,
                actor_type="plugin",
                actor_id=request.discord_user_id,
                request_id=command.id,
                data={
                    "projection_id": str(projection.id),
                    "source_view": source_view,
                    "source_page": request.source_page,
                    "target_view": target_view,
                    "target_page": request.target_page,
                    "reviewed_through_page": reviewed_through,
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
        replay = self._replay_result(request)
        if replay is not None:
            return replay
        projection = self._projection(request)
        revision, action, approval, queue_item, _field = self._bound_state(request, projection)
        command = self._start_command(request)
        if request.transition not in {
            "proposal_field_change",
            "proposal_edit",
            "proposal_refresh",
            "proposal_snooze",
            "proposal_review_navigate",
        }:
            raise DocketError(
                code="proposal_control_unavailable",
                message="This proposal control transition is not implemented.",
            )
        if (
            request.transition != "proposal_review_navigate"
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
        if request.transition == "proposal_review_navigate":
            return self._navigate_review(
                request,
                revision,
                action,
                queue_item,
                projection,
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
