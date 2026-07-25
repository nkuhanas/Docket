import uuid

import pytest

from docket.domain.errors import DocketError
from docket.models import Record
from docket.schemas.records import CourseData, TermData
from docket.services.course_manifest import compile_course_items


def _record(course: CourseData) -> Record:
    return Record(
        id=uuid.uuid4(),
        record_type="course",
        canonical_key=f"course:{uuid.uuid4()}",
        schema_version=1,
        title="DKT 930",
        status="active",
        version=3,
        data=course.model_dump(mode="json"),
    )


def _term() -> TermData:
    return TermData(
        institution="California Polytechnic State University, San Luis Obispo",
        term_name="Fall 2026",
        start_date="2026-08-24",
        end_date="2026-12-18",
        timezone="America/Los_Angeles",
    )


def _course(meeting: dict) -> CourseData:
    return CourseData.model_validate(
        {
            "term_record_id": str(uuid.uuid4()),
            "course_code": "DKT 930",
            "course_title": "Manifest Boundaries",
            "section": "01",
            "meetings": {"lecture": meeting},
        }
    )


def test_meeting_bounds_override_term_and_omissions_inherit() -> None:
    inherited = _course(
        {
            "meeting_type": "lecture",
            "days": ["MO"],
            "start_time": "09:00:00",
            "end_time": "09:50:00",
            "location": "Room A",
        }
    )
    inherited_item = compile_course_items(_record(inherited), inherited, _term())[0]
    assert inherited_item["date_range"] == {
        "start_date": "2026-08-24",
        "end_date": "2026-12-18",
        "timezone": "America/Los_Angeles",
        "start_source": "term",
        "end_source": "term",
    }

    explicit = _course(
        {
            "meeting_type": "lecture",
            "days": ["FR"],
            "start_time": "10:00:00",
            "end_time": "10:50:00",
            "location": "Room B",
            "start_date": "2026-09-04",
            "end_date": "2026-11-20",
            "timezone": "America/New_York",
        }
    )
    explicit_item = compile_course_items(_record(explicit), explicit, _term())[0]
    assert explicit_item["date_range"] == {
        "start_date": "2026-09-04",
        "end_date": "2026-11-20",
        "timezone": "America/New_York",
        "start_source": "meeting",
        "end_source": "meeting",
    }


def test_meeting_must_fit_term_and_contain_a_selected_weekday() -> None:
    outside = _course(
        {
            "meeting_type": "lecture",
            "days": ["MO"],
            "start_time": "09:00:00",
            "end_time": "09:50:00",
            "start_date": "2026-08-17",
            "end_date": "2026-12-18",
        }
    )
    with pytest.raises(DocketError) as raised:
        compile_course_items(_record(outside), outside, _term())
    assert raised.value.code == "course_meeting_outside_term"

    no_occurrence = _course(
        {
            "meeting_type": "lecture",
            "days": ["MO"],
            "start_time": "09:00:00",
            "end_time": "09:50:00",
            "start_date": "2026-08-25",
            "end_date": "2026-08-25",
        }
    )
    with pytest.raises(DocketError) as raised:
        compile_course_items(_record(no_occurrence), no_occurrence, _term())
    assert raised.value.code == "course_meeting_has_no_occurrence"


def test_course_manifest_is_bounded_to_fifty_provider_items() -> None:
    meeting = {
        "meeting_type": "lecture",
        "days": ["MO"],
        "start_time": "09:00:00",
        "end_time": "09:50:00",
        "additional_occurrences": [
            {
                "occurrence_id": f"extra-{index}",
                "date": "2026-09-01",
                "start_time": "10:00:00",
                "end_time": "10:15:00",
            }
            for index in range(50)
        ],
    }
    course = _course(meeting)
    with pytest.raises(DocketError) as raised:
        compile_course_items(_record(course), course, _term())
    assert raised.value.code == "course_manifest_too_large"
