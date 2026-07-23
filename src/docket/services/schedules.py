import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.canonical import canonical_record_key, sha256_json
from docket.domain.enums import CommandStatus, RecordStatus
from docket.domain.errors import DocketError, IdempotencyConflict
from docket.models import (
    AuditEvent,
    CalendarScheduleSnapshot,
    CommandRequest,
    Record,
    RecordSource,
)
from docket.models.base import utc_now
from docket.schemas.records import (
    CourseData,
    ExistingScheduleTerm,
    NewScheduleTerm,
    StoreTermScheduleInput,
    TermData,
    TermScheduleStoreResult,
)
from docket.services.records import serialize_record
from docket.services.source_context import validate_configured_discord_source

_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _differences(current: Any, requested: Any, prefix: str = "") -> list[str]:
    if isinstance(current, dict) and isinstance(requested, dict):
        result: list[str] = []
        for key in sorted(set(current) | set(requested)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in current or key not in requested:
                result.append(path)
            else:
                result.extend(_differences(current[key], requested[key], path))
        return result
    return [] if current == requested else [prefix or "data"]


def _first_occurrence(start_date: date, days: list[str]) -> date:
    desired = {_WEEKDAYS.index(day) for day in days}
    for offset in range(7):
        candidate = start_date + timedelta(days=offset)
        if candidate.weekday() in desired:
            return candidate
    raise AssertionError("validated weekdays always yield one occurrence")


class TermScheduleService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _start_command(
        self, request: StoreTermScheduleInput
    ) -> tuple[CommandRequest, TermScheduleStoreResult | None]:
        payload = request.model_dump(mode="json")
        input_sha256 = sha256_json(payload)
        existing = self.session.scalar(
            select(CommandRequest).where(CommandRequest.request_key == request.request_key)
        )
        if existing is not None:
            if (
                existing.operation_name != "docket_store_term_schedule"
                or existing.input_sha256 != input_sha256
            ):
                raise IdempotencyConflict(
                    request.request_key,
                    existing_operation=existing.operation_name,
                    attempted_operation="docket_store_term_schedule",
                )
            if existing.status == CommandStatus.SUCCEEDED.value and existing.result is not None:
                replay = dict(existing.result)
                replay["disposition"] = "replayed_request"
                return existing, TermScheduleStoreResult.model_validate(replay)
            raise DocketError(
                code="request_in_progress",
                message="The complete-schedule request has not completed.",
            )
        command = CommandRequest(
            request_key=request.request_key,
            operation_name="docket_store_term_schedule",
            input_sha256=input_sha256,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            status=CommandStatus.IN_PROGRESS.value,
        )
        self.session.add(command)
        self.session.flush()
        return command, None

    @staticmethod
    def _complete_term(record: Record) -> TermData:
        if record.record_type != "term" or record.status != RecordStatus.ACTIVE.value:
            raise DocketError(
                code="invalid_schedule_term",
                message="The schedule must bind one active term record.",
            )
        term = TermData.model_validate(record.data)
        if term.start_date is None or term.end_date is None:
            raise DocketError(
                code="incomplete_schedule_term",
                message="The selected term requires explicit start and end dates.",
            )
        return term

    def _resolve_term(self, request: StoreTermScheduleInput) -> tuple[Record, TermData, bool]:
        if isinstance(request.term, ExistingScheduleTerm):
            record = self.session.get(Record, request.term.record_id)
            if record is None:
                raise DocketError(
                    code="schedule_term_not_found",
                    message="The selected term record does not exist.",
                )
            if record.version != request.term.expected_version:
                raise DocketError(
                    code="schedule_term_version_conflict",
                    message="The selected term changed before schedule storage.",
                    details={
                        "record_id": str(record.id),
                        "expected_version": request.term.expected_version,
                        "current_version": record.version,
                    },
                )
            return record, self._complete_term(record), False

        assert isinstance(request.term, NewScheduleTerm)
        data = request.term.data.model_dump(mode="json")
        expected_key = canonical_record_key(
            "term",
            request.term.canonical_identity.model_dump(mode="json"),
        )
        data_key = canonical_record_key(
            "term",
            {
                "institution": request.term.data.institution,
                "term_name": request.term.data.term_name,
            },
        )
        if expected_key != data_key:
            raise DocketError(
                code="canonical_identity_mismatch",
                message="The term identity does not match its validated data.",
            )
        record = self.session.scalar(
            select(Record).where(
                Record.record_type == "term",
                Record.canonical_key == expected_key,
            )
        )
        if record is not None:
            differences = _differences(record.data, data)
            if differences:
                raise DocketError(
                    code="schedule_record_conflict",
                    message="The complete schedule conflicts with canonical term data.",
                    details={
                        "conflicts": [
                            {
                                "record_id": str(record.id),
                                "record_type": "term",
                                "fields": differences[:20],
                            }
                        ]
                    },
                )
            return record, self._complete_term(record), False
        record = Record(
            id=uuid.uuid4(),
            record_type="term",
            canonical_key=expected_key,
            schema_version=1,
            title=request.term.title,
            status=RecordStatus.ACTIVE.value,
            data=data,
            valid_from_date=request.term.data.start_date,
            valid_until_date=request.term.data.end_date,
        )
        self.session.add(record)
        self.session.flush()
        return record, request.term.data, True

    @staticmethod
    def _course_data(
        request: StoreTermScheduleInput,
        term: Record,
        index: int,
    ) -> CourseData:
        course = request.courses[index]
        return CourseData(
            term_record_id=term.id,
            course_code=course.course_code,
            course_title=course.course_title,
            section=course.section,
            instructor=course.instructor,
            meetings=course.meetings,
            notes=course.notes,
        )

    def _resolve_courses(
        self,
        request: StoreTermScheduleInput,
        term: Record,
    ) -> tuple[list[Record], list[bool]]:
        records: list[Record] = []
        created: list[bool] = []
        conflicts: list[dict[str, Any]] = []
        for index, submitted in enumerate(request.courses):
            data_model = self._course_data(request, term, index)
            data = data_model.model_dump(mode="json")
            key = canonical_record_key(
                "course",
                {
                    "term_record_id": term.id,
                    "course_code": submitted.course_code,
                    "section": submitted.section,
                },
            )
            record = self.session.scalar(
                select(Record).where(
                    Record.record_type == "course",
                    Record.canonical_key == key,
                )
            )
            if record is not None:
                differences = _differences(record.data, data)
                if differences:
                    conflicts.append(
                        {
                            "record_id": str(record.id),
                            "record_type": "course",
                            "course_code": submitted.course_code,
                            "section": submitted.section,
                            "fields": differences[:20],
                        }
                    )
                records.append(record)
                created.append(False)
                continue
            starts = [
                meeting.start_date
                for meeting in data_model.meetings.values()
                if meeting.start_date is not None
            ]
            ends = [
                meeting.end_date
                for meeting in data_model.meetings.values()
                if meeting.end_date is not None
            ]
            title_parts = [submitted.course_code]
            if submitted.section:
                title_parts.append(submitted.section)
            record = Record(
                id=uuid.uuid4(),
                record_type="course",
                canonical_key=key,
                schema_version=2,
                title="-".join(title_parts),
                status=RecordStatus.ACTIVE.value,
                data=data,
                valid_from_date=min(starts, default=None),
                valid_until_date=max(ends, default=None),
            )
            records.append(record)
            created.append(True)
        if conflicts:
            raise DocketError(
                code="schedule_record_conflict",
                message=(
                    "The complete schedule conflicts with existing canonical records; "
                    "nothing was stored."
                ),
                details={"conflicts": conflicts[:20]},
            )
        self.session.add_all(
            record for record, is_created in zip(records, created, strict=True) if is_created
        )
        self.session.flush()
        return records, created

    @staticmethod
    def _manifest(
        term: Record,
        term_data: TermData,
        courses: list[Record],
    ) -> dict[str, Any]:
        assert term_data.start_date is not None and term_data.end_date is not None
        items: list[dict[str, Any]] = []
        for record in courses:
            course = CourseData.model_validate(record.data)
            for meeting_id, meeting in sorted(course.meetings.items()):
                if any(
                    value is None
                    for value in (
                        meeting.start_time,
                        meeting.end_time,
                        meeting.start_date,
                        meeting.end_date,
                        meeting.timezone,
                    )
                ):
                    raise DocketError(
                        code="incomplete_schedule_meeting",
                        message=(
                            f"{course.course_code} {meeting_id} requires explicit "
                            "time, date, and timezone bounds."
                        ),
                    )
                assert meeting.start_date is not None
                assert meeting.end_date is not None
                assert meeting.start_time is not None
                assert meeting.end_time is not None
                assert meeting.timezone is not None
                if (
                    meeting.start_date < term_data.start_date
                    or meeting.end_date > term_data.end_date
                ):
                    raise DocketError(
                        code="schedule_meeting_outside_term",
                        message=(f"{course.course_code} {meeting_id} falls outside the term."),
                    )
                first = _first_occurrence(meeting.start_date, list(meeting.days))
                if first > meeting.end_date:
                    raise DocketError(
                        code="schedule_meeting_has_no_occurrence",
                        message=(
                            f"{course.course_code} {meeting_id} has no selected "
                            "weekday within its date bounds."
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
                        "timezone": meeting.timezone,
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
                        "until_date": meeting.end_date.isoformat(),
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
                    "event": event,
                    "classification": {
                        "recurrence_kind": "recurring",
                        "system_tags": [
                            "recurring",
                            "timed",
                            "course_meeting",
                        ],
                        "operator_tags": [],
                        "priority": "normal",
                        "priority_basis": "default",
                    },
                }
                item["item_sha256"] = sha256_json(item)
                items.append(item)
                for occurrence in meeting.additional_occurrences:
                    if (
                        occurrence.date < term_data.start_date
                        or occurrence.date > term_data.end_date
                    ) and not occurrence.out_of_term_confirmed:
                        raise DocketError(
                            code="schedule_exception_outside_term",
                            message=(
                                f"{course.course_code} {occurrence.occurrence_id} "
                                "requires explicit out-of-term confirmation."
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
                                occurrence.date, occurrence.start_time
                            ).isoformat(),
                            "end_local": datetime.combine(
                                occurrence.date, occurrence.end_time
                            ).isoformat(),
                            "timezone": meeting.timezone,
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
                            "system_tags": [
                                "one_time",
                                "timed",
                                "course_exception",
                            ],
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
                code="schedule_manifest_too_large",
                message="The compiled schedule must contain from 1 through 50 items.",
            )
        return {
            "version": 1,
            "term": {
                "record_id": str(term.id),
                "record_version": term.version,
                "institution": term_data.institution,
                "term_name": term_data.term_name,
                "start_date": term_data.start_date.isoformat(),
                "end_date": term_data.end_date.isoformat(),
                "timezone": term_data.timezone,
            },
            "items": items,
        }

    def store(self, request: StoreTermScheduleInput) -> TermScheduleStoreResult:
        validate_configured_discord_source(request.source, request.actor_id)
        command, replay = self._start_command(request)
        if replay is not None:
            return replay
        term, term_data, term_created = self._resolve_term(request)
        courses, courses_created = self._resolve_courses(request, term)
        manifest = self._manifest(term, term_data, courses)
        manifest_sha256 = sha256_json(manifest)
        source = request.source
        for record, is_created in [
            (term, term_created),
            *list(zip(courses, courses_created, strict=True)),
        ]:
            self.session.add(
                RecordSource(
                    record_id=record.id,
                    source_type=source.source_type,
                    source_account_id=None,
                    source_object_id=source.source_object_id,
                    source_request_key=request.request_key,
                    source_version=source.source_version,
                    source_metadata=source.metadata.model_dump(mode="json"),
                )
            )
            self.session.add(
                AuditEvent(
                    event_type=("record.created" if is_created else "record.matched"),
                    entity_type="record",
                    entity_id=record.id,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    request_id=command.id,
                    data={
                        "aggregate": "term_schedule",
                        "record_type": record.record_type,
                        "version": record.version,
                        "data_sha256": sha256_json(record.data),
                    },
                )
            )
        snapshot = CalendarScheduleSnapshot(
            command_request_id=command.id,
            term_record_id=term.id,
            term_record_version=term.version,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            item_count=len(manifest["items"]),
        )
        self.session.add(snapshot)
        self.session.flush()
        result = TermScheduleStoreResult(
            request_id=command.id,
            disposition="stored",
            schedule_snapshot_id=snapshot.id,
            manifest_sha256=manifest_sha256,
            item_count=snapshot.item_count,
            term_record=serialize_record(term),
            course_records=[serialize_record(record) for record in courses],
        )
        command.status = CommandStatus.SUCCEEDED.value
        command.result = result.model_dump(mode="json")
        command.completed_at = utc_now()
        self.session.add(
            AuditEvent(
                event_type="calendar.schedule_stored",
                entity_type="calendar_schedule_snapshot",
                entity_id=snapshot.id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                request_id=command.id,
                data={
                    "manifest_sha256": manifest_sha256,
                    "item_count": snapshot.item_count,
                    "term_record_id": str(term.id),
                },
            )
        )
        return result
