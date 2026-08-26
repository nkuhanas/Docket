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
    OperationStatus,
    OutboxStatus,
    QueueItemStatus,
    QueuePresentation,
    RecordStatus,
)
from docket.domain.errors import DocketError, IdempotencyConflict, VersionConflict
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
    CommandRequest,
    Operation,
    OperationItem,
    OutboxEvent,
    QueueItem,
    Record,
)
from docket.models.base import utc_now
from docket.policy import get_action_definition
from docket.schemas.actions import ProposalResult, ProposeCourseReconciliationInput
from docket.schemas.calendar import CalendarReminderPlanInput, StandaloneCalendarEventInput
from docket.schemas.records import CourseData, TermData
from docket.security import issue_approval_token, issue_short_code, short_code_sha256
from docket.services.calendar_actions import CalendarActionService, _occurrence_intervals
from docket.services.calendar_profile import CalendarProfileService
from docket.services.course_manifest import (
    calendar_material_snapshot,
    compile_course_items,
    current_calendar_material_snapshot,
    first_overlap,
)
from docket.services.operation_materialization import operation_idempotency_key
from docket.services.operational_logs import enqueue_action_system_log
from docket.services.operations import OperationRunner
from docket.services.proposal_dedup import find_materially_identical_pending_proposal
from docket.services.queue import queue_projection_date
from docket.services.source_context import validate_configured_discord_source


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def course_reconciliation_dependency_sha256(parameters: dict[str, Any]) -> str:
    """Hash only provider state that can change one course's approved effect."""

    dependencies: list[dict[str, Any]] = []
    raw_items = parameters.get("items")
    if not isinstance(raw_items, list):
        raise DocketError(
            code="approval_binding_mismatch",
            message="The course action contains an invalid item manifest.",
        )
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise DocketError(
                code="approval_binding_mismatch",
                message="The course action contains an invalid item manifest.",
            )
        item_parameters = raw_item.get("parameters")
        conflicts = raw_item.get("conflicts", [])
        if not isinstance(item_parameters, dict) or not isinstance(conflicts, list):
            raise DocketError(
                code="approval_binding_mismatch",
                message="The course action contains invalid provider dependencies.",
            )
        dependencies.append(
            {
                "item_key": raw_item.get("item_key"),
                "effect": raw_item.get("effect"),
                "operation_type": raw_item.get("operation_type"),
                "external_event_id": item_parameters.get("external_event_id"),
                "provider_etag": item_parameters.get("provider_etag"),
                "conflicts": sorted(conflicts, key=sha256_json),
            }
        )
    return sha256_json(
        {
            "calendar_id": parameters.get("calendar_id"),
            "record_id": parameters.get("record_id"),
            "record_version": parameters.get("record_version"),
            "mode": parameters.get("mode"),
            "items": sorted(dependencies, key=lambda item: str(item["item_key"])),
        }
    )


class CourseReconciliationService:
    """Compile one canonical course against its independently linked meetings."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def _start_command(
        self,
        request: ProposeCourseReconciliationInput,
        *,
        operation_name: str,
    ) -> tuple[CommandRequest, dict[str, Any] | None]:
        payload = request.model_dump(mode="json")
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing is not None:
            if existing.operation_name != operation_name or existing.input_sha256 != input_sha256:
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation=operation_name,
                )
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                replay = dict(existing.result)
                replay["disposition"] = "replayed_request"
                return existing, replay
            raise DocketError(
                code="request_in_progress",
                message="The course reconciliation request has not completed.",
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name=operation_name,
            input_sha256=input_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    def _validate_target(
        self, request: ProposeCourseReconciliationInput
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
                message="Course reconciliation requires an enabled Google Calendar account.",
            )
        if not hmac.compare_digest(request.calendar_id, self.settings.google_calendar_id):
            raise DocketError(
                code="calendar_not_allowed",
                message="Course reconciliation targets only the configured Docket calendar.",
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

    def _course(self, request: ProposeCourseReconciliationInput) -> tuple[Record, CourseData]:
        record = self.session.scalar(
            select(Record).where(Record.id == request.record_id).with_for_update()
        )
        if record is None or record.record_type != "course":
            raise DocketError(
                code="invalid_action_target",
                message="Course reconciliation requires one canonical course record.",
            )
        if record.version != request.expected_record_version:
            raise VersionConflict(str(record.id), request.expected_record_version, record.version)
        if record.status != RecordStatus.ACTIVE.value:
            raise DocketError(
                code="course_archived",
                message="Restore the archived course before reconciling its meetings.",
                details={"record_id": str(record.id), "version": record.version},
            )
        return record, CourseData.model_validate(record.data)

    def _desired_items(self, record: Record, course: CourseData) -> list[dict[str, Any]]:
        if not course.meetings:
            return []
        term = self.session.get(Record, course.term_record_id)
        if term is None or term.record_type != "term" or term.status != RecordStatus.ACTIVE.value:
            raise DocketError(
                code="invalid_term_reference",
                message="The course must reference one active term.",
            )
        term_data = TermData.model_validate(term.data)
        if term_data.start_date is None or term_data.end_date is None:
            raise DocketError(
                code="incomplete_schedule_term",
                message="The course term requires explicit start and end dates.",
            )
        return [
            deepcopy(item)
            for item in compile_course_items(
                record,
                course,
                term_data,
            )
        ]

    def _safe_target(
        self,
        *,
        account_id: uuid.UUID,
        calendar_id: str,
        link: CalendarLink,
    ) -> CalendarEventCache:
        controls = CalendarActionService(self.session)
        if link.recurrence_kind == "recurring":
            try:
                target, _ = controls._target_series(
                    account_id,
                    calendar_id,
                    link.external_event_id,
                )
                return target
            except DocketError as exc:
                if exc.code != "calendar_series_not_found":
                    raise
                # A just-written recurring master can be present before the next
                # expanded-instance sync. The exact Docket link and the private
                # master cache row still provide a safe, ETag-bound target.
                cached_target = self.session.scalar(
                    select(CalendarEventCache).where(
                        CalendarEventCache.account_id == account_id,
                        CalendarEventCache.calendar_id == calendar_id,
                        CalendarEventCache.provider_event_id == link.external_event_id,
                        CalendarEventCache.status != "cancelled",
                    )
                )
                if (
                    cached_target is None
                    or cached_target.has_attendees
                    or cached_target.organizer_is_self is False
                    or cached_target.provider_etag != link.provider_etag
                ):
                    raise
                return cached_target
        return controls._target_event(account_id, calendar_id, link.external_event_id)

    @staticmethod
    def _link_active(link: CalendarLink) -> bool:
        return link.synced_snapshot.get("status") != "cancelled"

    @staticmethod
    def _cancel_item(
        record: Record,
        course: CourseData,
        link: CalendarLink,
        *,
        calendar_id: str,
        target: CalendarEventCache,
        reason: str,
    ) -> dict[str, Any]:
        classification = {
            "recurrence_kind": link.recurrence_kind,
            "system_tags": list(link.system_tags),
            "operator_tags": list(link.operator_tags),
            "priority": link.priority,
            "priority_basis": link.priority_basis,
        }
        empty_plan = CalendarReminderPlanInput(lead_seconds=[]).model_dump(mode="json")
        parameters = {
            "calendar_id": calendar_id,
            "logical_key": link.logical_key,
            "record_id": str(record.id),
            "record_version": record.version,
            "meeting_id": link.meeting_id,
            "event": None,
            "reminder_plan": empty_plan,
            "reminder_plan_sha256": sha256_json(empty_plan),
            "priority": link.priority,
            "priority_basis": link.priority_basis,
            "origin_kind": "course_meeting",
            "classification": classification,
            "external_event_id": target.provider_event_id,
            "provider_etag": target.provider_etag,
            "provider_before": deepcopy(link.synced_snapshot),
            "reason": reason,
            "target_scope": "series" if link.recurrence_kind == "recurring" else "event",
        }
        item: dict[str, Any] = {
            "item_key": link.logical_key,
            "course_code": course.course_code,
            "section": course.section,
            "meeting_id": link.meeting_id,
            "exception_id": None,
            "effect": "cancel",
            "operation_type": "calendar_cancel_event",
            "event": None,
            "before": deepcopy(link.synced_snapshot),
            "classification": classification,
            "conflicts": [],
            "parameters": parameters,
        }
        item["parameters_sha256"] = sha256_json(parameters)
        return item

    def _compile_items(
        self,
        *,
        record: Record,
        course: CourseData,
        mode: str,
        account: Account,
        calendar_id: str,
        reminder_plan: dict[str, Any],
        state: CalendarSyncState,
        reason: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        links = {
            link.logical_key: link
            for link in self.session.scalars(
                select(CalendarLink).where(
                    CalendarLink.record_id == record.id,
                    CalendarLink.account_id == account.id,
                    CalendarLink.calendar_id == calendar_id,
                    CalendarLink.origin_kind == "course_meeting",
                )
            )
        }
        if mode == "drop":
            other_active = list(
                self.session.scalars(
                    select(CalendarLink).where(
                        CalendarLink.record_id == record.id,
                        CalendarLink.origin_kind == "course_meeting",
                        (
                            (CalendarLink.account_id != account.id)
                            | (CalendarLink.calendar_id != calendar_id)
                        ),
                    )
                )
            )
            other_active = [link for link in other_active if self._link_active(link)]
            if other_active:
                raise DocketError(
                    code="course_drop_multiple_calendar_targets",
                    message=(
                        "The course has active links outside the selected Calendar target; "
                        "Docket will not archive it after cancelling only a subset."
                    ),
                    details={
                        "active_targets": sorted(
                            {f"{link.account_id}:{link.calendar_id}" for link in other_active}
                        )
                    },
                )
        desired_items = self._desired_items(record, course) if mode == "sync" else []
        desired_keys = {str(item["logical_key"]) for item in desired_items}
        compiled: list[dict[str, Any]] = []
        all_conflicts: list[dict[str, Any]] = []
        intended_intervals: list[tuple[str, str, list[tuple[datetime, datetime]]]] = []

        for source_item in desired_items:
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
                    code="calendar_course_outside_fresh_window",
                    message="Every course occurrence must fall inside the fresh Calendar window.",
                    details={"item_key": item["item_key"]},
                )
            logical_key = str(item["logical_key"])
            link = links.get(logical_key)
            target: CalendarEventCache | None = None
            effect = "create"
            if link is not None and self._link_active(link):
                target = self._safe_target(
                    account_id=account.id,
                    calendar_id=calendar_id,
                    link=link,
                )
                intended = calendar_material_snapshot(
                    event_payload,
                    reminder_plan,
                    logical_key,
                )
                current = current_calendar_material_snapshot(
                    link.synced_snapshot,
                    intended,
                )
                effect = "no_op" if current == intended else "update"
            conflicts = CalendarActionService(self.session)._conflicts(
                account_id=account.id,
                calendar_id=calendar_id,
                event=event,
                exclude_provider_event_id=target.provider_event_id if target else None,
            )
            for other_key, other_title, other_intervals in intended_intervals:
                overlap = first_overlap(intervals, other_intervals)
                if overlap is None or len(conflicts) >= 10:
                    continue
                overlap_start, overlap_end = overlap
                conflicts.append(
                    {
                        "kind": "course_overlap",
                        "conflicting_item_key": other_key,
                        "summary": other_title,
                        "start_at": overlap_start.isoformat(),
                        "end_at": overlap_end.isoformat(),
                    }
                )
            intended_intervals.append((logical_key, str(event.title), intervals))
            all_conflicts.extend(
                {"item_key": item["item_key"], **conflict} for conflict in conflicts
            )
            parameters = {
                "calendar_id": calendar_id,
                "logical_key": logical_key,
                "record_id": str(record.id),
                "record_version": record.version,
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
                        "provider_before": deepcopy(link.synced_snapshot) if link else {},
                    }
                )
            operation_type = {
                "create": "calendar_create_event",
                "update": "calendar_update_event",
                "no_op": "calendar_no_op",
            }[effect]
            compiled_item = {
                **item,
                "effect": effect,
                "operation_type": operation_type,
                "parameters": parameters,
                "conflicts": conflicts,
            }
            compiled_item["parameters_sha256"] = sha256_json(parameters)
            compiled.append(compiled_item)

        cancellation_reason = reason or "Meeting removed from the canonical course"
        for logical_key, link in sorted(links.items()):
            if logical_key in desired_keys or not self._link_active(link):
                continue
            target = self._safe_target(
                account_id=account.id,
                calendar_id=calendar_id,
                link=link,
            )
            compiled.append(
                self._cancel_item(
                    record,
                    course,
                    link,
                    calendar_id=calendar_id,
                    target=target,
                    reason=cancellation_reason,
                )
            )

        if mode == "drop" and not compiled:
            item_key = f"course:{record.id}:archive"
            parameters = {
                "calendar_id": calendar_id,
                "logical_key": item_key,
                "record_id": str(record.id),
                "record_version": record.version,
                "meeting_id": None,
                "operation_type": "calendar_no_op",
            }
            compiled.append(
                {
                    "item_key": item_key,
                    "course_code": course.course_code,
                    "section": course.section,
                    "meeting_id": "local archive",
                    "exception_id": None,
                    "effect": "no_op",
                    "operation_type": "calendar_no_op",
                    "event": None,
                    "before": None,
                    "classification": {
                        "recurrence_kind": "one_time",
                        "system_tags": [],
                        "operator_tags": [],
                        "priority": "normal",
                        "priority_basis": "default",
                    },
                    "conflicts": [],
                    "parameters": parameters,
                    "parameters_sha256": sha256_json(parameters),
                }
            )
        return compiled, all_conflicts[:100]

    def _compile_material(
        self,
        *,
        record: Record,
        course: CourseData,
        mode: str,
        account: Account,
        calendar_id: str,
        reminder_plan: dict[str, Any],
        state: CalendarSyncState,
        reason: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        items, conflicts = self._compile_items(
            record=record,
            course=course,
            mode=mode,
            account=account,
            calendar_id=calendar_id,
            reminder_plan=reminder_plan,
            state=state,
            reason=reason,
        )
        counts = {
            key: sum(item["effect"] == key for item in items)
            for key in ("create", "update", "cancel", "no_op")
        }
        action_type = "calendar_drop_course" if mode == "drop" else "calendar_reconcile_course"
        parameters = {
            "calendar_id": calendar_id,
            "record_id": str(record.id),
            "record_version": record.version,
            "mode": mode,
            "reason": reason,
            "reminder_plan": reminder_plan,
            "reminder_plan_sha256": sha256_json(reminder_plan),
            "items": items,
        }
        term = self.session.get(Record, course.term_record_id)
        term_data = TermData.model_validate(term.data) if term is not None else None
        assert state.last_success_at is not None
        preview: dict[str, Any] = {
            "action_type": action_type,
            "target": {
                "account_id": str(account.id),
                "calendar_id": calendar_id,
            },
            "course": {
                "record_id": str(record.id),
                "version": record.version,
                "course_code": course.course_code,
                "section": course.section,
                "course_title": course.course_title,
            },
            "term": (
                {
                    "term_name": term_data.term_name,
                    "institution": term_data.institution,
                    "start_date": (
                        term_data.start_date.isoformat() if term_data.start_date else None
                    ),
                    "end_date": term_data.end_date.isoformat() if term_data.end_date else None,
                    "timezone": term_data.timezone,
                }
                if term_data is not None
                else None
            ),
            "mode": mode,
            "reason": reason,
            "item_count": len(items),
            "counts": counts,
            "items": [
                {
                    "item_key": item["item_key"],
                    "course_code": item["course_code"],
                    "section": item["section"],
                    "meeting_id": item["meeting_id"],
                    "exception_id": item.get("exception_id"),
                    "date_range": item.get("date_range"),
                    "effect": item["effect"],
                    "event": item.get("event"),
                    "before": item.get("before"),
                    "classification": item["classification"],
                    "conflicts": item["conflicts"],
                }
                for item in items
            ],
            "conflicts": conflicts,
            "course_date_ranges": sorted(
                {
                    sha256_json(date_range): date_range
                    for item in items
                    if isinstance(item.get("date_range"), dict)
                    for date_range in [item["date_range"]]
                }.values(),
                key=lambda date_range: (
                    str(date_range.get("start_date")),
                    str(date_range.get("end_date")),
                ),
            ),
            "freshness": {
                "last_success_at": _aware(state.last_success_at).isoformat(),
                "window_start": _aware(state.window_start).isoformat(),
                "window_end": _aware(state.window_end).isoformat(),
            },
        }
        if mode == "sync":
            preview["reminder_plan"] = reminder_plan
        material_fingerprint = sha256_json(
            {
                "action_type": action_type,
                "account_id": str(account.id),
                "calendar_id": calendar_id,
                "record_id": str(record.id),
                "record_version": record.version,
                "mode": mode,
                "reason": reason,
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

    def current_approval_dependency_sha256(
        self,
        *,
        revision_parameters: dict[str, Any],
        record: Record,
        account: Account,
        state: CalendarSyncState,
    ) -> str:
        """Recompile one course against the current cache for approval validation."""

        mode = revision_parameters.get("mode")
        calendar_id = revision_parameters.get("calendar_id")
        reminder_plan = revision_parameters.get("reminder_plan")
        reason = revision_parameters.get("reason")
        if (
            mode not in {"sync", "drop"}
            or not isinstance(calendar_id, str)
            or not isinstance(reminder_plan, dict)
            or (reason is not None and not isinstance(reason, str))
        ):
            raise DocketError(
                code="approval_binding_mismatch",
                message="The course action contains invalid reconciliation parameters.",
            )
        canonical_plan = CalendarReminderPlanInput.model_validate(reminder_plan).model_dump(
            mode="json"
        )
        current_parameters, _, _ = self._compile_material(
            record=record,
            course=CourseData.model_validate(record.data),
            mode=mode,
            account=account,
            calendar_id=calendar_id,
            reminder_plan=canonical_plan,
            state=state,
            reason=reason,
        )
        return course_reconciliation_dependency_sha256(current_parameters)

    def propose(self, request: ProposeCourseReconciliationInput) -> dict[str, Any]:
        """Internal compatibility surface for deterministic approval-path tests."""
        return self._apply(
            request,
            force_decision=True,
            operation_name="docket_propose_course_reconciliation",
        )

    def apply_explicit(self, request: ProposeCourseReconciliationInput) -> dict[str, Any]:
        return self._apply(
            request,
            force_decision=False,
            operation_name="docket_apply_course_intent",
        )

    def _apply(
        self,
        request: ProposeCourseReconciliationInput,
        *,
        force_decision: bool,
        operation_name: str,
    ) -> dict[str, Any]:
        validate_configured_discord_source(self.session, request.source, request.actor_id)
        command, replay = self._start_command(request, operation_name=operation_name)
        if replay is not None:
            return replay
        account, state = self._validate_target(request)
        if not force_decision and self.settings.calendar_write_mode() == "disabled":
            raise DocketError(
                code="external_writes_disabled",
                message="External Calendar writes are disabled.",
            )
        record, course = self._course(request)
        profile = CalendarProfileService(self.session).get()
        plan_model = request.reminder_plan or CalendarReminderPlanInput(
            delivery_channels=profile.default_reminder_delivery_channels,
            lead_seconds=profile.default_reminder_lead_seconds,
        )
        reminder_plan = plan_model.model_dump(mode="json")
        parameters, preview, material_fingerprint = self._compile_material(
            record=record,
            course=course,
            mode=request.mode,
            account=account,
            calendar_id=request.calendar_id,
            reminder_plan=reminder_plan,
            state=state,
            reason=request.reason,
        )
        counts = preview["counts"]
        items = parameters["items"]
        assert isinstance(counts, dict) and isinstance(items, list)
        conflicts = preview["conflicts"]
        assert isinstance(conflicts, list)
        needs_decision = force_decision or bool(conflicts)
        parameters["conflict_resolution"] = None if conflicts else "not_applicable"
        preview["conflict_resolution"] = None if conflicts else "not_applicable"
        if request.mode == "sync" and counts == {
            "create": 0,
            "update": 0,
            "cancel": 0,
            "no_op": len(items),
        }:
            result = {
                "request_id": str(command.id),
                "disposition": "no_op",
                "record_id": str(record.id),
                "record_version": record.version,
                "counts": counts,
            }
            command.status = CommandStatus.SUCCEEDED.value
            command.result = result
            command.completed_at = utc_now()
            self.session.add(
                AuditEvent(
                    event_type="course.reconciliation_noop",
                    entity_type="record",
                    entity_id=record.id,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    request_id=command.id,
                    data={"record_version": record.version, "item_count": len(items)},
                )
            )
            return result

        action_type = str(preview["action_type"])
        parameters_sha256 = sha256_json(parameters)
        preview_sha256 = sha256_json(preview)
        now = utc_now()
        matched = (
            find_materially_identical_pending_proposal(
                self.session,
                category="calendar_course",
                material_fingerprint=material_fingerprint,
                now=now,
            )
            if needs_decision
            else None
        )
        if matched is not None:
            matched_result = matched.model_copy(update={"request_id": command.id})
            payload: dict[str, Any] = matched_result.model_dump(mode="json")
            command.status = CommandStatus.SUCCEEDED.value
            command.result = payload
            command.completed_at = now
            self.session.add(
                AuditEvent(
                    event_type="action.duplicate_suppressed",
                    entity_type="action",
                    entity_id=matched_result.action_id,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    request_id=command.id,
                    data={
                        "material_fingerprint": material_fingerprint,
                        "matched_queue_item_id": str(matched_result.queue_item_id),
                    },
                )
            )
            return payload

        active_action = self.session.scalar(
            select(Action)
            .where(
                Action.record_id == record.id,
                Action.action_type.in_({"calendar_reconcile_course", "calendar_drop_course"}),
                Action.status.in_(
                    {
                        ActionStatus.APPROVAL_PENDING.value,
                        ActionStatus.READY.value,
                        ActionStatus.EXECUTING.value,
                        ActionStatus.RECONCILIATION_REQUIRED.value,
                    }
                ),
            )
            .limit(1)
        )
        if active_action is not None:
            raise DocketError(
                code="course_lifecycle_busy",
                message="The course already has an unfinished Calendar lifecycle action.",
                details={
                    "action_id": str(active_action.id),
                    "status": active_action.status,
                },
            )

        course_label = " ".join(value for value in (course.course_code, course.section) if value)
        queue_item = QueueItem(
            deduplication_key=f"manual_action:{request.request_key}",
            material_fingerprint=material_fingerprint,
            category="calendar_course",
            title=(
                f"{'Drop' if request.mode == 'drop' else 'Sync'} "
                f"{course_label} ({len(items)} changes)"
            )[:512],
            summary=(
                f"{counts['create']} create, {counts['update']} update, "
                f"{counts['cancel']} cancel, {counts['no_op']} unchanged"
            ),
            status=(
                QueueItemStatus.AWAITING_APPROVAL.value
                if needs_decision
                else QueueItemStatus.EXECUTING.value
            ),
            priority="normal",
            presentation=(
                QueuePresentation.CONFLICT_RESOLUTION.value
                if conflicts
                else QueuePresentation.PROPOSAL.value
                if needs_decision
                else QueuePresentation.SUPPRESSED.value
            ),
            received_at=now,
        )
        self.session.add(queue_item)
        self.session.flush()
        action = Action(
            queue_item_id=queue_item.id,
            record_id=record.id,
            action_type=action_type,
            status=(
                ActionStatus.APPROVAL_PENDING.value if needs_decision else ActionStatus.READY.value
            ),
            current_revision=1,
        )
        self.session.add(action)
        self.session.flush()
        assert state.last_success_at is not None
        revision = ActionRevision(
            action_id=action.id,
            revision=1,
            action_type=action_type,
            account_id=account.id,
            parameters=parameters,
            parameters_sha256=parameters_sha256,
            preview=preview,
            preview_sha256=preview_sha256,
            risk_class=get_action_definition(action_type).risk_class.value,
            authority="explicit_user",
            target_versions={
                "queue_item": {"id": str(queue_item.id), "version": queue_item.version},
                "record": {
                    "id": str(record.id),
                    "version": record.version,
                    "status": record.status,
                },
                "calendar_snapshot": {
                    "last_success_at": _aware(state.last_success_at).isoformat(),
                    "course_dependency_sha256": course_reconciliation_dependency_sha256(parameters),
                },
            },
            created_by_actor_type=request.actor_type,
            created_by_actor_id=request.actor_id,
        )
        self.session.add(revision)
        self.session.flush()
        if request.mode == "sync":
            for item in items:
                if item["operation_type"] not in {
                    "calendar_create_event",
                    "calendar_update_event",
                }:
                    continue
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
        if not needs_decision:
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
            for manifest_item in items:
                item_key = str(manifest_item["item_key"])
                item_parameters = dict(manifest_item["parameters"])
                item_parameters["operation_type"] = manifest_item["operation_type"]
                item_parameters_sha256 = sha256_json(item_parameters)
                item_no_op = manifest_item["operation_type"] == "calendar_no_op"
                self.session.add(
                    OperationItem(
                        operation_id=operation.id,
                        item_key=item_key,
                        item_type=str(manifest_item["operation_type"]),
                        idempotency_key=(
                            f"calendar:batch-item:{operation.id}:"
                            f"{item_key}:{item_parameters_sha256}"
                        ),
                        parameters=item_parameters,
                        parameters_sha256=item_parameters_sha256,
                        status="succeeded" if item_no_op else "pending",
                        next_attempt_at=None if item_no_op else now,
                        result={"disposition": "no_op"} if item_no_op else None,
                    )
                )
            all_no_op = all(item["operation_type"] == "calendar_no_op" for item in items)
            if all_no_op:
                operation.status = OperationStatus.SUCCEEDED.value
                operation.next_attempt_at = None
                operation.result = {
                    "item_count": len(items),
                    "counts": {
                        "pending": 0,
                        "running": 0,
                        "succeeded": len(items),
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
                action.status = ActionStatus.SUCCEEDED.value
                queue_item.status = QueueItemStatus.COMPLETED.value
                queue_item.resolved_at = now
                queue_item.resolution_code = (
                    "calendar_course_dropped"
                    if request.mode == "drop"
                    else "calendar_course_synchronized"
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
                        "operation_id": str(operation.id),
                        "item_count": len(items),
                    },
                )
            )
            enqueue_action_system_log(
                self.session,
                action=action,
                revision=revision,
                state="succeeded" if all_no_op else "queued",
                result=operation.result,
            )
            result = {
                "request_id": str(command.id),
                "disposition": "execution_queued" if not all_no_op else "no_op",
                "authority": "explicit_user",
                "record_id": str(record.id),
                "record_version": record.version,
                "queue_item_id": str(queue_item.id),
                "action_id": str(action.id),
                "action_revision_id": str(revision.id),
                "operation_id": str(operation.id),
                "operation_status": operation.status,
                "counts": counts,
                "conflicts": [],
            }
            command.status = CommandStatus.SUCCEEDED.value
            command.result = result
            command.completed_at = now
            return result

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
                    "action_type": action_type,
                    "risk_class": revision.risk_class,
                    "record_version": record.version,
                    "item_count": len(items),
                    "parameters_sha256": parameters_sha256,
                    "preview_sha256": preview_sha256,
                },
            )
        )
        proposal = ProposalResult(
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
        result = proposal.model_dump(mode="json")
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result
        command.completed_at = utc_now()
        return result
