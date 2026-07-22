import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from docket.config import get_settings
from docket.internal_api.schemas import ApprovalResponse
from docket.models import Account, CalendarEventCache, CalendarLink, Operation, Record
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.actions import ProposeActionInput
from docket.schemas.records import RecordSourceInput, StoreRecordInput, UpdateRecordInput
from docket.services.actions import ActionService
from docket.services.approvals import ApprovalService
from docket.services.operations import OperationRunner
from docket.services.records import RecordService


def source(message_id: str, intent_index: int) -> RecordSourceInput:
    settings = get_settings()
    return RecordSourceInput(
        source_type="discord_message",
        source_object_id=message_id,
        metadata={
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "user_id": settings.operator_discord_user_id,
            "intent_index": intent_index,
        },
    )


def request_key(message_id: str, intent_index: int) -> str:
    settings = get_settings()
    return (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:{intent_index}"
    )


def approve(short_code: str, interaction_id: str) -> ApprovalResponse:
    settings = get_settings()
    return ApprovalResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id=interaction_id,
        approval_id=None,
        approval_token=None,
        short_code=short_code,
        decision="approve",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.queue_channel_id,
        message_id="999999999999999999",
        responded_at=datetime.now(UTC),
    )


def propose(
    *,
    action_type: str,
    course: Record,
    account: Account,
    message_id: str,
) -> ProposeActionInput:
    settings = get_settings()
    return ProposeActionInput(
        action_type=action_type,
        record_id=course.id,
        expected_record_version=course.version,
        account_id=account.id,
        parameters={
            "meeting_id": "lecture-mo-we-1",
            "calendar_id": settings.google_calendar_id,
        },
        request_key=request_key(message_id, 0),
        source=source(message_id, 0),
        actor_id=settings.operator_discord_user_id,
    )


@pytest.mark.integration
def test_schedule_is_stored_approved_created_once_and_modified_in_place(
    session_factory,
) -> None:
    settings = get_settings()
    initial_message = "111111111111111111"
    with session_factory.begin() as session:
        records = RecordService(session)
        term = records.store(
            StoreRecordInput(
                record_type="term",
                canonical_identity={
                    "institution": "California Polytechnic State University, San Luis Obispo",
                    "term_name": "Fall 2026",
                },
                title="Fall 2026",
                data={
                    "institution": (
                        "California Polytechnic State University, San Luis Obispo"
                    ),
                    "term_name": "Fall 2026",
                    "start_date": "2026-08-24",
                    "end_date": "2026-12-18",
                    "timezone": "America/Los_Angeles",
                    "notes": None,
                },
                request_key=request_key(initial_message, 0),
                source=source(initial_message, 0),
                actor_id=settings.operator_discord_user_id,
            )
        )
        course = records.store(
            StoreRecordInput(
                record_type="course",
                canonical_identity={
                    "term_record_id": term.record_id,
                    "course_code": "CSC 101",
                    "section": "01",
                },
                title="CSC 101-01",
                data={
                    "term_record_id": term.record_id,
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
                            "location": "Building 14",
                            "start_date": "2026-08-24",
                            "end_date": "2026-12-18",
                            "timezone": "America/Los_Angeles",
                        }
                    },
                    "notes": None,
                },
                request_key=request_key(initial_message, 1),
                source=source(initial_message, 1),
                actor_id=settings.operator_discord_user_id,
            )
        )
        account = Account(
            provider="google",
            external_account_id="workflow-test",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        stored_course = session.get(Record, course.record_id)
        creation = ActionService(session).propose(
            propose(
                action_type="calendar_create_meeting",
                course=stored_course,
                account=account,
                message_id="222222222222222222",
            )
        )
        ApprovalService(session).respond(approve(creation.short_code, "interaction-create"))

    provider = FakeCalendarProvider()
    OperationRunner(session_factory, provider).run_due_once()
    with session_factory() as session:
        link = session.scalar(select(CalendarLink))
        cached_event = session.scalar(select(CalendarEventCache))
        assert link is not None
        assert cached_event is not None
        assert cached_event.account_id == account.id
        assert cached_event.calendar_id == settings.google_calendar_id
        assert cached_event.provider_event_id == link.external_event_id
        assert cached_event.summary == "CSC 101 - Fundamentals of Computer Science"
        original_event_id = link.external_event_id
        assert len(provider.events) == 1

    with session_factory.begin() as session:
        stored_course = session.get(Record, course.record_id)
        assert stored_course is not None
        updated = RecordService(session).update(
            UpdateRecordInput(
                record_id=stored_course.id,
                expected_version=1,
                data={
                    **stored_course.data,
                    "meetings": {
                        "lecture-mo-we-1": {
                            "meeting_type": "lecture",
                            "days": ["MO"],
                            "start_time": "10:30:00",
                            "end_time": "11:50:00",
                            "location": "Building 14",
                            "start_date": "2026-08-24",
                            "end_date": "2026-12-18",
                            "timezone": "America/Los_Angeles",
                        },
                        "lecture-we-2": {
                            "meeting_type": "lecture",
                            "days": ["WE"],
                            "start_time": "12:30:00",
                            "end_time": "13:50:00",
                            "location": "Building 14",
                            "start_date": "2026-08-24",
                            "end_date": "2026-12-18",
                            "timezone": "America/Los_Angeles",
                        },
                    },
                },
                request_key="workflow:update-course:1",
                reason="Wednesday lecture moved later",
                actor_id=settings.operator_discord_user_id,
            )
        )
        assert updated.version == 2
        update_action = ActionService(session).propose(
            propose(
                action_type="calendar_update_meeting",
                course=stored_course,
                account=session.get(Account, account.id),
                message_id="333333333333333333",
            )
        )
        ApprovalService(session).respond(
            approve(update_action.short_code, "interaction-update")
        )

    # A new runner instance models a Docket restart after approval and before execution.
    OperationRunner(session_factory, provider).run_due_once()
    with session_factory() as session:
        link = session.scalar(select(CalendarLink))
        operations = list(session.scalars(select(Operation).order_by(Operation.created_at)))
        assert link is not None
        assert [operation.status for operation in operations] == ["succeeded", "succeeded"]
        assert link.external_event_id == original_event_id
        assert link.last_synced_version == 2
        assert link.synced_snapshot["recurrence"][0].find("BYDAY=MO") > 0
        assert len(provider.events) == 1
