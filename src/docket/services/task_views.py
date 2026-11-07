"""Bounded Task views with public identities and lossless detail routes."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.models import (
    GoogleTaskList,
    GoogleTaskObservation,
    ProviderAccount,
    Task,
    TaskConflictResolution,
    TaskProviderBinding,
    TaskSyncConflict,
    TaskSyncState,
)
from docket.services.task_bindings import TaskBindingService
from docket.services.task_item_titles import title_relationship
from docket.services.task_lists import TaskListService
from docket.services.task_reconciliation import observed_values


def iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat() if value else None


def notes_detail(ref: str) -> dict[str, Any]:
    return {"tool": "docket_get_history_entry", "ref": ref, "view": "audit", "text_limit": 4096}


class TaskViewService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def task(self, task: Task, *, with_notes: bool = False) -> dict[str, Any]:
        binding = self.session.scalar(
            select(TaskProviderBinding)
            .where(
                TaskProviderBinding.task_ref == task.ref_id,
            )
            .order_by(TaskProviderBinding.active.desc(), TaskProviderBinding.created_at.desc())
            .limit(1)
        )
        result = {
            "title": task.title,
            "item_ref": task.item_ref,
            "state": task.task_state,
            "priority": task.priority,
            "canonical_status": task.canonical_status,
            "task_list_ref": task.task_list_ref,
            "scheduled_date": task.scheduled_date.isoformat() if task.scheduled_date else None,
            "parent_task_ref": task.parent_task_ref,
            "completed_at": iso(task.completed_at),
            "remind_at": iso(task.remind_at),
            "timezone": task.reminder_timezone,
            "item_title_relationship": title_relationship(self.session, task.item_ref),
            "sync": TaskBindingService.status(binding) if binding else {"state": "local_only"},
            "version": task.version,
        }
        from docket.services.task_reminders import TaskReminderService
        from docket.services.task_title_labels import task_time_header

        if task.reminder_generation:
            result["reminder"] = TaskReminderService(self.session).status(task)

        header, omitted = task_time_header(task)
        if header:
            result["google_time_header"] = header.rstrip(" —")
        elif omitted:
            result["google_time_header_omitted"] = "title_limit"
        if task.description:
            if with_notes and len(task.description.encode()) <= 1024:
                result["notes"] = task.description
            else:
                result["notes_detail"] = notes_detail(task.ref_id)
        return result

    def listing(self, row: GoogleTaskList) -> dict[str, Any]:
        state = self.session.get(TaskSyncState, row.ref_id)
        account = self.session.get(ProviderAccount, row.account_id)
        return {
            "ref": row.ref_id,
            "title": row.title,
            "version": row.version,
            "account_ref": account.ref_id if account else None,
            "mode": row.policy_mode,
            "is_default": row.is_default,
            "reminder_calendar_ref": row.reminder_calendar_ref,
            "available": row.available,
            "lifecycle": TaskListService.lifecycle(row.policy_mode),
            "freshness": {
                "last_success_at": iso(state.last_success_at) if state else None,
                "complete": bool(state and state.status == "current"),
                "last_error": state.last_error_code if state else None,
                "refresh_pending": bool(state and (state.requested or state.lease_token)),
            },
        }

    def observation(self, row: GoogleTaskObservation) -> dict[str, Any]:
        binding = TaskBindingService(self.session).identity(row.task_list_ref, row.provider_task_id)
        result: dict[str, Any] = {
            "ref": row.ref_id,
            "task_list_ref": row.task_list_ref,
            "observed_at": iso(row.observed_at),
            "assigned": bool(row.fields.get("assignmentInfo")),
            "hidden": bool(row.fields.get("hidden")),
            "source": "untrusted_google_task",
            "linked_task_ref": binding.task_ref if binding else None,
        }
        if binding is None:
            from docket.providers.google.tasks import creation_correlation

            notes = row.fields.get("notes")
            correlation = creation_correlation(notes) if isinstance(notes, str) else None
            pending = (
                self.session.scalar(
                    select(TaskProviderBinding).where(
                        TaskProviderBinding.task_list_ref == row.task_list_ref,
                        TaskProviderBinding.provider_task_id.is_(None),
                        TaskProviderBinding.correlation == correlation,
                    )
                )
                if correlation
                else None
            )
            if pending is not None:
                result["pending_creation_task_ref"] = pending.task_ref
                result["pending_creation_binding_ref"] = pending.ref_id
        if binding is not None:
            result["sync"] = TaskBindingService.status(binding)
        from docket.domain.errors import DocketError

        try:
            values, _marker = observed_values(self.session, row, binding)
        except DocketError as exc:
            title = row.fields.get("title")
            result.update(
                title=title[:256] if isinstance(title, str) else None,
                limitation=exc.code,
                observed_fields_detail=notes_detail(row.ref_id),
            )
        else:
            result.update({key: value for key, value in values.items() if key != "notes"})
            if values.get("notes"):
                result["notes_detail"] = notes_detail(row.ref_id)
        return result

    def conflict(self, row: TaskSyncConflict) -> dict[str, Any]:
        binding = self.session.get(TaskProviderBinding, row.binding_ref)
        resolved = set(
            self.session.scalars(
                select(TaskConflictResolution.field_name).where(
                    TaskConflictResolution.conflict_ref == row.ref_id,
                )
            )
        )
        remaining = [key for key in row.fields if key not in resolved]
        values = {}
        for key in remaining:
            choices = {"docket": row.local_values.get(key), "google": row.google_values.get(key)}
            for side, value in choices.items():
                if isinstance(value, str) and len(value.encode()) > 512:
                    choices[side] = {
                        "detail_ref": binding.task_ref
                        if side == "docket" and binding
                        else row.observation_ref,
                        "field": key,
                        "characters": len(value),
                    }
            values[key] = choices
        return {
            "ref": row.ref_id,
            "version": row.version,
            "task_ref": binding.task_ref if binding else None,
            "binding_ref": row.binding_ref,
            "observation_ref": row.observation_ref,
            "fields": remaining,
            "choices": values,
            "state": (
                "resolved"
                if not remaining
                else "superseded"
                if binding and binding.observation_ref != row.observation_ref
                else "open"
            ),
            "resolved_fields": sorted(resolved),
        }
