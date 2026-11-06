"""Identify owned reminder markers using retained local identity, never labels."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import sha256_json
from docket.models import (
    CalendarEventCache,
    ExecutionAttempt,
    Operation,
    OperationTarget,
    ProviderEventBinding,
    TaskReminderGeneration,
    TemporalBinding,
    TemporalCalendarProjection,
)


@dataclass(frozen=True)
class TaskReminderOrigin:
    task_ref: str
    projection_ref: str


def task_reminder_origins(
    session: Session, account_id: uuid.UUID, calendar_id: str,
) -> dict[str, TaskReminderOrigin]:
    """Scope exact reserved event IDs to a genuine generation and sent effect.

    Calendar clients can copy or remove private properties. Neither operation
    changes this local identity. Retired generations stay recognizable while
    cancellation or uncertain creation is being reconciled.
    """
    result: dict[str, TaskReminderOrigin] = {}
    for generation, projection, time, target, operation in session.execute(
        select(TaskReminderGeneration, TemporalCalendarProjection, TemporalBinding,
               OperationTarget, Operation)
        .join(TemporalCalendarProjection,
              TemporalCalendarProjection.ref_id == TaskReminderGeneration.projection_ref)
        .join(TemporalBinding,
              TemporalBinding.ref_id == TaskReminderGeneration.temporal_binding_ref)
        .join(OperationTarget,
              OperationTarget.canonical_target_ref == TaskReminderGeneration.projection_ref)
        .join(Operation, Operation.id == OperationTarget.operation_id)
        .where(Operation.account_id == account_id,
               Operation.operation_type == "calendar_create_event",
               OperationTarget.target_kind == "temporal_projection")
    ):
        parameters = target.parameters
        if (
            generation.created_by_changeset_ref != operation.originating_changeset_ref
            or projection.temporal_binding_ref != time.ref_id
            or projection.reminder_plan_ref != generation.plan_ref
            or time.subject_ref != generation.task_ref
            or parameters.get("task_reminder_generation_ref") != generation.ref_id
            or parameters.get("calendar_id") != calendar_id
            or parameters.get("external_event_id") != operation.id.hex
            or sha256_json(parameters) != target.parameters_sha256
        ):
            continue
        event_id = operation.id.hex
        binding = session.scalar(select(ProviderEventBinding.id).where(
            ProviderEventBinding.account_id == account_id,
            ProviderEventBinding.calendar_id == calendar_id,
            ProviderEventBinding.provider_event_id == event_id,
            ProviderEventBinding.canonical_target_ref == projection.ref_id,
            ProviderEventBinding.target_kind == "temporal_projection",
        ))
        if binding is None and session.scalar(select(ExecutionAttempt.id).where(
            ExecutionAttempt.operation_id == operation.id,
            ExecutionAttempt.provider_request_id.is_not(None),
        ).limit(1)) is None:
            continue
        result[event_id] = TaskReminderOrigin(generation.task_ref, projection.ref_id)
    return result


def stamp_task_reminder_origin(
    row: CalendarEventCache, origins: dict[str, TaskReminderOrigin],
) -> None:
    origin = origins.get(row.provider_event_id)
    row.origin_kind = "task_reminder" if origin else None
    row.origin_owner_ref = origin.task_ref if origin else None
    row.origin_projection_ref = origin.projection_ref if origin else None
