"""Ordinary Task support derived from a named parent, never from task text."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import Task
from docket.schemas.authority import TaskModify, TaskRetract
from docket.schemas.tracked_context import TaskPatchInput


def descendants(session: Session, root_ref: str) -> list[Task]:
    """Return active descendants in parent-first order under the task graph lock."""
    result: list[Task] = []
    visited = {root_ref}
    frontier = [root_ref]
    while frontier:
        rows = list(
            session.scalars(
                select(Task)
                .where(
                    Task.parent_task_ref.in_(frontier),
                    Task.canonical_status == "active",
                )
                .order_by(Task.ref_id)
            )
        )
        frontier = []
        for row in rows:
            if row.ref_id in visited:
                raise DocketError(
                    code="task_parent_cycle", message="Task hierarchy contains a cycle."
                )
            visited.add(row.ref_id)
            frontier.append(row.ref_id)
            result.append(row)
    return result


def child_change(change: Any, child: Task) -> TaskModify | TaskRetract | None:
    """Only lifecycle and list membership propagate; titles and notes never do."""
    common = {
        "change_id": change.change_id,
        "object_type": "task",
        "object_ref": child.ref_id,
        "basis_refs": list(change.basis_refs),
    }
    if change.action == "retract":
        return TaskRetract.model_validate(
            {
                **common,
                "action": "retract",
                "affected_fields": ["canonical_status"],
            }
        )
    if change.action != "update":
        return None
    payload = TaskPatchInput.model_validate(change.payload).model_dump(exclude_unset=True)
    selected = {key: payload[key] for key in ("task_list_ref",) if key in payload}
    state = payload.get("task_state")
    if state in {"completed", "cancelled"}:
        selected["task_state"] = state
        selected["completed_at"] = payload.get("completed_at") if state == "completed" else None
    if payload.get("canonical_status") == "retracted":
        selected["canonical_status"] = "retracted"
    if not selected:
        return None
    return TaskModify.model_validate(
        {
            **common,
            "action": "update",
            "payload": selected,
            "affected_fields": list(selected),
        }
    )


def changed_child(change: Any, child: Task) -> TaskModify | TaskRetract | None:
    derived = child_change(change, child)
    if derived is None or derived.action == "retract":
        return derived
    payload = derived.payload.model_dump(exclude_unset=True)
    # Previously completed children retain their actual recorded completion.
    if child.task_state == "completed" and payload.get("task_state") == "completed":
        payload.pop("task_state")
        payload.pop("completed_at", None)
    payload = {key: value for key, value in payload.items() if getattr(child, key) != value}
    return (
        derived.model_copy(update={"payload": derived.payload.model_validate(payload)})
        if payload
        else None
    )
