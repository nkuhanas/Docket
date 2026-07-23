import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.domain.errors import DocketError
from docket.internal_api.schemas import ApprovalResponse, LocalActionResponse
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    AuditEvent,
    CalendarReminderPlan,
    CalendarScheduleSnapshot,
    CalendarSyncState,
    CommandRequest,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OperationItem,
    OutboxEvent,
    QueueItem,
    Record,
    RecordSource,
    ReminderRule,
)
from docket.providers.discord import (
    FakeDiscordBackend,
    FakeDiscordProjectionAdapter,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.actions import ProposalResult, ProposeTermScheduleInput
from docket.schemas.records import StoreTermScheduleInput
from docket.services.approvals import ApprovalService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.operations import OperationRunner
from docket.services.proposal_controls import ProposalControlService
from docket.services.schedule_actions import TermScheduleActionService
from docket.services.schedules import TermScheduleService


def _source(message_id: str) -> dict:
    settings = get_settings()
    return {
        "source_type": "discord_message",
        "source_object_id": message_id,
        "metadata": {
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "user_id": settings.operator_discord_user_id,
            "intent_index": 0,
        },
    }


def _request_key(message_id: str) -> str:
    settings = get_settings()
    return f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"


def _schedule(
    message_id: str,
    *,
    location: str = "Building 14",
    include_second_course: bool = False,
) -> StoreTermScheduleInput:
    settings = get_settings()
    courses = [
        {
            "course_code": "CSC 101",
            "course_title": "Fundamentals",
            "section": "01",
            "instructor": "Professor Example",
            "meetings": {
                "lecture-mo-we": {
                    "meeting_type": "lecture",
                    "days": ["MO", "WE"],
                    "start_time": "10:30:00",
                    "end_time": "11:50:00",
                    "location": location,
                    "start_date": "2026-08-24",
                    "end_date": "2026-12-18",
                    "timezone": "America/Los_Angeles",
                    "excluded_dates": ["2026-11-25"],
                    "additional_occurrences": [
                        {
                            "occurrence_id": "final-2026-12-08",
                            "date": "2026-12-08",
                            "start_time": "10:10:00",
                            "end_time": "13:00:00",
                            "location": "Building 14-246",
                        }
                    ],
                }
            },
        }
    ]
    if include_second_course:
        courses.append(
            {
                "course_code": "MATH 141",
                "course_title": "Calculus I",
                "section": "02",
                "meetings": {
                    "lecture-tu-th": {
                        "meeting_type": "lecture",
                        "days": ["TU", "TH"],
                        "start_time": "13:10:00",
                        "end_time": "14:30:00",
                        "location": "Building 38",
                        "start_date": "2026-08-25",
                        "end_date": "2026-12-17",
                        "timezone": "America/Los_Angeles",
                    }
                },
            }
        )
    return StoreTermScheduleInput.model_validate(
        {
            "term": {
                "kind": "new",
                "canonical_identity": {
                    "institution": ("California Polytechnic State University, San Luis Obispo"),
                    "term_name": "Fall 2026",
                },
                "title": "Fall 2026",
                "data": {
                    "institution": ("California Polytechnic State University, San Luis Obispo"),
                    "term_name": "Fall 2026",
                    "start_date": "2026-08-24",
                    "end_date": "2026-12-18",
                    "timezone": "America/Los_Angeles",
                },
            },
            "courses": courses,
            "request_key": _request_key(message_id),
            "source": _source(message_id),
            "actor_id": settings.operator_discord_user_id,
        }
    )


def _store_and_propose(
    session_factory: sessionmaker[Session],
    *,
    store_message_id: str,
    proposal_message_id: str,
    include_second_course: bool = False,
    request: StoreTermScheduleInput | None = None,
) -> tuple[ProposalResult, uuid.UUID]:
    settings = get_settings()
    with session_factory.begin() as session:
        stored = TermScheduleService(session).store(
            request
            or _schedule(
                store_message_id,
                include_second_course=include_second_course,
            )
        )
        account = Account(
            provider="google",
            external_account_id=f"schedule-{store_message_id}",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        now = datetime.now(UTC)
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=settings.google_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                snapshot_generation=uuid.uuid4(),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
        proposal = TermScheduleActionService(session).propose(
            ProposeTermScheduleInput(
                schedule_snapshot_id=stored.schedule_snapshot_id,
                account_id=account.id,
                calendar_id=settings.google_calendar_id,
                request_key=_request_key(proposal_message_id),
                source=_source(proposal_message_id),
                actor_id=settings.operator_discord_user_id,
            )
        )
        return proposal, account.id


def _approve_schedule(
    session: Session,
    *,
    short_code: str,
    interaction_id: str,
) -> uuid.UUID:
    settings = get_settings()
    result = ApprovalService(session).respond(
        ApprovalResponse(
            request_id=uuid.uuid4(),
            discord_interaction_id=interaction_id,
            approval_id=None,
            approval_token=None,
            short_code=short_code,
            decision="approve",
            discord_user_id=settings.operator_discord_user_id,
            guild_id=settings.discord_guild_id,
            channel_id=settings.queue_channel_id,
            message_id="377777777777777777",
            responded_at=datetime.now(UTC),
        )
    )
    return uuid.UUID(result["operation_id"])


@pytest.mark.integration
def test_complete_schedule_store_is_atomic_source_bound_and_replayable(
    session_factory,
) -> None:
    request = _schedule("311111111111111111")
    with session_factory.begin() as session:
        result = TermScheduleService(session).store(request)
        snapshot_id = result.schedule_snapshot_id
        assert result.item_count == 2
        assert result.disposition == "stored"

    with session_factory() as session:
        records = list(session.scalars(select(Record).order_by(Record.record_type.desc())))
        sources = list(session.scalars(select(RecordSource)))
        snapshot = session.get(CalendarScheduleSnapshot, snapshot_id)
        command = session.get(CommandRequest, result.request_id)
        assert len(records) == 2
        assert len(sources) == 2
        assert {source.source_request_key for source in sources} == {request.request_key}
        course = next(record for record in records if record.record_type == "course")
        assert course.schema_version == 2
        assert snapshot is not None and command is not None
        assert snapshot.manifest_sha256 == result.manifest_sha256
        assert snapshot.item_count == 2
        assert [item["item_type"] for item in snapshot.manifest["items"]] == [
            "recurring_series",
            "exception_occurrence",
        ]
        assert all(len(item["item_sha256"]) == 64 for item in snapshot.manifest["items"])

    with session_factory.begin() as session:
        replay = TermScheduleService(session).store(request)
    assert replay.disposition == "replayed_request"
    assert replay.schedule_snapshot_id == snapshot_id


@pytest.mark.integration
def test_one_course_conflict_rolls_back_new_records_and_all_new_provenance(
    session_factory,
) -> None:
    first = _schedule("322222222222222222")
    with session_factory.begin() as session:
        TermScheduleService(session).store(first)
    conflicting = _schedule(
        "333333333333333333",
        location="Changed room",
        include_second_course=True,
    )
    with pytest.raises(DocketError) as rejected, session_factory.begin() as session:
        TermScheduleService(session).store(conflicting)
    assert rejected.value.code == "schedule_record_conflict"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Record)) == 2
        assert session.scalar(select(func.count()).select_from(RecordSource)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(RecordSource)
                .where(RecordSource.source_request_key == conflicting.request_key)
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(CalendarScheduleSnapshot)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(CommandRequest)
                .where(CommandRequest.request_key == conflicting.request_key)
            )
            == 0
        )


def test_schedule_schema_rejects_a_fifty_first_compiled_item() -> None:
    payload = _schedule("344444444444444444").model_dump(mode="json")
    meeting = payload["courses"][0]["meetings"]["lecture-mo-we"]
    meeting["additional_occurrences"] = [
        {
            "occurrence_id": f"extra-{index}",
            "date": "2026-12-01",
            "start_time": "15:00:00",
            "end_time": "16:00:00",
            "location": None,
            "out_of_term_confirmed": False,
        }
        for index in range(50)
    ]
    with pytest.raises(ValidationError, match="1 through 50"):
        StoreTermScheduleInput.model_validate(payload)


@pytest.mark.integration
def test_schedule_store_rejects_a_series_with_no_in_range_occurrence(
    session_factory: sessionmaker[Session],
) -> None:
    payload = _schedule("345555555555555555").model_dump(mode="json")
    meeting = payload["courses"][0]["meetings"]["lecture-mo-we"]
    meeting["days"] = ["MO"]
    meeting["start_date"] = "2026-08-25"
    meeting["end_date"] = "2026-08-25"
    meeting["excluded_dates"] = []
    meeting["additional_occurrences"] = []
    request = StoreTermScheduleInput.model_validate(payload)
    with pytest.raises(DocketError) as rejected, session_factory.begin() as session:
        TermScheduleService(session).store(request)
    assert rejected.value.code == "schedule_meeting_has_no_occurrence"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Record)) == 0
        assert session.scalar(select(func.count()).select_from(CommandRequest)) == 0


@pytest.mark.integration
def test_complete_snapshot_produces_one_bulk_proposal_with_one_unified_plan(
    session_factory,
) -> None:
    settings = get_settings()
    store_request = _schedule("355555555555555555")
    with session_factory.begin() as session:
        stored = TermScheduleService(session).store(store_request)
        account = Account(
            provider="google",
            external_account_id="schedule-account",
            capabilities=["google_calendar"],
            enabled=True,
        )
        session.add(account)
        session.flush()
        now = datetime.now(UTC)
        session.add(
            CalendarSyncState(
                account_id=account.id,
                calendar_id=settings.google_calendar_id,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=400),
                snapshot_generation=uuid.uuid4(),
                status="current",
                last_attempt_at=now,
                last_success_at=now,
            )
        )
        account_id = account.id

    message_id = "366666666666666666"
    request = ProposeTermScheduleInput(
        schedule_snapshot_id=stored.schedule_snapshot_id,
        account_id=account_id,
        calendar_id=settings.google_calendar_id,
        request_key=_request_key(message_id),
        source=_source(message_id),
        actor_id=settings.operator_discord_user_id,
    )
    with session_factory.begin() as session:
        proposed = TermScheduleActionService(session).propose(request)
        assert proposed.preview["counts"] == {
            "create": 2,
            "update": 0,
            "no_op": 0,
        }
        assert proposed.preview["item_count"] == 2
    with session_factory() as session:
        revision = session.get(ActionRevision, proposed.action_revision_id)
        plans = list(
            session.scalars(
                select(CalendarReminderPlan).order_by(CalendarReminderPlan.manifest_item_key)
            )
        )
        assert revision is not None
        assert revision.action_type == "calendar_apply_term_schedule"
        assert revision.risk_class == "bulk"
        assert len(revision.parameters["items"]) == 2
        assert len(plans) == 2
        assert {plan.lead_seconds for plan in plans} == {600}
        assert all(plan.status == "planned" for plan in plans)

    with session_factory.begin() as session:
        replay = TermScheduleActionService(session).propose(request)
    assert replay.disposition == "replayed_request"
    assert replay.action_id == proposed.action_id

    duplicate_message_id = "367777777777777777"
    duplicate_request = request.model_copy(
        update={
            "request_key": _request_key(duplicate_message_id),
            "source": request.source.model_copy(
                update={
                    "source_object_id": duplicate_message_id,
                    "metadata": request.source.metadata.model_copy(
                        update={"message_id": duplicate_message_id}
                    ),
                }
            ),
        }
    )
    with session_factory.begin() as session:
        duplicate = TermScheduleActionService(session).propose(duplicate_request)
        assert duplicate.disposition == "matched_existing"
        assert duplicate.request_id != proposed.request_id
        assert duplicate.queue_item_id == proposed.queue_item_id
        assert duplicate.action_id == proposed.action_id
        assert duplicate.approval_id == proposed.approval_id
        assert session.scalar(select(func.count()).select_from(QueueItem)) == 1
        assert session.scalar(select(func.count()).select_from(Action)) == 1
        assert session.scalar(select(func.count()).select_from(Approval)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "action.duplicate_suppressed")
            )
            == 1
        )


@pytest.mark.integration
def test_aggregate_card_exposes_read_only_manifest_review_without_revision_churn(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    proposal, _account_id = _store_and_propose(
        session_factory,
        store_message_id="388888888888888888",
        proposal_message_id="399999999999999999",
        include_second_course=True,
    )
    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert runner.run_due_once()
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and projection.message_id is not None
        assert thread is not None and thread.thread_id is not None
        projected = backend.messages[str(projection.id)]
        review = next(
            control for control in projected["controls"] if control.get("field") == "review_page"
        )
        assert [option["value"] for option in review["options"]] == ["1"]
        assert not any(option["default"] for option in review["options"])
        assert len(projected["embed"]["fields"]) <= 25
        projection_id = projection.id
        message_id = projection.message_id
        thread_id = thread.thread_id

    response = LocalActionResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="schedule-review-page-one",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection_id,
        message_id=message_id,
        responded_at=datetime.now(UTC),
        action_revision_id=proposal.action_revision_id,
        action_token=review["token"],
        transition="proposal_review_page",
        field="review_page",
        value="1",
    )
    with session_factory.begin() as session:
        result = ProposalControlService(session).respond(response)
        assert result["item_count"] == 3
        assert len(result["items"]) == 3
        assert "Schedule items" in str(result["content"])
    with pytest.raises(DocketError) as rejected, session_factory.begin() as session:
        ProposalControlService(session).respond(
            response.model_copy(
                update={
                    "request_id": uuid.uuid4(),
                    "discord_interaction_id": "schedule-review-forged-page",
                    "value": "2",
                }
            )
        )
    assert rejected.value.code == "invalid_schedule_review_page"
    with session_factory() as session:
        action = session.get(Action, proposal.action_id)
        approval = session.scalar(
            select(ActionRevision).where(ActionRevision.id == proposal.action_revision_id)
        )
        assert action is not None and action.current_revision == 1
        assert approval is not None
        assert session.scalar(select(Operation)) is None


@pytest.mark.integration
def test_schedule_approval_executes_every_item_once_and_activates_one_plan(
    session_factory: sessionmaker[Session],
) -> None:
    proposal, _account_id = _store_and_propose(
        session_factory,
        store_message_id="411111111111111111",
        proposal_message_id="422222222222222222",
    )
    with session_factory.begin() as session:
        operation_id = _approve_schedule(
            session,
            short_code=proposal.short_code,
            interaction_id="schedule-approval-complete",
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationItem)
                .where(OperationItem.operation_id == operation_id)
            )
            == 2
        )

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once()
    restarted = OperationRunner(session_factory, provider)
    assert restarted.run_due_once()
    assert not restarted.run_due_once()

    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        action = session.get(Action, proposal.action_id)
        items = list(
            session.scalars(
                select(OperationItem)
                .where(OperationItem.operation_id == operation_id)
                .order_by(OperationItem.item_key)
            )
        )
        plans = list(
            session.scalars(
                select(CalendarReminderPlan).order_by(CalendarReminderPlan.manifest_item_key)
            )
        )
        rules = list(session.scalars(select(ReminderRule)))
        assert operation is not None and operation.status == "succeeded"
        assert action is not None and action.status == "succeeded"
        assert [item.status for item in items] == ["succeeded", "succeeded"]
        assert len(provider.events) == 2
        assert len(plans) == 2
        assert all(plan.status == "activated" for plan in plans)
        assert len(rules) == 2
        assert all(rule.lead_seconds == 600 for rule in rules)


@pytest.mark.integration
def test_schedule_batch_reports_partial_failure_without_replaying_siblings(
    session_factory: sessionmaker[Session],
) -> None:
    proposal, _account_id = _store_and_propose(
        session_factory,
        store_message_id="433333333333333333",
        proposal_message_id="444444444444444444",
        include_second_course=True,
    )
    with session_factory.begin() as session:
        operation_id = _approve_schedule(
            session,
            short_code=proposal.short_code,
            interaction_id="schedule-approval-partial",
        )

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once()
    provider.next_create_outcome = "permanent"
    assert runner.run_due_once()
    assert runner.run_due_once()
    assert not runner.run_due_once()
    event_ids = set(provider.events)
    assert len(event_ids) == 2

    restarted = OperationRunner(session_factory, provider)
    assert not restarted.run_due_once()
    assert set(provider.events) == event_ids
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        action = session.get(Action, proposal.action_id)
        items = list(
            session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id))
        )
        plans = list(
            session.scalars(
                select(CalendarReminderPlan).order_by(CalendarReminderPlan.manifest_item_key)
            )
        )
        assert operation is not None
        assert operation.status == "partial_failed"
        assert operation.result["counts"]["succeeded"] == 2
        assert operation.result["counts"]["failed"] == 1
        assert len(operation.result["failures"]) == 1
        assert action is not None and action.status == "partial_failed"
        assert sorted(item.status for item in items) == [
            "failed",
            "succeeded",
            "succeeded",
        ]
        assert sorted(plan.status for plan in plans) == [
            "activated",
            "activated",
            "cancelled",
        ]

    settings = get_settings()
    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    while projection_runner.run_due_once():
        pass
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and projection.message_id is not None
        assert thread is not None and thread.thread_id is not None
        review = next(
            control
            for control in backend.messages[str(projection.id)]["controls"]
            if control.get("field") == "review_page"
        )
        embed = backend.messages[str(projection.id)]["embed"]
        aggregate_embed_characters = (
            len(embed["title"])
            + len(embed["description"])
            + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
        )
        assert len(embed["fields"]) <= 25
        assert aggregate_embed_characters < 6000
        assert [option["value"] for option in review["options"]] == [
            "1",
            "failures",
        ]
        projection_id = projection.id
        message_id = projection.message_id
        thread_id = thread.thread_id
    with session_factory.begin() as session:
        failure_page = ProposalControlService(session).respond(
            LocalActionResponse(
                request_id=uuid.uuid4(),
                discord_interaction_id="schedule-review-failures",
                discord_user_id=settings.operator_discord_user_id,
                guild_id=settings.discord_guild_id,
                channel_id=thread_id,
                parent_channel_id=settings.queue_channel_id,
                projection_id=projection_id,
                message_id=message_id,
                responded_at=datetime.now(UTC),
                action_revision_id=proposal.action_revision_id,
                action_token=review["token"],
                transition="proposal_review_page",
                field="review_page",
                value="failures",
            )
        )
        assert failure_page["value"] == "failures"
        assert len(failure_page["items"]) == 1
        assert "fake_permanent" in str(failure_page["content"])


@pytest.mark.integration
def test_schedule_unknown_item_reconciles_without_duplicate_creation(
    session_factory: sessionmaker[Session],
) -> None:
    proposal, _account_id = _store_and_propose(
        session_factory,
        store_message_id="455555555555555555",
        proposal_message_id="466666666666666666",
    )
    with session_factory.begin() as session:
        operation_id = _approve_schedule(
            session,
            short_code=proposal.short_code,
            interaction_id="schedule-approval-reconcile",
        )

    provider = FakeCalendarProvider()
    provider.next_create_outcome = "unknown_after_write"
    runner = OperationRunner(session_factory, provider, consistency_window_seconds=0)
    assert runner.run_due_once()
    assert len(provider.events) == 1
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None
        assert operation.status == "reconciliation_required"
        assert sorted(plan.status for plan in session.scalars(select(CalendarReminderPlan))) == [
            "planned",
            "reconciliation_required",
        ]

    assert runner.reconcile_once()
    assert len(provider.events) == 1
    assert runner.run_due_once()
    assert not runner.run_due_once()
    assert len(provider.events) == 2
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None and operation.status == "succeeded"
        assert all(
            plan.status == "activated" for plan in session.scalars(select(CalendarReminderPlan))
        )


@pytest.mark.integration
def test_schedule_approval_rejects_changed_calendar_snapshot(
    session_factory: sessionmaker[Session],
) -> None:
    proposal, account_id = _store_and_propose(
        session_factory,
        store_message_id="467777777777777777",
        proposal_message_id="468888888888888888",
    )
    settings = get_settings()
    with session_factory.begin() as session:
        state = session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.account_id == account_id,
                CalendarSyncState.calendar_id == settings.google_calendar_id,
            )
        )
        assert state is not None and state.last_success_at is not None
        state.last_success_at = state.last_success_at + timedelta(seconds=1)
    with pytest.raises(DocketError) as rejected, session_factory.begin() as session:
        _approve_schedule(
            session,
            short_code=proposal.short_code,
            interaction_id="schedule-approval-stale-calendar",
        )
    assert rejected.value.code == "target_version_changed"
    with session_factory() as session:
        assert session.scalar(select(Operation)) is None


@pytest.mark.integration
def test_fifty_item_schedule_survives_restart_and_partial_failure_without_replay(
    session_factory: sessionmaker[Session],
) -> None:
    request = _schedule("477777777777777777")
    payload = request.model_dump(mode="json")
    meeting = payload["courses"][0]["meetings"]["lecture-mo-we"]
    meeting["additional_occurrences"] = [
        {
            "occurrence_id": f"makeup-{index:02d}",
            "date": "2026-12-01",
            "start_time": "15:00:00",
            "end_time": "16:00:00",
            "location": f"Room {index:02d}",
            "out_of_term_confirmed": False,
        }
        for index in range(49)
    ]
    bounded = StoreTermScheduleInput.model_validate(payload)
    proposal, _account_id = _store_and_propose(
        session_factory,
        store_message_id="477777777777777777",
        proposal_message_id="488888888888888888",
        request=bounded,
    )
    assert any(
        conflict.get("kind") == "schedule_overlap" for conflict in proposal.preview["conflicts"]
    )
    settings = get_settings()
    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert projection_runner.run_due_once()
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        assert projection is not None
        review = next(
            control
            for control in backend.messages[str(projection.id)]["controls"]
            if control.get("field") == "review_page"
        )
        embed = backend.messages[str(projection.id)]["embed"]
        assert (
            len(embed["title"])
            + len(embed["description"])
            + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
            < 6000
        )
        assert len(embed["fields"]) <= 25
        assert [option["value"] for option in review["options"]] == [
            "1",
            "2",
            "3",
            "4",
            "5",
        ]
    with session_factory.begin() as session:
        operation_id = _approve_schedule(
            session,
            short_code=proposal.short_code,
            interaction_id="schedule-approval-fifty",
        )

    provider = FakeCalendarProvider()
    first_runner = OperationRunner(session_factory, provider)
    for _ in range(13):
        assert first_runner.run_due_once()
    assert len(provider.events) == 13
    restarted = OperationRunner(session_factory, provider)
    provider.next_create_outcome = "permanent"
    assert restarted.run_due_once()
    processed_after_restart = 0
    while restarted.run_due_once():
        processed_after_restart += 1
    assert processed_after_restart == 36
    assert len(provider.events) == 49
    assert not OperationRunner(session_factory, provider).run_due_once()
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None and operation.status == "partial_failed"
        assert operation.result["counts"]["succeeded"] == 49
        assert operation.result["counts"]["failed"] == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationItem)
                .where(
                    OperationItem.operation_id == operation_id,
                    OperationItem.status == "succeeded",
                )
            )
            == 49
        )
