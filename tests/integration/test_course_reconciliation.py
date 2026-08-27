import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import ApprovalResponse, LocalActionResponse
from docket.models import (
    Account,
    ActionRevision,
    Approval,
    CalendarEventCache,
    CalendarLane,
    CalendarLink,
    CalendarSyncState,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OperationItem,
    QueueItem,
    Record,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.actions import ProposeCourseReconciliationInput
from docket.schemas.records import (
    CourseData,
    RecordSourceInput,
    RestoreRecordInput,
    StoreRecordInput,
    UpdateRecordInput,
)
from docket.services.approvals import ApprovalService
from docket.services.course_reconciliation import CourseReconciliationService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.operations import OperationRunner
from docket.services.proposal_controls import ProposalControlService
from docket.services.records import RecordService


def _source(message_id: str, intent_index: int = 0) -> RecordSourceInput:
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


def _request_key(message_id: str, intent_index: int = 0) -> str:
    settings = get_settings()
    return (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:{intent_index}"
    )


def _academic_calendar_id() -> str:
    return f"academic:{get_settings().google_calendar_id}"


def _meeting(
    meeting_type: str,
    days: list[str],
    start_time: str,
    end_time: str,
    location: str,
) -> dict:
    return {
        "meeting_type": meeting_type,
        "days": days,
        "start_time": start_time,
        "end_time": end_time,
        "location": location,
        "start_date": "2026-08-24",
        "end_date": "2026-12-18",
        "timezone": "America/Los_Angeles",
    }


def _course_data(term_id: uuid.UUID) -> dict:
    return {
        "term_record_id": str(term_id),
        "course_code": "DKT 937",
        "course_title": "Independent Course Lifecycle",
        "section": "LIFECYCLE",
        "instructor": None,
        "meetings": {
            "lecture-mo": _meeting(
                "lecture",
                ["MO"],
                "09:00:00",
                "09:50:00",
                "Room A",
            ),
            "lab-we": _meeting(
                "lab",
                ["WE"],
                "10:00:00",
                "10:50:00",
                "Room B",
            ),
        },
        "notes": None,
    }


def _seed_course(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    settings = get_settings()
    records = RecordService(session)
    term_message = "511111111111111111"
    term = records.store(
        StoreRecordInput(
            record_type="term",
            canonical_identity={
                "institution": "California Polytechnic State University, San Luis Obispo",
                "term_name": "Fall 2026",
            },
            title="Fall 2026",
            data={
                "institution": "California Polytechnic State University, San Luis Obispo",
                "term_name": "Fall 2026",
                "start_date": "2026-08-24",
                "end_date": "2026-12-18",
                "timezone": "America/Los_Angeles",
            },
            request_key=_request_key(term_message),
            source=_source(term_message),
            actor_id=settings.operator_discord_user_id,
        )
    )
    course_message = "522222222222222222"
    data = _course_data(term.record_id)
    course = records.store(
        StoreRecordInput(
            record_type="course",
            canonical_identity={
                "term_record_id": term.record_id,
                "course_code": "DKT 937",
                "section": "LIFECYCLE",
            },
            title="DKT 937 LIFECYCLE",
            data=data,
            request_key=_request_key(course_message),
            source=_source(course_message),
            actor_id=settings.operator_discord_user_id,
        )
    )
    account = Account(
        provider="google",
        external_account_id="course-lifecycle",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    session.add(
        CalendarLane(
            account_id=account.id,
            lane="academic",
            display_name="Docket · Academic",
            color_hex="#3F51B5",
            calendar_id=_academic_calendar_id(),
            status="active",
        )
    )
    now = datetime.now(UTC)
    session.add(
        CalendarSyncState(
            account_id=account.id,
            calendar_id=_academic_calendar_id(),
            window_start=now - timedelta(days=30),
            window_end=now + timedelta(days=400),
            snapshot_generation=uuid.uuid4(),
            status="current",
            last_attempt_at=now,
            last_success_at=now,
        )
    )
    return course.record_id, account.id


def _apply_explicit(
    session: Session,
    *,
    record_id: uuid.UUID,
    version: int,
    account_id: uuid.UUID,
    message_id: str,
    mode: str = "sync",
    reason: str | None = None,
) -> dict:
    settings = get_settings()
    return CourseReconciliationService(session).apply_explicit(
        ProposeCourseReconciliationInput(
            record_id=record_id,
            expected_record_version=version,
            mode=mode,
            account_id=account_id,
            calendar_id=_academic_calendar_id(),
            reason=reason,
            request_key=_request_key(message_id),
            source=_source(message_id),
            actor_id=settings.operator_discord_user_id,
        )
    )


def _seed_course_conflict(
    session: Session,
    *,
    account_id: uuid.UUID,
    provider_event_id: str = "existing-course-conflict",
) -> None:
    state = session.scalar(
        select(CalendarSyncState).where(
            CalendarSyncState.account_id == account_id,
            CalendarSyncState.calendar_id == _academic_calendar_id(),
        )
    )
    assert state is not None and state.snapshot_generation is not None
    session.add(
        CalendarEventCache(
            account_id=account_id,
            calendar_id=_academic_calendar_id(),
            provider_event_id=provider_event_id,
            snapshot_generation=state.snapshot_generation,
            status="confirmed",
            summary="Existing course conflict",
            location="Elsewhere",
            is_all_day=False,
            start_at=datetime(2026, 8, 24, 16, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 24, 16, 20, tzinfo=UTC),
            timezone="America/Los_Angeles",
            recurrence_kind="one_time",
            system_tags=["one_time", "timed", "external"],
            operator_tags=[],
            priority="normal",
            priority_basis="default",
            provider_reminders={},
            provider_etag=f'"{provider_event_id}"',
            synced_at=state.last_success_at or datetime.now(UTC),
        )
    )


def _approve(session: Session, proposal: dict, interaction_id: str) -> uuid.UUID:
    settings = get_settings()
    result = ApprovalService(session).respond(
        ApprovalResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id=interaction_id,
            approval_id=None,
            approval_token=None,
            short_code=str(proposal["short_code"]),
            decision="approve",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.queue_channel_id,
            message_id="533333333333333333",
            responded_at=datetime.now(UTC),
        )
    )
    return uuid.UUID(str(result["operation_id"]))


def _select_keep_both_and_prepare_approval(
    session_factory: sessionmaker[Session],
    projection_runner: DiscordProjectionRunner,
    backend: FakeDiscordBackend,
    proposal: dict,
    interaction_prefix: str,
) -> ApprovalResponse:
    settings = get_settings()
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        assert projection is not None and projection.message_id is not None
        thread = session.get(DiscordDailyThread, projection.daily_thread_id)
        assert thread is not None and thread.thread_id is not None
        conflict_control = next(
            control
            for control in backend.messages[str(projection.id)]["controls"]
            if control.get("field") == "conflict_resolution"
        )
        selection = LocalActionResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id=f"{interaction_prefix}-keep-both",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=thread.thread_id,
            parent_channel_id=settings.queue_channel_id,
            projection_id=projection.id,
            message_id=projection.message_id,
            responded_at=datetime.now(UTC),
            action_revision_id=uuid.UUID(str(proposal["action_revision_id"])),
            action_token=str(conflict_control["token"]),
            transition="proposal_field_change",
            field="conflict_resolution",
            value="keep_both",
        )
    with session_factory.begin() as session:
        selected = ProposalControlService(session).respond(selection)
        assert selected["revision"] == 2
    while projection_runner.run_due_once():
        pass
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        assert projection is not None and projection.message_id is not None
        thread = session.get(DiscordDailyThread, projection.daily_thread_id)
        assert thread is not None and thread.thread_id is not None
        approval_control = next(
            control
            for control in backend.messages[str(projection.id)]["controls"]
            if control.get("decision") == "approve"
        )
        return ApprovalResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id=f"{interaction_prefix}-approve",
            approval_id=uuid.UUID(str(approval_control["approval_id"])),
            approval_token=str(approval_control["token"]),
            short_code=None,
            decision="approve",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=thread.thread_id,
            parent_channel_id=settings.queue_channel_id,
            projection_id=projection.id,
            message_id=projection.message_id,
            responded_at=datetime.now(UTC),
        )


def _run_all(runner: OperationRunner) -> int:
    count = 0
    while runner.run_due_once():
        count += 1
    return count


@pytest.mark.integration
def test_explicit_course_intent_executes_without_redundant_approval(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        course_id, account_id = _seed_course(session)
        result = CourseReconciliationService(session).apply_explicit(
            ProposeCourseReconciliationInput(
                record_id=course_id,
                expected_record_version=1,
                mode="sync",
                account_id=account_id,
                calendar_id=_academic_calendar_id(),
                request_key=_request_key("543333333333333333"),
                source=_source("543333333333333333"),
                actor_id=settings.operator_discord_user_id,
            )
        )
        assert result["disposition"] == "execution_queued"
        assert result["authority"] == "explicit_user"
        assert session.scalar(select(Approval)) is None
        queue_item = session.get(QueueItem, uuid.UUID(result["queue_item_id"]))
        assert queue_item is not None and queue_item.presentation == "suppressed"
        operation_id = uuid.UUID(result["operation_id"])
        items = list(
            session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id))
        )
        assert len(items) == 2 and all(item.status == "pending" for item in items)

    runner = OperationRunner(session_factory, FakeCalendarProvider())
    assert runner.run_due_once()
    assert runner.run_due_once()
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None and operation.status == "succeeded"


@pytest.mark.integration
def test_course_sync_rejects_active_pre_lane_series_instead_of_duplicating(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        course_id, account_id = _seed_course(session)
        session.add(
            CalendarLink(
                record_id=course_id,
                meeting_id="lecture-mo",
                origin_kind="course_meeting",
                logical_key=f"course:{course_id}:lecture-mo",
                account_id=account_id,
                calendar_id=get_settings().google_calendar_id,
                external_event_id="legacy-unsorted-course-series",
                provider_etag='"legacy-course"',
                provider_correlation="legacy-unsorted-course-correlation",
                last_synced_version=1,
                recurrence_kind="recurring",
                synced_snapshot={"status": "confirmed"},
            )
        )

    with pytest.raises(DocketError) as exc, session_factory.begin() as session:
        _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="543333333333333334",
        )

    assert exc.value.code == "course_lane_migration_required"


@pytest.mark.integration
def test_course_add_change_partial_drop_restore_and_unchanged_reimport(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        course_id, account_id = _seed_course(session)
        first = _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="544444444444444444",
        )
        assert first["counts"] == {
            "create": 2,
            "update": 0,
            "cancel": 0,
            "no_op": 0,
        }
        revision = session.get(ActionRevision, uuid.UUID(first["action_revision_id"]))
        assert revision is not None
        assert revision.preview["course_date_ranges"] == [
            {
                "start_date": "2026-08-24",
                "end_date": "2026-12-18",
                "timezone": "America/Los_Angeles",
                "start_source": "meeting",
                "end_source": "meeting",
            }
        ]
        first_operation = uuid.UUID(first["operation_id"])

    with session_factory.begin() as session:
        replay = _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="544444444444444444",
        )
        assert replay["disposition"] == "replayed_request"
        assert uuid.UUID(replay["operation_id"]) == first_operation

    with pytest.raises(DocketError) as busy, session_factory.begin() as session:
        _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="546666666666666666",
            mode="drop",
            reason="Conflicting lifecycle request.",
        )
    assert busy.value.code == "course_lifecycle_busy"

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert _run_all(runner) == 2
    with session_factory() as session:
        operation = session.get(Operation, first_operation)
        links = list(
            session.scalars(
                select(CalendarLink)
                .where(CalendarLink.record_id == course_id)
                .order_by(CalendarLink.logical_key)
            )
        )
        assert operation is not None and operation.status == "succeeded"
        assert len(links) == 2
        original_event_ids = {link.logical_key: link.external_event_id for link in links}

    with session_factory() as session:
        course = session.get(Record, course_id)
        assert course is not None
        changed_data = deepcopy(course.data)
    changed_data["meetings"]["lecture-mo"]["location"] = "Room C"
    changed_data["meetings"].pop("lab-we")
    changed_data["meetings"]["discussion-fr"] = _meeting(
        "discussion",
        ["FR"],
        "11:00:00",
        "11:50:00",
        "Room D",
    )
    with session_factory.begin() as session:
        RecordService(session).update(
            UpdateRecordInput(
                record_id=course_id,
                expected_version=1,
                data=changed_data,
                request_key="course-lifecycle:update:1",
                reason="Change one meeting, remove one, and add one.",
            )
        )
        changed = _apply_explicit(
            session,
            record_id=course_id,
            version=2,
            account_id=account_id,
            message_id="555555555555555555",
        )
        assert changed["counts"] == {
            "create": 1,
            "update": 1,
            "cancel": 1,
            "no_op": 0,
        }
        changed_operation = uuid.UUID(changed["operation_id"])

    assert _run_all(runner) == 3
    with session_factory() as session:
        operation = session.get(Operation, changed_operation)
        links = {
            link.logical_key: link
            for link in session.scalars(
                select(CalendarLink).where(CalendarLink.record_id == course_id)
            )
        }
        assert operation is not None and operation.status == "succeeded"
        lecture_key = f"course:{course_id}:lecture-mo"
        removed_key = f"course:{course_id}:lab-we"
        added_key = f"course:{course_id}:discussion-fr"
        assert links[lecture_key].external_event_id == original_event_ids[lecture_key]
        assert links[lecture_key].synced_snapshot["location"] == "Room C"
        assert links[removed_key].synced_snapshot["status"] == "cancelled"
        assert links[added_key].synced_snapshot["status"] != "cancelled"

    with session_factory.begin() as session:
        course = session.get(Record, course_id)
        assert course is not None
        active_links = [
            link
            for link in session.scalars(
                select(CalendarLink).where(CalendarLink.record_id == course_id)
            )
            if link.synced_snapshot.get("status") != "cancelled"
        ]
        for link in active_links:
            snapshot = deepcopy(link.synced_snapshot)
            recurrence = snapshot["recurrence"][0]
            parts = dict(
                part.split("=", 1) for part in recurrence.removeprefix("RRULE:").split(";")
            )
            snapshot["recurrence"] = [
                "RRULE:"
                + ";".join(
                    (
                        f"FREQ={parts['FREQ']}",
                        f"UNTIL={parts['UNTIL']}",
                        f"INTERVAL={parts['INTERVAL']}",
                        f"BYDAY={parts['BYDAY']}",
                    )
                )
            ]
            link.synced_snapshot = snapshot
        repeated = RecordService(session).update(
            UpdateRecordInput(
                record_id=course_id,
                expected_version=2,
                data=deepcopy(course.data),
                request_key="course-lifecycle:update-identical:2",
                reason="Repeat the already-current course state.",
            )
        )
        assert repeated.disposition == "matched_existing"
        assert repeated.version == 2
        unchanged = _apply_explicit(
            session,
            record_id=course_id,
            version=2,
            account_id=account_id,
            message_id="566666666666666666",
        )
        assert unchanged["disposition"] == "no_op"
        assert unchanged["counts"] == {
            "create": 0,
            "update": 0,
            "cancel": 0,
            "no_op": 2,
        }

    with session_factory.begin() as session:
        drop = _apply_explicit(
            session,
            record_id=course_id,
            version=2,
            account_id=account_id,
            message_id="577777777777777777",
            mode="drop",
            reason="Operator dropped the course.",
        )
        assert drop["counts"]["cancel"] == 2
        drop_operation = uuid.UUID(drop["operation_id"])

    assert runner.run_due_once()
    provider.next_cancel_outcome = "permanent"
    assert runner.run_due_once()
    assert not runner.run_due_once()
    with session_factory() as session:
        record = session.get(Record, course_id)
        operation = session.get(Operation, drop_operation)
        assert record is not None and record.status == "active" and record.version == 2
        assert operation is not None and operation.status == "partial_failed"
        assert operation.result["counts"]["succeeded"] == 1
        assert operation.result["counts"]["failed"] == 1

    with session_factory.begin() as session:
        retry = _apply_explicit(
            session,
            record_id=course_id,
            version=2,
            account_id=account_id,
            message_id="588888888888888888",
            mode="drop",
            reason="Retry the incomplete course drop.",
        )
        assert retry["counts"]["cancel"] == 1
        retry_operation = uuid.UUID(retry["operation_id"])
    assert _run_all(runner) == 1
    with session_factory() as session:
        record = session.get(Record, course_id)
        operation = session.get(Operation, retry_operation)
        assert record is not None and record.status == "archived" and record.version == 3
        assert operation is not None and operation.status == "succeeded"
        assert all(
            link.synced_snapshot["status"] == "cancelled"
            for link in session.scalars(
                select(CalendarLink).where(CalendarLink.record_id == course_id)
            )
        )

    with session_factory.begin() as session:
        restored = RecordService(session).restore(
            RestoreRecordInput(
                record_id=course_id,
                expected_version=3,
                request_key="course-lifecycle:restore:1",
                reason="Operator re-added the course.",
            )
        )
        assert restored.version == 4
        restore_sync = _apply_explicit(
            session,
            record_id=course_id,
            version=4,
            account_id=account_id,
            message_id="599999999999999999",
        )
        assert restore_sync["counts"] == {
            "create": 2,
            "update": 0,
            "cancel": 0,
            "no_op": 0,
        }
        restore_operation = uuid.UUID(restore_sync["operation_id"])
    assert _run_all(runner) == 2
    with session_factory() as session:
        record = session.get(Record, course_id)
        operation = session.get(Operation, restore_operation)
        active_links = [
            link
            for link in session.scalars(
                select(CalendarLink).where(CalendarLink.record_id == course_id)
            )
            if link.synced_snapshot.get("status") != "cancelled"
        ]
        assert record is not None and record.status == "active" and record.version == 4
        assert operation is not None and operation.status == "succeeded"
        assert len(active_links) == 2
        assert all(
            link.external_event_id != original_event_ids.get(link.logical_key)
            for link in active_links
        )

    with session_factory.begin() as session:
        record = session.get(Record, course_id)
        assert record is not None
        reimport_message = "611111111111111111"
        reimport = RecordService(session).store(
            StoreRecordInput(
                record_type="course",
                canonical_identity={
                    "term_record_id": record.data["term_record_id"],
                    "course_code": record.data["course_code"],
                    "section": record.data["section"],
                },
                title=record.title,
                data=CourseData.model_validate(record.data),
                request_key=_request_key(reimport_message),
                source=_source(reimport_message),
                actor_id=get_settings().operator_discord_user_id,
            )
        )
        assert reimport.disposition == "matched_existing"
        final_sync = _apply_explicit(
            session,
            record_id=course_id,
            version=4,
            account_id=account_id,
            message_id="622222222222222222",
        )
        assert final_sync["disposition"] == "no_op"


@pytest.mark.integration
def test_empty_course_reconciliation_cancels_links_without_archiving(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        course_id, account_id = _seed_course(session)
        applied = _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="633333333333333333",
        )
        assert applied["disposition"] == "execution_queued"
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert _run_all(runner) == 2

    with session_factory.begin() as session:
        record = session.get(Record, course_id)
        assert record is not None
        empty_data = dict(record.data)
        empty_data["meetings"] = {}
        RecordService(session).update(
            UpdateRecordInput(
                record_id=course_id,
                expected_version=1,
                data=empty_data,
                request_key="course-lifecycle:empty:1",
                reason="Remove every meeting without dropping the course.",
            )
        )
        applied = _apply_explicit(
            session,
            record_id=course_id,
            version=2,
            account_id=account_id,
            message_id="644444444444444444",
        )
        assert applied["counts"]["cancel"] == 2
    assert _run_all(runner) == 2
    with session_factory() as session:
        record = session.get(Record, course_id)
        assert record is not None and record.status == "active" and record.version == 2


@pytest.mark.integration
def test_course_approval_tolerates_unrelated_snapshot_refresh(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        course_id, account_id = _seed_course(session)
        _seed_course_conflict(session, account_id=account_id)
        proposal = _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="655555555555555555",
        )
        assert proposal["disposition"] == "proposed"
        assert proposal["preview"]["conflicts"]

    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    projected = next(iter(backend.messages.values()))
    assert projected["embed"]["title"] == "Review course changes"
    assert [field["name"] for field in projected["embed"]["fields"][:4]] == [
        "Course",
        "Calendar",
        "Course dates",
        "Notifications",
    ]
    assert projected["embed"]["fields"][1]["value"] == "Academic"
    assert projected["embed"]["fields"][3]["value"] == "10 minutes beforehand"
    assert "<t:" in projected["embed"]["fields"][2]["value"]
    assert "<t:" in projected["embed"]["fields"][4]["value"]
    assert [control["label"] for control in projected["controls"]] == [
        "Reject",
        "Snooze until tomorrow",
        "Conflict outcome",
    ]
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        assert projection is not None
        assert projection.view_mode == "decision"
    approval_response = _select_keep_both_and_prepare_approval(
        session_factory,
        projection_runner,
        backend,
        proposal,
        "course-refresh-unrelated",
    )

    with session_factory.begin() as session:
        state = session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == account_id,
                CalendarSyncState.calendar_id == _academic_calendar_id(),
            )
        )
        assert state is not None and state.last_success_at is not None
        refreshed_at = state.last_success_at + timedelta(seconds=1)
        generation = uuid.uuid4()
        state.snapshot_generation = generation
        state.last_attempt_at = refreshed_at
        state.last_success_at = refreshed_at
        session.add(
            CalendarEventCache(
                account_id=account_id,
                calendar_id=_academic_calendar_id(),
                provider_event_id="unrelated-course-write",
                snapshot_generation=generation,
                status="confirmed",
                summary="Unrelated approved course",
                location="Elsewhere",
                is_all_day=False,
                start_at=datetime(2026, 8, 24, 20, 0, tzinfo=UTC),
                end_at=datetime(2026, 8, 24, 20, 50, tzinfo=UTC),
                timezone="America/Los_Angeles",
                recurrence_kind="one_time",
                system_tags=["one_time", "timed", "course_meeting"],
                operator_tags=[],
                priority="normal",
                priority_basis="default",
                provider_reminders={},
                provider_etag='"unrelated-course"',
                synced_at=refreshed_at,
            )
        )

    with session_factory.begin() as session:
        accepted = ApprovalService(session).respond(approval_response)
        operation_id = uuid.UUID(str(accepted["operation_id"]))

    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None and operation.status == "pending"


@pytest.mark.integration
def test_course_approval_rejects_changed_conflict_dependency(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        course_id, account_id = _seed_course(session)
        _seed_course_conflict(session, account_id=account_id)
        proposal = _apply_explicit(
            session,
            record_id=course_id,
            version=1,
            account_id=account_id,
            message_id="666666666666666666",
        )
        assert proposal["disposition"] == "proposed"

    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    approval_response = _select_keep_both_and_prepare_approval(
        session_factory,
        projection_runner,
        backend,
        proposal,
        "course-refresh-conflict",
    )

    with session_factory.begin() as session:
        state = session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == account_id,
                CalendarSyncState.calendar_id == _academic_calendar_id(),
            )
        )
        assert state is not None and state.last_success_at is not None
        refreshed_at = state.last_success_at + timedelta(seconds=1)
        generation = uuid.uuid4()
        state.snapshot_generation = generation
        state.last_attempt_at = refreshed_at
        state.last_success_at = refreshed_at
        session.add(
            CalendarEventCache(
                account_id=account_id,
                calendar_id=_academic_calendar_id(),
                provider_event_id="new-course-conflict",
                snapshot_generation=generation,
                status="confirmed",
                summary="Newly synchronized conflict",
                location="Elsewhere",
                is_all_day=False,
                start_at=datetime(2026, 8, 24, 16, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 24, 16, 20, tzinfo=UTC),
                timezone="America/Los_Angeles",
                recurrence_kind="one_time",
                system_tags=["one_time", "timed", "external"],
                operator_tags=[],
                priority="normal",
                priority_basis="default",
                provider_reminders={},
                provider_etag='"course-conflict"',
                synced_at=refreshed_at,
            )
        )

    with pytest.raises(DocketError) as stale, session_factory.begin() as session:
        ApprovalService(session).respond(approval_response)
    assert stale.value.code == "target_version_changed"
    assert "targets or conflicts changed" in stale.value.message
