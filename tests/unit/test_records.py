import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.domain.errors import (
    DocketError,
    IdempotencyConflict,
    RecordConflict,
    VersionConflict,
)
from docket.models import AuditEvent, CommandRequest, Record, RecordSource
from docket.schemas.records import (
    ArchiveRecordInput,
    CourseData,
    RecordSourceInput,
    StoreRecordInput,
    TermData,
    UpdateRecordInput,
)
from docket.services.records import RecordService

OPERATOR_ID = "000000000000000001"
GUILD_ID = "000000000000000002"
CHAT_CHANNEL_ID = "000000000000000003"
MESSAGE_ID = "111111111111111111"


def store_term_request(
    *, message_id: str = MESSAGE_ID, intent_index: int = 0
) -> StoreRecordInput:
    request_key = f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{message_id}:{intent_index}"
    return StoreRecordInput(
        record_type="term",
        canonical_identity={"institution": "Cal Poly", "term_name": "Fall 2026"},
        title="Fall 2026",
        data={
            "institution": "Cal Poly",
            "term_name": "Fall 2026",
            "start_date": "2026-09-21",
            "end_date": "2026-12-11",
            "timezone": "America/Los_Angeles",
            "notes": None,
        },
        request_key=request_key,
        source=RecordSourceInput(
            source_type="discord_message",
            source_object_id=message_id,
            metadata={
                "guild_id": GUILD_ID,
                "channel_id": CHAT_CHANNEL_ID,
                "message_id": message_id,
                "user_id": OPERATOR_ID,
                "intent_index": intent_index,
            },
        ),
        actor_id=OPERATOR_ID,
    )


def store_course_request(term_record_id: object) -> StoreRecordInput:
    request_key = f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:1"
    return StoreRecordInput(
        record_type="course",
        canonical_identity={
            "term_record_id": term_record_id,
            "course_code": "CSC 101",
            "section": "01",
        },
        title="CSC 101-01",
        data={
            "term_record_id": term_record_id,
            "course_code": "CSC 101",
            "course_title": "Fundamentals of Computer Science",
            "section": "01",
            "instructor": None,
            "meetings": {
                "lecture-mo-we-1": {
                    "meeting_type": "lecture",
                    "days": ["MO", "WE"],
                    "start_time": "10:30:00",
                    "end_time": "11:50:00",
                    "location": None,
                    "start_date": "2026-09-21",
                    "end_date": "2026-12-11",
                    "timezone": "America/Los_Angeles",
                }
            },
            "notes": None,
        },
        request_key=request_key,
        source=RecordSourceInput(
            source_type="discord_message",
            source_object_id=MESSAGE_ID,
            metadata={
                "guild_id": GUILD_ID,
                "channel_id": CHAT_CHANNEL_ID,
                "message_id": MESSAGE_ID,
                "user_id": OPERATOR_ID,
                "intent_index": 1,
            },
        ),
        actor_id=OPERATOR_ID,
    )


def test_store_and_replay_are_idempotent(session: Session) -> None:
    service = RecordService(session)
    first = service.store(store_term_request())
    session.commit()
    second = service.store(store_term_request())

    assert first.record_id == second.record_id
    assert first.disposition == "created"
    assert second.disposition == "replayed_request"
    assert first.record is not None
    assert first.record["data"]["term_name"] == "Fall 2026"
    assert second.record is not None
    assert second.record["record_id"] == str(first.record_id)
    assert len(list(session.scalars(select(Record)))) == 1


def test_store_replays_historical_remember_operation_name(session: Session) -> None:
    service = RecordService(session)
    first = service.store(store_term_request())
    session.flush()
    command = session.scalar(
        select(CommandRequest).where(CommandRequest.request_key == store_term_request().request_key)
    )
    assert command is not None
    command.operation_name = "docket_remember_record"
    session.commit()

    replay = service.store(store_term_request())

    assert replay.record_id == first.record_id
    assert replay.disposition == "replayed_request"


def test_new_request_matches_canonical_record(session: Session) -> None:
    service = RecordService(session)
    first = service.store(store_term_request())
    session.commit()
    second = service.store(store_term_request(message_id="222222222222222222"))

    assert first.record_id == second.record_id
    assert second.disposition == "matched_existing"
    assert second.record is not None
    assert second.record["version"] == 1


def test_new_request_with_conflicting_canonical_data_is_rejected(session: Session) -> None:
    service = RecordService(session)
    service.store(store_term_request())
    session.commit()
    changed = store_term_request(message_id="222222222222222222")
    assert isinstance(changed.data, TermData)
    changed.data.end_date = changed.data.end_date.replace(day=18) if changed.data.end_date else None

    with pytest.raises(RecordConflict) as raised:
        service.store(changed)

    assert raised.value.code == "record_conflict"
    assert "Do not copy the existing record" in raised.value.message
    assert raised.value.details is not None
    assert raised.value.details["differing_fields"] == ["end_date"]
    session.rollback()
    assert len(list(session.scalars(select(RecordSource)))) == 1


def test_search_matches_terms_across_title_and_canonical_key(session: Session) -> None:
    service = RecordService(session)
    request = store_term_request()
    assert isinstance(request.data, TermData)
    request.canonical_identity.institution = (
        "California Polytechnic State University, San Luis Obispo"
    )
    request.data.institution = "California Polytechnic State University, San Luis Obispo"
    request.title = "Cal Poly San Luis Obispo — Fall 2026"
    created = service.store(request)

    matches = service.search(
        record_type="term",
        query=(
            "California Polytechnic State University San Luis Obispo Fall 2026"
        ),
    )

    assert [record.id for record in matches] == [created.record_id]


def test_course_record_references_term_and_derives_date_bounds(session: Session) -> None:
    service = RecordService(session)
    term = service.store(store_term_request())
    course = service.store(store_course_request(term.record_id))
    session.flush()

    stored = session.get(Record, course.record_id)
    assert stored is not None
    assert stored.canonical_key == f"course:{term.record_id}:csc-101:01"
    assert stored.valid_from_date.isoformat() == "2026-09-21"
    assert stored.valid_until_date.isoformat() == "2026-12-11"
    assert stored.data["meetings"]["lecture-mo-we-1"]["days"] == ["MO", "WE"]


def test_incomplete_course_is_storable(session: Session) -> None:
    service = RecordService(session)
    term = service.store(store_term_request())
    request = store_course_request(term.record_id)
    assert isinstance(request.data, CourseData)
    request.data.meetings = {}

    result = service.store(request)
    stored = session.get(Record, result.record_id)

    assert stored is not None
    assert stored.valid_from_date is None
    assert stored.valid_until_date is None


def test_course_requires_existing_active_term(session: Session) -> None:
    service = RecordService(session)
    request = store_course_request("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with pytest.raises(DocketError) as raised:
        service.store(request)

    assert raised.value.code == "invalid_term_reference"


def test_reusing_request_key_with_different_input_fails(session: Session) -> None:
    service = RecordService(session)
    service.store(store_term_request())
    session.commit()
    changed = store_term_request()
    changed.title = "Different"

    with pytest.raises(IdempotencyConflict) as raised:
        service.store(changed)

    assert raised.value.details is not None
    assert raised.value.details["existing_operation"] == "docket_store_record"
    assert raised.value.details["attempted_operation"] == "docket_store_record"


def test_optimistic_update_and_archive(session: Session) -> None:
    service = RecordService(session)
    created = service.store(store_term_request())
    session.commit()
    updated = service.update(
        UpdateRecordInput(
            record_id=created.record_id,
            expected_version=1,
            data={
                "institution": "Cal Poly",
                "term_name": "Fall 2026",
                "start_date": "2026-09-22",
                "end_date": "2026-12-11",
                "timezone": "America/Los_Angeles",
                "notes": "corrected",
            },
            request_key="discord:guild:channel:update:0",
            reason="User corrected the date",
        )
    )
    assert updated.version == 2

    with pytest.raises(VersionConflict):
        service.archive(
            ArchiveRecordInput(
                record_id=created.record_id,
                expected_version=1,
                request_key="discord:guild:channel:archive:0",
                reason="stale archive",
            )
        )


def test_audit_stores_hash_not_record_body(session: Session) -> None:
    secret_text = "private body must not enter audit"
    request = store_term_request()
    assert isinstance(request.data, TermData)
    request.data.notes = secret_text
    RecordService(session).store(request)
    session.flush()
    event = session.scalar(select(AuditEvent))

    assert event is not None
    assert secret_text not in str(event.data)
    assert "data_sha256" in event.data


def test_unknown_tool_fields_are_rejected() -> None:
    payload = store_term_request().model_dump(mode="json")
    payload["risk_class"] = "read_only"
    with pytest.raises(ValidationError):
        StoreRecordInput.model_validate(payload)


def test_unsupported_record_alias_is_rejected() -> None:
    payload = store_term_request().model_dump(mode="json")
    payload["record_type"] = "academic_term"
    with pytest.raises(ValidationError, match="record_type"):
        StoreRecordInput.model_validate(payload)


def test_term_cannot_fall_back_to_generic_data() -> None:
    payload = store_term_request().model_dump(mode="json")
    payload["data"] = {
        "institution": "Cal Poly",
        "term": "Fall 2026",
        "start_date": "2026-08-24",
        "end_date": "2026-12-18",
    }
    with pytest.raises(ValidationError, match="TermData"):
        StoreRecordInput.model_validate(payload)


def test_course_meeting_rejects_duplicate_days_and_unstable_ids() -> None:
    duplicate_days = {
        "term_record_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "course_code": "CSC 101",
        "meetings": {
            "lecture-1": {
                "meeting_type": "lecture",
                "days": ["MO", "MO"],
            }
        },
    }
    with pytest.raises(ValidationError, match="meeting days must be unique"):
        CourseData.model_validate(duplicate_days)

    unstable_id = duplicate_days | {
        "meetings": {
            "0/lecture": {
                "meeting_type": "lecture",
                "days": ["MO"],
            }
        }
    }
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        CourseData.model_validate(unstable_id)


def test_update_cannot_change_canonical_identity(session: Session) -> None:
    service = RecordService(session)
    created = service.store(store_term_request())

    with pytest.raises(DocketError) as raised:
        service.update(
            UpdateRecordInput(
                record_id=created.record_id,
                expected_version=1,
                data={
                    "institution": "A Different School",
                    "term_name": "Fall 2026",
                    "timezone": "America/Los_Angeles",
                },
                request_key="discord:guild:channel:identity-update:0",
                reason="Attempted identity change",
            )
        )

    assert raised.value.code == "canonical_identity_mismatch"


def test_discord_request_key_must_match_source_metadata() -> None:
    payload = store_term_request().model_dump(mode="json")
    payload["request_key"] = (
        f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:333333333333333333:0"
    )
    with pytest.raises(ValidationError, match="request_key"):
        StoreRecordInput.model_validate(payload)


def test_discord_source_must_match_configured_operator_context(session: Session) -> None:
    request = store_term_request()
    request.source.metadata.guild_id = "999999999999999999"
    request.request_key = (
        f"discord:999999999999999999:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:0"
    )

    with pytest.raises(DocketError) as raised:
        RecordService(session).store(request)

    assert raised.value.code == "invalid_source_context"
