"""Deterministic, generation-owned Calendar effects of canonical Task reminders."""

import uuid
from datetime import UTC, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    AuditEvent,
    CalendarLane,
    ChangeSet,
    ExecutionAttempt,
    GoogleTaskList,
    IntentSession,
    Operation,
    OperationTarget,
    ProviderAccount,
    ProviderEventBinding,
    ReminderPlan,
    Task,
    TaskPolicyEffect,
    TaskReminderGeneration,
    TemporalBinding,
    TemporalCalendarProjection,
)
from docket.models.base import utc_now
from docket.schemas.authority import ChangeSetContent, ProviderIntentInput
from docket.schemas.tracked_context import DateTimeTemporalValue
from docket.services.provider_intents import ProviderIntentService


def guard_owned_reminder(session: Session, change: Any) -> None:
    """Generic Time/plan/projection commands must use the owning Task instead."""
    field = {
        "temporal_binding": TaskReminderGeneration.temporal_binding_ref,
        "temporal_calendar_projection": TaskReminderGeneration.projection_ref,
        "reminder_plan": TaskReminderGeneration.plan_ref,
    }.get(change.object_type)
    if field is None:
        return
    refs = [change.object_ref] if change.object_ref else []
    spec = change.create_spec or change.payload
    if str(getattr(spec, "binding_key", "")).startswith("task-reminder:"):
        raise DocketError(
            code="task_reminder_owned",
            message="Use remind_at on a Task for reminder markers.",
            details={"next_action": "docket_update_task"},
        )
    related = getattr(spec, "temporal_binding_ref", None) or getattr(spec, "subject_ref", None)
    conditions = [field.in_(refs)]
    if related:
        conditions.append(TaskReminderGeneration.temporal_binding_ref == related)
    from sqlalchemy import or_

    generation = session.scalar(select(TaskReminderGeneration).where(or_(*conditions)))
    if generation is not None:
        raise DocketError(
            code="task_reminder_owned",
            message="Change or clear remind_at on the owning Task.",
            details={"task_ref": generation.task_ref, "next_action": "docket_update_task"},
        )


class TaskReminderService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self, task: Task) -> TaskReminderGeneration | None:
        return self.session.scalar(
            select(TaskReminderGeneration).where(
                TaskReminderGeneration.task_ref == task.ref_id,
                TaskReminderGeneration.generation == task.reminder_generation,
            )
        )

    def status(self, task: Task) -> dict[str, Any]:
        generation = self.current(task)
        if generation is None:
            return {"state": "unset"}
        projection = self.session.scalar(
            select(TemporalCalendarProjection).where(
                TemporalCalendarProjection.ref_id == generation.projection_ref,
            )
        )
        operation = self.session.scalar(
            select(Operation)
            .join(OperationTarget)
            .where(
                OperationTarget.canonical_target_ref == generation.projection_ref,
            )
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
        binding = self.session.scalar(
            select(ProviderEventBinding).where(
                ProviderEventBinding.canonical_target_ref == generation.projection_ref,
            )
        )
        return {
            "state": "set" if task.remind_at is not None else "cleared",
            "generation": generation.generation,
            "calendar_ref": projection.lane_ref if projection else None,
            "delivery": operation.status if operation else "not_queued",
            "operation_ref": operation.ref_id if operation else None,
            "provider_binding": binding.status if binding else "unconfirmed",
        }

    def route(self, task: Task, previous: TaskReminderGeneration | None) -> CalendarLane:
        lane_ref: str | None
        if previous is not None:
            projection = self.session.scalar(
                select(TemporalCalendarProjection).where(
                    TemporalCalendarProjection.ref_id == previous.projection_ref,
                )
            )
            assert projection is not None
            lane_ref = projection.lane_ref
        else:
            listing = (
                self.session.get(GoogleTaskList, task.task_list_ref) if task.task_list_ref else None
            )
            lane_ref = listing.reminder_calendar_ref if listing else None
            if lane_ref is None:
                from docket.services.policies import LaneRoutingService

                route = LaneRoutingService(self.session).resolve(semantic_classes={"task_reminder"})
                lane_ref = route.get("lane_ref")
        lane = (
            self.session.scalar(
                select(CalendarLane).where(
                    CalendarLane.ref_id == lane_ref,
                )
            )
            if lane_ref
            else None
        )
        account = self.session.get(ProviderAccount, lane.account_id) if lane else None
        if (
            lane is None
            or not lane.enabled
            or lane.status != "active"
            or not lane.calendar_id
            or account is None
            or not account.enabled
            or "google_calendar" not in account.capabilities
        ):
            raise DocketError(
                code="task_reminder_calendar_required",
                message="Configure one reminder Calendar on the task list or Calendar preference.",
                details={"field": "remind_at", "next_action": "docket_configure_task_list"},
            )
        return lane

    def _supersede(
        self, operation: Operation, target: OperationTarget, changeset: ChangeSet | None = None
    ) -> None:
        operation.status = target.status = "superseded"
        operation.next_attempt_at = target.next_attempt_at = None
        operation.result = target.result = {
            "disposition": "superseded",
            "superseded_before_next_call": True,
            **({"by_changeset_ref": changeset.ref_id} if changeset else {}),
        }
        self.session.add(
            AuditEvent(
                event_type="operation.superseded",
                actor_type="system",
                actor_id="task_reminder_projection",
                primary_ref=operation.ref_id,
                affected_refs=[operation.ref_id, target.canonical_target_ref],
                basis_refs=[changeset.ref_id if changeset else operation.originating_changeset_ref],
                data={"reason": "task_reminder_retired", "superseded_before_next_call": True},
            )
        )

    def retire(self, generation: TaskReminderGeneration, changeset: ChangeSet) -> list[Operation]:
        projection = self.session.scalar(
            select(TemporalCalendarProjection).where(
                TemporalCalendarProjection.ref_id == generation.projection_ref,
            )
        )
        assert projection is not None
        if not projection.enabled:
            return []
        projection.enabled = False
        projection.version += 1
        plan = self.session.scalar(
            select(ReminderPlan).where(
                ReminderPlan.ref_id == generation.plan_ref,
            )
        )
        time = self.session.scalar(
            select(TemporalBinding).where(
                TemporalBinding.ref_id == generation.temporal_binding_ref,
            )
        )
        assert time is not None and plan is not None
        plan.canonical_status = time.canonical_status = "retracted"
        plan.version += 1
        time.version += 1
        self.session.add(
            AuditEvent(
                event_type="task_reminder.generation_retired",
                actor_type="system",
                actor_id="task_reminder_projection",
                primary_ref=generation.task_ref,
                affected_refs=[generation.ref_id, projection.ref_id, plan.ref_id, time.ref_id],
                basis_refs=[changeset.ref_id],
                data={"generation": generation.generation},
            )
        )
        queued = []
        for creation, target in self.session.execute(
            select(Operation, OperationTarget)
            .join(OperationTarget)
            .where(
                Operation.operation_type == "calendar_create_event",
                OperationTarget.canonical_target_ref == projection.ref_id,
            )
            .with_for_update(of=Operation)
        ):
            uncertain = (
                self.session.scalar(
                    select(ExecutionAttempt.id)
                    .where(
                        ExecutionAttempt.operation_id == creation.id,
                        ExecutionAttempt.status == "unknown",
                    )
                    .limit(1)
                )
                is not None
            )
            if creation.status == "pending" and not uncertain:
                self._supersede(creation, target, changeset)
                continue
            if creation.status in {"failed", "superseded"} and not uncertain:
                continue
            if creation.status in {"pending", "failed", "superseded"} and uncertain:
                creation.status = target.status = "reconciliation_required"
            key = f"task-reminder:{generation.ref_id}:cancel"
            existing = self.session.scalar(
                select(Operation).where(Operation.idempotency_key == key)
            )
            if existing is not None:
                queued.append(existing)
                continue
            cancellation = Operation(
                originating_changeset_ref=changeset.ref_id,
                basis_refs=[changeset.ref_id],
                canonical_target_refs=[projection.ref_id],
                idempotency_key=key,
                operation_type="calendar_cancel_event",
                account_id=creation.account_id,
                predecessor_operation_id=creation.id,
                provider_correlation=str(uuid.uuid4()),
            )
            self.session.add(cancellation)
            self.session.flush()
            parameters = {
                **target.parameters,
                "reason": "task_reminder_retired",
                "creation_operation_ref": creation.ref_id,
            }
            self.session.add(
                OperationTarget(
                    operation_id=cancellation.id,
                    target_key=projection.ref_id,
                    canonical_target_ref=projection.ref_id,
                    target_kind="temporal_projection",
                    idempotency_key=key + ":target",
                    parameters=parameters,
                    parameters_sha256=sha256_json(parameters),
                )
            )
            self.session.add(
                AuditEvent(
                    event_type="operation.created",
                    actor_type="system",
                    actor_id="task_reminder_projection",
                    primary_ref=cancellation.ref_id,
                    affected_refs=[cancellation.ref_id, generation.task_ref, projection.ref_id],
                    basis_refs=[changeset.ref_id],
                    data={"operation_type": "calendar_cancel_event"},
                )
            )
            queued.append(cancellation)
        self.session.flush()
        return queued

    def materialize(
        self,
        changeset: ChangeSet,
        content: ChangeSetContent,
        refs_by_change: dict[str, list[str]],
    ) -> list[Operation]:
        from docket.services.compensation_policies import instant

        queued = []
        intent = self.session.get(IntentSession, changeset.intent_session_id)
        effect = (
            self.session.get(TaskPolicyEffect, intent.source_policy_effect_ref)
            if intent is not None and intent.source_policy_effect_ref
            else None
        )
        explicit_clocks = (
            {
                ref
                for change in content.tracked_context_changes
                if change.object_type == "task"
                and (spec := change.create_spec or change.payload) is not None
                and "remind_at" in spec.model_fields_set
                for ref in refs_by_change.get(change.change_id, [])
            }
            if intent is not None and intent.source_request_ref
            else set()
        )
        task_refs = {
            ref
            for change in content.tracked_context_changes
            if change.object_type in {"task", "task_sync_resolution"}
            for ref in refs_by_change.get(change.change_id, [])
            if ref.startswith("task_")
        }
        for ref in sorted(task_refs):
            task = self.session.scalar(select(Task).where(Task.ref_id == ref).with_for_update())
            assert task is not None
            old = self.current(task)
            active = (
                task.canonical_status == "active"
                and task.task_state
                not in {
                    "completed",
                    "cancelled",
                }
                and task.remind_at is not None
            )
            if old is not None and active:
                assert task.remind_at is not None
                time = self.session.scalar(
                    select(TemporalBinding).where(
                        TemporalBinding.ref_id == old.temporal_binding_ref,
                    )
                )
                projection = self.session.scalar(
                    select(TemporalCalendarProjection).where(
                        TemporalCalendarProjection.ref_id == old.projection_ref,
                    )
                )
                assert time is not None and projection is not None
                value = DateTimeTemporalValue.model_validate(time.temporal_value)
                previous_instant = value.local_datetime.replace(
                    tzinfo=ZoneInfo(value.timezone),
                    fold=value.fold or 0,
                ).astimezone(UTC)
                title = self.session.scalar(
                    select(
                        OperationTarget.parameters["event"]["title"].as_string(),
                    )
                    .join(Operation)
                    .where(
                        OperationTarget.canonical_target_ref == old.projection_ref,
                        Operation.operation_type == "calendar_create_event",
                    )
                    .limit(1)
                )
                if (
                    projection.enabled
                    and previous_instant == instant(task.remind_at)
                    and value.timezone == task.reminder_timezone
                    and title == ("Task reminder: " + task.title)[:512]
                    and (
                        ref not in explicit_clocks
                        or (
                            self.route(task, None).ref_id == projection.lane_ref
                            and self.session.scalar(
                                select(ProviderEventBinding.id).where(
                                    ProviderEventBinding.canonical_target_ref == old.projection_ref,
                                    ProviderEventBinding.status == "diverged",
                                )
                            )
                            is None
                        )
                    )
                    and not (
                        effect is not None
                        and effect.reminder_generation_ref == old.ref_id
                        and (effect.reminder_observation or {}).get("repair_required")
                    )
                ):
                    continue
            lane = self.route(task, None if ref in explicit_clocks else old) if active else None
            if old is not None:
                queued.extend(self.retire(old, changeset))
            if not active:
                continue
            assert lane is not None and task.remind_at is not None and task.reminder_timezone
            task.reminder_generation += 1
            basis = [changeset.ref_id]
            local = instant(task.remind_at).astimezone(ZoneInfo(task.reminder_timezone))
            time = TemporalBinding(
                subject_ref=task.ref_id,
                role="expected_at",
                binding_key=f"task-reminder:{task.reminder_generation}",
                temporal_value=DateTimeTemporalValue(
                    kind="datetime",
                    local_datetime=local.replace(tzinfo=None),
                    timezone=task.reminder_timezone,
                    fold=local.fold,
                ).model_dump(mode="json"),
                basis_refs=basis,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(time)
            self.session.flush()
            plan = ReminderPlan(
                subject_ref=time.ref_id,
                delivery_channels=["google_popup"],
                lead_seconds=[0],
                basis_refs=basis,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(plan)
            self.session.flush()
            projection = TemporalCalendarProjection(
                temporal_binding_ref=time.ref_id,
                lane_ref=lane.ref_id,
                reminder_plan_ref=plan.ref_id,
                display_policy={
                    "kind": "timed_marker",
                    "duration_seconds": 60,
                    "transparency": "transparent",
                },
                basis_refs=basis,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(projection)
            self.session.flush()
            generation = TaskReminderGeneration(
                task_ref=task.ref_id,
                generation=task.reminder_generation,
                task_version=task.version,
                temporal_binding_ref=time.ref_id,
                projection_ref=projection.ref_id,
                plan_ref=plan.ref_id,
                created_by_changeset_ref=changeset.ref_id,
            )
            self.session.add(generation)
            self.session.flush()
            from docket.services.task_reminder_inbound import retain_authority

            retain_authority(self.session, generation, changeset, old)
            account = self.session.get(ProviderAccount, lane.account_id)
            assert account is not None
            refs = ProviderIntentService(self.session).materialize(
                self.session,
                changeset,
                ProviderIntentInput(
                    intent_id=f"task-reminder-{task.reminder_generation}",
                    operation_type="calendar_create_event",
                    account_ref=account.ref_id,
                    canonical_target_refs=[projection.ref_id, lane.ref_id],
                    basis_refs=basis,
                    idempotency_key=f"task-reminder:{generation.ref_id}:create",
                    parameters={},
                ),
                {},
            )
            queued.extend(self.session.scalars(select(Operation).where(Operation.ref_id.in_(refs))))
            self.session.add(
                AuditEvent(
                    event_type="task_reminder.generation_created",
                    actor_type="system",
                    actor_id="task_reminder_projection",
                    primary_ref=task.ref_id,
                    affected_refs=[
                        task.ref_id,
                        generation.ref_id,
                        time.ref_id,
                        projection.ref_id,
                        plan.ref_id,
                    ],
                    basis_refs=basis,
                    data={"generation": generation.generation},
                )
            )
        return queued

    def generation(self, parameters: dict[str, Any]) -> TaskReminderGeneration | None:
        ref = parameters.get("task_reminder_generation_ref")
        return self.session.get(TaskReminderGeneration, ref) if ref else None

    def retired(self, generation: TaskReminderGeneration) -> bool:
        task = self.session.scalar(select(Task).where(Task.ref_id == generation.task_ref))
        projection = self.session.scalar(
            select(TemporalCalendarProjection).where(
                TemporalCalendarProjection.ref_id == generation.projection_ref,
            )
        )
        return (
            task is None
            or projection is None
            or not projection.enabled
            or task.canonical_status != "active"
            or task.remind_at is None
            or task.task_state in {"completed", "cancelled"}
            or task.reminder_generation != generation.generation
        )

    def prepare(self, operation: Operation, target: OperationTarget) -> bool:
        generation = self.generation(target.parameters)
        if generation is None:
            if target.parameters.get("task_reminder_generation_ref"):
                raise DocketError(
                    code="task_reminder_generation_missing",
                    message="The retained reminder generation is unavailable.",
                )
            return True
        if operation.operation_type == "calendar_create_event" and self.retired(generation):
            uncertain = (
                self.session.scalar(
                    select(ExecutionAttempt.id)
                    .where(
                        ExecutionAttempt.operation_id == operation.id,
                        ExecutionAttempt.status == "unknown",
                    )
                    .limit(1)
                )
                is not None
            )
            if uncertain:
                operation.status = target.status = "reconciliation_required"
                operation.next_attempt_at = target.next_attempt_at = utc_now() + timedelta(
                    seconds=30
                )
            else:
                self._supersede(operation, target)
                for child, child_target in self.session.execute(
                    select(Operation, OperationTarget)
                    .join(OperationTarget)
                    .where(
                        Operation.predecessor_operation_id == operation.id,
                        Operation.operation_type == "calendar_cancel_event",
                        Operation.status == "pending",
                    )
                ):
                    self._supersede(child, child_target)
            return False
        if operation.operation_type == "calendar_cancel_event":
            binding = self.session.scalar(
                select(ProviderEventBinding).where(
                    ProviderEventBinding.account_id == operation.account_id,
                    ProviderEventBinding.calendar_id == target.parameters["calendar_id"],
                    ProviderEventBinding.provider_event_id
                    == target.parameters["external_event_id"],
                    ProviderEventBinding.canonical_target_ref == generation.projection_ref,
                )
            )
            sent_before = self.session.scalar(select(ExecutionAttempt.id).where(
                ExecutionAttempt.operation_id == operation.id,
                ExecutionAttempt.provider_request_id.is_not(None),
            ).limit(1)) is not None
            if binding is not None and not sent_before:
                # Resolve a newly created marker before its first cancellation.
                # Later attempts retain the pin used before the unknown outcome.
                target.parameters = {
                    **target.parameters,
                    "provider_etag": binding.provider_etag,
                    "provider_before": dict(binding.provider_snapshot),
                }
                target.parameters_sha256 = sha256_json(target.parameters)
        return True
