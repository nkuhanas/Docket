import hmac
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
)
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarEventCache,
    CalendarLink,
    CalendarReminderPlan,
    CalendarScheduleSnapshot,
    CalendarSyncState,
    CommandRequest,
    OutboxEvent,
    QueueItem,
    Record,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.providers.google.calendar import CalendarEventRequest
from docket.schemas.actions import ProposalResult, ProposeTermScheduleInput
from docket.schemas.calendar import CalendarReminderPlanInput, StandaloneCalendarEventInput
from docket.security import issue_approval_token, issue_short_code, short_code_sha256
from docket.services.calendar_actions import (
    CalendarActionService,
    _occurrence_intervals,
)
from docket.services.calendar_profile import CalendarProfileService
from docket.services.proposal_dedup import find_materially_identical_pending_proposal
from docket.services.queue import queue_projection_date
from docket.services.source_context import validate_configured_discord_source


def _aware(value: Any) -> Any:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class TermScheduleActionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def _start_command(
        self, request: ProposeTermScheduleInput
    ) -> tuple[CommandRequest, ProposalResult | None]:
        payload = request.model_dump(mode="json")
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing is not None:
            if (
                existing.operation_name != "docket_propose_term_schedule"
                or existing.input_sha256 != input_sha256
            ):
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation="docket_propose_term_schedule",
                )
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                replay = dict(existing.result)
                replay["disposition"] = "replayed_request"
                return existing, ProposalResult.model_validate(replay)
            raise DocketError(
                code="request_in_progress",
                message="The schedule proposal has not completed.",
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name="docket_propose_term_schedule",
            input_sha256=input_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    def _validate_target(
        self, request: ProposeTermScheduleInput
    ) -> tuple[Account, CalendarSyncState]:
        account = self.session.get(Account, request.account_id)
        if (
            account is None
            or account.provider != "google"
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="invalid_account",
                message="The schedule requires an enabled Google Calendar account.",
            )
        if not hmac.compare_digest(request.calendar_id, self.settings.google_calendar_id):
            raise DocketError(
                code="calendar_not_allowed",
                message="The schedule target is not the configured Docket calendar.",
            )
        profile = CalendarProfileService(self.session).get()
        if profile.proposal_mode == "off":
            raise DocketError(
                code="calendar_proposals_disabled",
                message="The Calendar profile suppresses new proposals.",
            )
        state = self.session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == account.id,
                CalendarSyncState.calendar_id == request.calendar_id,
            )
        )
        now = utc_now()
        if (
            state is None
            or state.status != "current"
            or state.last_success_at is None
            or (now - _aware(state.last_success_at)).total_seconds()
            > self.settings.calendar_stale_seconds
        ):
            raise DocketError(
                code="calendar_freshness_required",
                message="A current complete Calendar snapshot is required.",
            )
        return account, state

    def _verified_snapshot(
        self,
        snapshot_id: uuid.UUID,
    ) -> CalendarScheduleSnapshot:
        snapshot = self.session.get(CalendarScheduleSnapshot, snapshot_id)
        if snapshot is None:
            raise DocketError(
                code="schedule_snapshot_not_found",
                message="The immutable schedule snapshot does not exist.",
            )
        if sha256_json(snapshot.manifest) != snapshot.manifest_sha256:
            raise DocketError(
                code="schedule_snapshot_binding_mismatch",
                message="The schedule snapshot manifest hash does not match.",
            )
        term = self.session.get(Record, snapshot.term_record_id)
        if term is None or term.version != snapshot.term_record_version:
            raise DocketError(
                code="schedule_snapshot_stale",
                message="The term changed after the schedule snapshot was stored.",
            )
        for item in snapshot.manifest.get("items", []):
            if not isinstance(item, dict):
                raise DocketError(
                    code="schedule_snapshot_invalid",
                    message="The schedule snapshot contains an invalid item.",
                )
            try:
                record_id = uuid.UUID(str(item["course_record_id"]))
                version = int(item["course_record_version"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DocketError(
                    code="schedule_snapshot_invalid",
                    message="The schedule snapshot contains an invalid record binding.",
                ) from exc
            record = self.session.get(Record, record_id)
            if record is None or record.version != version:
                raise DocketError(
                    code="schedule_snapshot_stale",
                    message="A course changed after the schedule snapshot was stored.",
                    details={"record_id": str(record_id)},
                )
            item_hash = item.get("item_sha256")
            material = dict(item)
            material.pop("item_sha256", None)
            if item_hash != sha256_json(material):
                raise DocketError(
                    code="schedule_snapshot_binding_mismatch",
                    message="A schedule manifest item hash does not match.",
                )
        return snapshot

    def _snapshot(self, request: ProposeTermScheduleInput) -> CalendarScheduleSnapshot:
        return self._verified_snapshot(request.schedule_snapshot_id)

    @staticmethod
    def _material_snapshot(
        event: dict[str, Any],
        reminder_plan: dict[str, Any],
        logical_key: str,
    ) -> dict[str, Any]:
        request = CalendarEventRequest(
            calendar_id="preview",
            provider_correlation="preview",
            summary=str(event["title"]),
            event_spec=event,
            reminder_plan=reminder_plan,
            logical_key=logical_key,
            reminder_plan_sha256=sha256_json(reminder_plan),
            origin_kind="course_meeting",
            operation_type="calendar_create_event",
        )
        snapshot = request.snapshot()
        return {
            key: snapshot.get(key)
            for key in (
                "summary",
                "location",
                "start",
                "end",
                "recurrence",
                "reminders",
                "docket_logical_key",
                "docket_priority",
                "docket_priority_basis",
                "docket_reminder_plan_sha256",
            )
        }

    def _compile_items(
        self,
        snapshot: CalendarScheduleSnapshot,
        account: Account,
        calendar_id: str,
        reminder_plan: dict[str, Any],
        state: CalendarSyncState,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        links = {
            link.logical_key: link
            for link in self.session.scalars(
                select(CalendarLink).where(
                    CalendarLink.account_id == account.id,
                    CalendarLink.calendar_id == calendar_id,
                )
            )
        }
        compiled: list[dict[str, Any]] = []
        all_conflicts: list[dict[str, Any]] = []
        intended_intervals: list[
            tuple[
                str,
                str,
                list[tuple[datetime, datetime]],
            ]
        ] = []
        for source_item in snapshot.manifest["items"]:
            item = deepcopy(source_item)
            event_payload = deepcopy(item["event"])
            event_payload["reminder_plan"] = None
            event = StandaloneCalendarEventInput.model_validate(event_payload)
            intervals = _occurrence_intervals(event)
            if (
                not intervals
                or intervals[0][0] < _aware(state.window_start)
                or intervals[-1][1] > _aware(state.window_end)
            ):
                raise DocketError(
                    code="calendar_schedule_outside_fresh_window",
                    message=(
                        "Every schedule occurrence must fall inside Docket's "
                        "fresh complete Calendar window."
                    ),
                    details={"item_key": item["item_key"]},
                )
            link = links.get(str(item["logical_key"]))
            effect = "create"
            target: CalendarEventCache | None = None
            if link is not None:
                target = self.session.scalar(
                    select(CalendarEventCache).where(
                        CalendarEventCache.account_id == account.id,
                        CalendarEventCache.calendar_id == calendar_id,
                        CalendarEventCache.provider_event_id == link.external_event_id,
                        CalendarEventCache.status != "cancelled",
                    )
                )
                if target is None or target.has_attendees or target.organizer_is_self is False:
                    raise DocketError(
                        code="schedule_link_target_unsafe",
                        message=(
                            "A linked schedule item no longer resolves to one safe private event."
                        ),
                        details={"item_key": item["item_key"]},
                    )
                intended = self._material_snapshot(
                    event_payload,
                    reminder_plan,
                    str(item["logical_key"]),
                )
                current = {key: link.synced_snapshot.get(key) for key in intended}
                effect = "no_op" if current == intended else "update"
            conflicts = CalendarActionService(self.session)._conflicts(
                account_id=account.id,
                calendar_id=calendar_id,
                event=event,
                exclude_provider_event_id=(
                    target.provider_event_id if target is not None else None
                ),
            )
            for other_key, other_title, other_intervals in intended_intervals:
                overlap = self._first_overlap(intervals, other_intervals)
                if overlap is None or len(conflicts) >= 10:
                    continue
                overlap_start, overlap_end = overlap
                conflicts.append(
                    {
                        "kind": "schedule_overlap",
                        "conflicting_item_key": other_key,
                        "summary": other_title,
                        "start_at": overlap_start.isoformat(),
                        "end_at": overlap_end.isoformat(),
                    }
                )
            intended_intervals.append(
                (
                    str(item["item_key"]),
                    str(event.title),
                    intervals,
                )
            )
            for conflict in conflicts:
                all_conflicts.append({"item_key": item["item_key"], **conflict})
            parameters: dict[str, Any] = {
                "calendar_id": calendar_id,
                "logical_key": item["logical_key"],
                "record_id": item["course_record_id"],
                "record_version": item["course_record_version"],
                "meeting_id": item["meeting_id"],
                "event": event_payload,
                "reminder_plan": reminder_plan,
                "reminder_plan_sha256": sha256_json(reminder_plan),
                "priority": "normal",
                "priority_basis": "default",
                "origin_kind": "course_meeting",
                "classification": item["classification"],
            }
            if target is not None:
                parameters.update(
                    {
                        "external_event_id": target.provider_event_id,
                        "provider_etag": target.provider_etag,
                        "provider_before": link.synced_snapshot if link else {},
                    }
                )
            compiled_item = {
                **item,
                "effect": effect,
                "operation_type": (
                    "calendar_create_event"
                    if effect == "create"
                    else "calendar_update_event"
                    if effect == "update"
                    else "calendar_no_op"
                ),
                "parameters": parameters,
                "conflicts": conflicts,
            }
            compiled_item["parameters_sha256"] = sha256_json(parameters)
            compiled.append(compiled_item)
        return compiled, all_conflicts[:100]

    def _compile_proposal_material(
        self,
        snapshot: CalendarScheduleSnapshot,
        account: Account,
        calendar_id: str,
        reminder_plan: dict[str, Any],
        state: CalendarSyncState,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        items, conflicts = self._compile_items(
            snapshot,
            account,
            calendar_id,
            reminder_plan,
            state,
        )
        counts = {
            key: sum(item["effect"] == key for item in items)
            for key in ("create", "update", "no_op")
        }
        parameters: dict[str, Any] = {
            "calendar_id": calendar_id,
            "schedule_snapshot_id": str(snapshot.id),
            "manifest_sha256": snapshot.manifest_sha256,
            "reminder_plan": reminder_plan,
            "reminder_plan_sha256": sha256_json(reminder_plan),
            "items": items,
        }
        preview: dict[str, Any] = {
            "action_type": "calendar_apply_term_schedule",
            "target": {
                "account_id": str(account.id),
                "calendar_id": calendar_id,
            },
            "schedule_snapshot_id": str(snapshot.id),
            "manifest_sha256": snapshot.manifest_sha256,
            "term": snapshot.manifest["term"],
            "item_count": len(items),
            "counts": counts,
            "items": [
                {
                    "item_key": item["item_key"],
                    "course_code": item["course_code"],
                    "section": item["section"],
                    "meeting_id": item["meeting_id"],
                    "exception_id": item.get("exception_id"),
                    "effect": item["effect"],
                    "event": item["event"],
                    "classification": item["classification"],
                    "conflicts": item["conflicts"],
                }
                for item in items
            ],
            "reminder_plan": reminder_plan,
            "conflicts": conflicts,
            "freshness": {
                "last_success_at": _aware(state.last_success_at).isoformat(),
                "window_start": _aware(state.window_start).isoformat(),
                "window_end": _aware(state.window_end).isoformat(),
            },
        }
        material_fingerprint = sha256_json(
            {
                "action_type": "calendar_apply_term_schedule",
                "account_id": str(account.id),
                "calendar_id": calendar_id,
                "manifest_sha256": snapshot.manifest_sha256,
                "reminder_plan_sha256": sha256_json(reminder_plan),
                "targets": [
                    {
                        "item_key": item["item_key"],
                        "effect": item["effect"],
                        "external_event_id": item["parameters"].get("external_event_id"),
                        "provider_etag": item["parameters"].get("provider_etag"),
                    }
                    for item in items
                ],
            }
        )
        return parameters, preview, material_fingerprint

    @staticmethod
    def _first_overlap(
        left: list[tuple[datetime, datetime]],
        right: list[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime] | None:
        left_index = 0
        right_index = 0
        while left_index < len(left) and right_index < len(right):
            left_start, left_end = left[left_index]
            right_start, right_end = right[right_index]
            if left_start < right_end and left_end > right_start:
                return max(left_start, right_start), min(left_end, right_end)
            if left_end <= right_end:
                left_index += 1
            else:
                right_index += 1
        return None

    def propose(self, request: ProposeTermScheduleInput) -> ProposalResult:
        validate_configured_discord_source(request.source, request.actor_id)
        command, replay = self._start_command(request)
        if replay is not None:
            return replay
        account, state = self._validate_target(request)
        snapshot = self._snapshot(request)
        profile = CalendarProfileService(self.session).get()
        plan_model = request.reminder_plan or CalendarReminderPlanInput(
            delivery_channels=profile.default_reminder_delivery_channels,
            lead_seconds=profile.default_reminder_lead_seconds,
        )
        reminder_plan = plan_model.model_dump(mode="json")
        parameters, preview, material_fingerprint = self._compile_proposal_material(
            snapshot,
            account,
            request.calendar_id,
            reminder_plan,
            state,
        )
        conflicts = preview["conflicts"]
        assert isinstance(conflicts, list)
        if conflicts and profile.conflict_policy == "block":
            raise DocketError(
                code="calendar_conflict_blocked",
                message="The Calendar profile blocks this conflicting schedule.",
                details={"conflicts": conflicts[:10]},
            )
        counts = preview["counts"]
        items = parameters["items"]
        assert isinstance(counts, dict)
        assert isinstance(items, list)
        parameters_sha256 = sha256_json(parameters)
        preview_sha256 = sha256_json(preview)
        now = utc_now()
        matched = find_materially_identical_pending_proposal(
            self.session,
            category="calendar_schedule",
            material_fingerprint=material_fingerprint,
            now=now,
        )
        if matched is not None:
            result = matched.model_copy(update={"request_id": command.id})
            self.session.add(
                AuditEvent(
                    event_type="action.duplicate_suppressed",
                    entity_type="action",
                    entity_id=result.action_id,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    request_id=command.id,
                    data={
                        "material_fingerprint": material_fingerprint,
                        "matched_queue_item_id": str(result.queue_item_id),
                    },
                )
            )
            command.status = CommandStatus.SUCCEEDED.value
            command.result = result.model_dump(mode="json")
            command.completed_at = now
            return result

        queue_item = QueueItem(
            deduplication_key=f"manual_action:{request.request_key}",
            material_fingerprint=material_fingerprint,
            category="calendar_schedule",
            title=(f"Apply {snapshot.manifest['term']['term_name']} schedule ({len(items)} items)")[
                :512
            ],
            summary=(
                f"{counts['create']} create, {counts['update']} update, "
                f"{counts['no_op']} already synchronized"
            ),
            status=QueueItemStatus.AWAITING_APPROVAL.value,
            priority="normal",
            received_at=utc_now(),
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            record_id=snapshot.term_record_id,
            action_type="calendar_apply_term_schedule",
            status=ActionStatus.APPROVAL_PENDING.value,
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type=action.action_type,
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=parameters_sha256,
            preview=preview,
            preview_sha256=preview_sha256,
            risk_class=get_action_definition(action.action_type).risk_class.value,
            target_versions={
                "queue_item": {
                    "id": str(queue_item.id),
                    "version": queue_item.version,
                },
                "schedule_snapshot": {
                    "id": str(snapshot.id),
                    "manifest_sha256": snapshot.manifest_sha256,
                },
                "calendar_snapshot": {
                    "last_success_at": _aware(state.last_success_at).isoformat(),
                },
            },
            created_by_actor_type=request.actor_type,
            created_by_actor_id=request.actor_id,
        )
        self.session.add(revision)
        self.session.flush()
        for item in items:
            for lead_seconds in plan_model.lead_seconds:
                self.session.add(
                    CalendarReminderPlan(
                        action_revision_id=revision.id,
                        manifest_item_key=str(item["item_key"]),
                        lead_seconds=lead_seconds,
                        delivery_channels=list(plan_model.delivery_channels),
                        status="planned",
                    )
                )
        expires_at = now + timedelta(seconds=self.settings.approval_ttl_seconds)
        approval_id = uuid.uuid4()
        signing_key = self.settings.read_secret(self.settings.interaction_signing_key_file).encode()
        short_code = issue_short_code(approval_id, expires_at, signing_key)
        approval_token = issue_approval_token(approval_id, expires_at, signing_key)
        approval = Approval(
            id=approval_id,
            action_revision_id=revision.id,
            status=ApprovalStatus.PENDING.value,
            short_code_sha256=short_code_sha256(short_code),
            authorized_user_id=self.settings.operator_discord_user_id,
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
                    "target_local_date": queue_projection_date(
                        queue_item, self.settings
                    ).isoformat(),
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
                    "action_type": action.action_type,
                    "risk_class": revision.risk_class,
                    "manifest_sha256": snapshot.manifest_sha256,
                    "item_count": len(items),
                    "parameters_sha256": parameters_sha256,
                    "preview_sha256": preview_sha256,
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
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()
        return result
