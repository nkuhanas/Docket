from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import Record
from docket.providers.google.calendar import (
    CalendarEventRequest,
    normalize_recurrence_lines,
)
from docket.schemas.records import CourseData, TermData

_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _first_occurrence(start_date: date, days: list[str]) -> date:
    desired = {_WEEKDAYS.index(day) for day in days}
    for offset in range(7):
        candidate = start_date + timedelta(days=offset)
        if candidate.weekday() in desired:
            return candidate
    raise AssertionError("validated weekdays always yield one occurrence")


def compile_course_items(
    record: Record,
    course: CourseData,
    term: TermData,
) -> list[dict[str, Any]]:
    """Compile one canonical course into stable provider-facing meeting items."""

    if term.start_date is None or term.end_date is None:
        raise DocketError(
            code="incomplete_schedule_term",
            message="The course term requires explicit start and end dates.",
        )
    items: list[dict[str, Any]] = []
    for meeting_id, meeting in sorted(course.meetings.items()):
        if meeting.start_time is None or meeting.end_time is None:
            raise DocketError(
                code="incomplete_course_meeting",
                message=(
                    f"{course.course_code} {meeting_id} requires explicit start and end times."
                ),
            )
        effective_start_date = meeting.start_date or term.start_date
        effective_end_date = meeting.end_date or term.end_date
        effective_timezone = meeting.timezone or term.timezone
        if effective_end_date < effective_start_date:
            raise DocketError(
                code="invalid_course_meeting_bounds",
                message=(
                    f"{course.course_code} {meeting_id} ends before its effective start date."
                ),
            )
        if effective_start_date < term.start_date or effective_end_date > term.end_date:
            raise DocketError(
                code="course_meeting_outside_term",
                message=f"{course.course_code} {meeting_id} falls outside the term.",
            )
        first = _first_occurrence(effective_start_date, list(meeting.days))
        if first > effective_end_date:
            raise DocketError(
                code="course_meeting_has_no_occurrence",
                message=(
                    f"{course.course_code} {meeting_id} has no selected weekday "
                    "within its date bounds."
                ),
            )
        title = " ".join(
            value
            for value in (
                course.course_code,
                course.section,
                course.course_title,
            )
            if value
        )
        event: dict[str, Any] = {
            "title": title,
            "timing": {
                "kind": "timed",
                "start_local": datetime.combine(first, meeting.start_time).isoformat(),
                "end_local": datetime.combine(first, meeting.end_time).isoformat(),
                "timezone": effective_timezone,
                "fold": None,
            },
            "location": meeting.location,
            "notes": None,
            "operator_tags": [],
            "priority": "normal",
            "recurrence": {
                "frequency": "weekly",
                "interval": 1,
                "weekdays": list(meeting.days),
                "month_days": [],
                "count": None,
                "until_date": effective_end_date.isoformat(),
                "excluded_dates": [value.isoformat() for value in meeting.excluded_dates],
                "additional_dates": [],
            },
            "reminder_plan": None,
        }
        item_key = f"course:{record.id}:{meeting_id}"
        item = {
            "item_key": item_key,
            "item_type": "recurring_series",
            "logical_key": item_key,
            "course_record_id": str(record.id),
            "course_record_version": record.version,
            "course_code": course.course_code,
            "section": course.section,
            "meeting_id": meeting_id,
            "exception_id": None,
            "date_range": {
                "start_date": effective_start_date.isoformat(),
                "end_date": effective_end_date.isoformat(),
                "timezone": effective_timezone,
                "start_source": "meeting" if meeting.start_date is not None else "term",
                "end_source": "meeting" if meeting.end_date is not None else "term",
            },
            "event": event,
            "classification": {
                "recurrence_kind": "recurring",
                "system_tags": ["recurring", "timed", "course_meeting"],
                "operator_tags": [],
                "priority": "normal",
                "priority_basis": "default",
            },
        }
        item["item_sha256"] = sha256_json(item)
        items.append(item)
        for occurrence in meeting.additional_occurrences:
            if (
                occurrence.date < term.start_date or occurrence.date > term.end_date
            ) and not occurrence.out_of_term_confirmed:
                raise DocketError(
                    code="course_exception_outside_term",
                    message=(
                        f"{course.course_code} {occurrence.occurrence_id} requires "
                        "explicit out-of-term confirmation."
                    ),
                )
            exception_link_id = (
                f"exception:{sha256_json([meeting_id, occurrence.occurrence_id])[:24]}"
            )
            exception_key = (
                f"course:{record.id}:{meeting_id}:exception:{occurrence.occurrence_id}"
            )
            exception_event: dict[str, Any] = {
                "title": f"{title} — {occurrence.occurrence_id}",
                "timing": {
                    "kind": "timed",
                    "start_local": datetime.combine(
                        occurrence.date,
                        occurrence.start_time,
                    ).isoformat(),
                    "end_local": datetime.combine(
                        occurrence.date,
                        occurrence.end_time,
                    ).isoformat(),
                    "timezone": effective_timezone,
                    "fold": None,
                },
                "location": occurrence.location,
                "notes": None,
                "operator_tags": [],
                "priority": "normal",
                "recurrence": None,
                "reminder_plan": None,
            }
            exception_item = {
                "item_key": exception_key,
                "item_type": "exception_occurrence",
                "logical_key": exception_key,
                "course_record_id": str(record.id),
                "course_record_version": record.version,
                "course_code": course.course_code,
                "section": course.section,
                "meeting_id": exception_link_id,
                "parent_meeting_id": meeting_id,
                "exception_id": occurrence.occurrence_id,
                "event": exception_event,
                "classification": {
                    "recurrence_kind": "one_time",
                    "system_tags": ["one_time", "timed", "course_exception"],
                    "operator_tags": [],
                    "priority": "normal",
                    "priority_basis": "default",
                },
            }
            exception_item["item_sha256"] = sha256_json(exception_item)
            items.append(exception_item)
    items.sort(key=lambda item: str(item["item_key"]))
    if not 1 <= len(items) <= 50:
        raise DocketError(
            code="course_manifest_too_large",
            message="A course reconciliation must contain from one through fifty items.",
        )
    return items


def calendar_material_snapshot(
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


def current_calendar_material_snapshot(
    snapshot: dict[str, Any],
    intended: dict[str, Any],
) -> dict[str, Any]:
    current = {key: snapshot.get(key) for key in intended}
    if "recurrence" in current:
        current["recurrence"] = normalize_recurrence_lines(current["recurrence"])
    return current


def first_overlap(
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
