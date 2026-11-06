"""Deterministic Task field comparisons; provider text never supplies authority."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import DocketError
from docket.models import GoogleTaskObservation, Task, TaskProviderBinding
from docket.providers.google.tasks import canonical_notes

SYNC_FIELDS = ("title", "notes", "scheduled_date", "status", "completed_at", "parent", "deletion")


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A provider instant requires its UTC offset")
    return parsed.astimezone(UTC)


def transport_fields_match(
    actual: dict[str, Any], requested: dict[str, Any], correlation: str,
) -> bool:
    """Compare supported request fields; note markers do not become user notes."""
    for key, expected in requested.items():
        value = actual.get(key)
        if key == "notes":
            value, _ = canonical_notes(value or "", correlation)
            expected, _ = canonical_notes(expected or "", correlation)
        if key == "due" and value and expected:
            value, expected = value[:10], expected[:10]
        if key == "completed" and value and expected:
            try:
                value, expected = instant(value), instant(expected)
            except ValueError:
                return False
        if value != expected:
            return False
    return not actual.get("deleted")


def canonical_values(task: Task) -> dict[str, Any]:
    completed = task.completed_at
    return {
        "title": task.title,
        "notes": task.description or "",
        "scheduled_date": task.scheduled_date.isoformat() if task.scheduled_date else None,
        "status": "completed" if task.task_state == "completed" else "needsAction",
        "completed_at": (
            completed.replace(tzinfo=completed.tzinfo or UTC).astimezone(UTC).isoformat()
            if completed is not None
            else None
        ),
        "parent": task.parent_task_ref,
        "deletion": task.canonical_status == "retracted",
    }


def observed_values(
    session: Session,
    observation: GoogleTaskObservation,
    binding: TaskProviderBinding | None,
) -> tuple[dict[str, Any], bool]:
    fields = observation.fields
    if fields.get("deleted") is True:
        before = binding.provider_snapshot if binding is not None else {}
        return {**before, "deletion": True}, bool(binding and binding.marker_present)
    title = fields.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= 1024:
        raise DocketError(
            code="google_task_invalid_title", message="The observed task needs a valid title."
        )
    notes = fields.get("notes") or ""
    if not isinstance(notes, str):
        raise DocketError(
            code="google_task_invalid_notes", message="The task notes could not be read."
        )
    marker = False
    if binding is not None:
        notes, marker = canonical_notes(notes, binding.correlation)
    status = fields.get("status", "needsAction")
    if status not in {"needsAction", "completed"}:
        raise DocketError(
            code="google_task_invalid_state", message="The task has an unsupported state."
        )
    due = fields.get("due")
    scheduled = None
    completed = None
    try:
        if due is not None:
            scheduled = date.fromisoformat(str(due)[:10]).isoformat()
        if status == "completed":
            if not isinstance(fields.get("completed"), str):
                raise ValueError("Missing completion evidence")
            completed = instant(fields["completed"]).isoformat()
    except ValueError as exc:
        raise DocketError(
            code="google_task_invalid_time",
            message="The task's date or completion evidence is incomplete.",
        ) from exc
    parent_ref = None
    if fields.get("parent"):
        parent_ref = session.scalar(
            select(TaskProviderBinding.task_ref).where(
                TaskProviderBinding.task_list_ref == observation.task_list_ref,
                TaskProviderBinding.provider_task_id == fields["parent"],
                TaskProviderBinding.active.is_(True),
                TaskProviderBinding.detached.is_(False),
            )
        )
        if parent_ref is None:
            raise DocketError(
                code="google_task_parent_pending", message="The parent task is not linked yet."
            )
    return {
        "title": title,
        "notes": notes,
        "scheduled_date": scheduled,
        "status": status,
        "completed_at": completed,
        "parent": parent_ref,
        "deletion": False,
    }, marker


@dataclass
class FieldMerge:
    baseline: dict[str, Any]
    inbound: dict[str, Any] = field(default_factory=dict)
    outbound: dict[str, Any] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def merge_fields(
    baseline: dict[str, Any],
    local: dict[str, Any],
    google: dict[str, Any],
    provider_before: dict[str, Any],
    *,
    limited: set[str] | None = None,
) -> FieldMerge:
    """Merge each supported field without converting unprojected notes into a clear."""
    result = FieldMerge(dict(baseline))
    limited = limited or set()
    for key in SYNC_FIELDS:
        if key not in google or key not in local:
            continue
        left, right = local[key], google[key]
        if left == right:
            result.baseline[key] = left
            continue
        if key not in baseline:
            if key in provider_before and right == provider_before[key]:
                if key not in limited:
                    result.outbound[key] = left
            else:
                result.conflicts.append(key)
            continue
        base = baseline[key]
        if left == base:
            result.inbound[key] = right
            result.baseline[key] = right
        elif right == base:
            if key not in limited:
                result.outbound[key] = left
        else:
            result.conflicts.append(key)
    return result


def task_patch(values: dict[str, Any], current: Task | None = None) -> dict[str, Any]:
    patch = {
        {"notes": "description", "status": "task_state", "parent": "parent_task_ref"}.get(
            key, key
        ): value
        for key, value in values.items()
        if key not in {"deletion", "completed_at"}
    }
    if "status" in values:
        status = values["status"]
        patch["task_state"] = "completed" if status == "completed" else "not_started"
        if status == "completed":
            patch["completed_at"] = values.get("completed_at") or (
                current.completed_at
                if current is not None and current.task_state == "completed"
                else None
            )
            if patch["completed_at"] is None:
                raise DocketError(
                    code="google_task_completion_incomplete", message="Retain completion evidence."
                )
        else:
            patch["completed_at"] = None
    elif "completed_at" in values:
        if current is not None and current.task_state == "completed":
            patch["completed_at"] = values["completed_at"]
    return patch
