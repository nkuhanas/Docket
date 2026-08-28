from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.enums import OutboxStatus
from docket.models import Action, ActionRevision, OutboxEvent
from docket.models.base import utc_now

_ACTION_LABELS = {
    "calendar_create_event": "Create event",
    "calendar_update_event": "Update event",
    "calendar_update_reminders": "Update reminders",
    "calendar_configure_lane": "Configure Calendar lane",
    "calendar_move_events": "Move events between Calendar lanes",
    "calendar_delete_lane": "Delete Calendar lane",
    "calendar_cancel_event": "Cancel event",
    "calendar_reconcile_course": "Synchronize course",
    "calendar_drop_course": "Drop course",
    "gmail_archive_message": "Archive message",
    "gmail_mark_read": "Mark message as read",
}
_STATE_TITLES = {
    "queued": "Calendar change queued",
    "succeeded": "Calendar change completed",
    "rejected": "Calendar change rejected",
    "failed": "Calendar change failed",
    "partial_failed": "Calendar batch partially completed",
    "reconciliation_required": "Calendar result needs reconciliation",
}
_STATE_SEVERITIES = {
    "queued": "info",
    "succeeded": "success",
    "rejected": "notice",
    "failed": "error",
    "partial_failed": "warning",
    "reconciliation_required": "warning",
}


def _subject(revision: ActionRevision) -> str:
    preview = revision.preview
    source = preview.get("source")
    if isinstance(source, dict):
        if source.get("subject"):
            return str(source["subject"])
        if source.get("sender"):
            return str(source["sender"])
    event = preview.get("event")
    if isinstance(event, dict) and event.get("title"):
        return str(event["title"])
    before = preview.get("before")
    if isinstance(before, dict) and before.get("summary"):
        return str(before["summary"])
    course = preview.get("course")
    if isinstance(course, dict):
        values = [
            str(value) for value in (course.get("course_code"), course.get("section")) if value
        ]
        if values:
            return " · ".join(values)
    term = preview.get("term")
    if isinstance(term, dict) and term.get("term_name"):
        return str(term["term_name"])
    lane = preview.get("lane")
    if isinstance(lane, dict) and lane.get("display_name"):
        return str(lane["display_name"])
    source_lane = preview.get("source_lane")
    destination_lane = preview.get("destination_lane")
    if isinstance(source_lane, dict) and isinstance(destination_lane, dict):
        return (
            f"{source_lane.get('display_name', source_lane.get('lane', 'Source'))} → "
            f"{destination_lane.get('display_name', destination_lane.get('lane', 'Destination'))}"
        )
    return "Configured Docket calendar"


def _result_detail(result: dict[str, Any] | None) -> str | None:
    counts = result.get("counts") if isinstance(result, dict) else None
    if not isinstance(counts, dict):
        return None
    return (
        f"{int(counts.get('succeeded', 0))} succeeded · "
        f"{int(counts.get('failed', 0))} failed · "
        f"{int(counts.get('reconciliation_required', 0))} uncertain"
    )


def enqueue_action_system_log(
    session: Session,
    *,
    action: Action,
    revision: ActionRevision,
    state: str,
    occurred_at: datetime | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    if state not in _STATE_TITLES:
        return
    gmail_action = revision.action_type.startswith("gmail_")
    target = revision.preview.get("target")
    target_scope = target.get("scope") if isinstance(target, dict) else None
    effect = (
        "Cancel recurring series"
        if revision.action_type == "calendar_cancel_event" and target_scope == "series"
        else _ACTION_LABELS.get(revision.action_type, "Calendar change")
    )
    summary = f"{effect} · {_subject(revision)}"
    detail = _result_detail(result)
    if detail is not None:
        summary = f"{summary}\n{detail}"
    deduplication_key = (
        f"discord_system_log:action:{action.id}:revision:{revision.revision}:{state}"
    )
    if (
        session.scalar(
            select(OutboxEvent.id).where(OutboxEvent.deduplication_key == deduplication_key)
        )
        is not None
    ):
        return
    session.add(
        OutboxEvent(
            event_type="discord.system_log.requested",
            aggregate_type="action",
            aggregate_id=action.id,
            deduplication_key=deduplication_key,
            payload={
                "title": (
                    _STATE_TITLES[state].replace("Calendar", "Gmail")
                    if gmail_action
                    else _STATE_TITLES[state]
                ),
                "summary": summary,
                "status": state,
                "severity": _STATE_SEVERITIES[state],
                "subsystem": "Gmail" if gmail_action else "Calendar",
                "occurred_at": (occurred_at or utc_now()).isoformat(),
            },
            status=OutboxStatus.PENDING.value,
        )
    )
