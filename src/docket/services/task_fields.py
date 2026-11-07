"""Canonical task scheduling and hierarchy, independent of provider availability."""

from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.models import GoogleTaskList, Task
from docket.schemas.tracked_context import TaskInput, TaskPatchInput
from docket.services.work_attribution import read_operator


class TaskFieldService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self, ref: str) -> GoogleTaskList:
        row = self.session.get(GoogleTaskList, ref)
        if row is None or row.operator_ref != read_operator():
            raise DocketError(
                code="task_list_unavailable", message="Select one of your task lists."
            )
        return row

    def related_task(self, ref: str) -> Task:
        row = self.session.scalar(select(Task).where(Task.ref_id == ref))
        if row is None or row.canonical_status != "active":
            raise DocketError(code="task_unavailable", message="Select an active canonical task.")
        return row

    def values(
        self,
        spec: TaskInput | TaskPatchInput,
        current: Task | None,
    ) -> tuple[dict[str, Any], str | None]:
        fields = spec.model_dump(exclude_unset=current is not None)
        after = fields.pop("after_task_ref", None)
        list_ref = fields.get("task_list_ref", current.task_list_ref if current else None)
        parent_ref = fields.get("parent_task_ref", current.parent_task_ref if current else None)
        if current is None and parent_ref and not list_ref:
            list_ref = self.related_task(parent_ref).task_list_ref
            fields["task_list_ref"] = list_ref
        if list_ref:
            self.list(list_ref)
        if parent_ref and (current is None or {"parent_task_ref", "task_list_ref"}.intersection(
            fields,
        )):
            parent = self.related_task(parent_ref)
            if parent.task_list_ref != list_ref:
                raise DocketError(
                    code="task_parent_list_mismatch",
                    message=(
                        "A task and its parent must use the same list. Select or clear its parent."
                    ),
                    details={"field": "parent_task_ref"},
                )
            visited = {current.ref_id} if current else set()
            while parent_ref:
                if parent_ref in visited:
                    raise DocketError(
                        code="task_parent_cycle", message="A task cannot contain itself."
                    )
                visited.add(parent_ref)
                parent_ref = self.related_task(parent_ref).parent_task_ref
        parent_ref = fields.get("parent_task_ref", current.parent_task_ref if current else None)
        if after:
            sibling = self.related_task(after)
            if (
                (current is not None and sibling.ref_id == current.ref_id)
                or sibling.task_list_ref != list_ref
                or sibling.parent_task_ref != parent_ref
            ):
                raise DocketError(
                    code="task_sibling_mismatch",
                    message="Select a sibling in the same list and parent.",
                    details={"field": "after_task_ref"},
                )
        if "remind_at" in fields:
            if fields["remind_at"] is None:
                fields["reminder_timezone"] = None
            else:
                zone = (
                    fields.get("reminder_timezone")
                    or (current.reminder_timezone if current else None)
                    or get_settings().timezone
                )
                try:
                    ZoneInfo(zone)
                except (ValueError, ZoneInfoNotFoundError) as exc:
                    raise DocketError(
                        code="invalid_timezone",
                        message="Use an IANA timezone for the reminder.",
                        details={"field": "timezone"},
                    ) from exc
                fields["reminder_timezone"] = zone
                fields["remind_at"] = fields["remind_at"].astimezone(UTC)
        state = fields.get("task_state", current.task_state if current else "not_started")
        status = fields.get("canonical_status", current.canonical_status if current else "active")
        if state in {"completed", "cancelled"} or status != "active":
            fields.update(remind_at=None, reminder_timezone=None)
        return fields, after
