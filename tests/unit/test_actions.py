import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import canonical_record_key, sha256_json
from docket.domain.errors import DocketError
from docket.internal_api.schemas import ApprovalResponse
from docket.models import (
    Account,
    Action,
    ActionRevision,
    Approval,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OutboxEvent,
    QueueItem,
    Record,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.schemas.actions import ProposeActionInput
from docket.services.actions import ActionService
from docket.services.approvals import ApprovalService
from docket.services.discord_projection import DiscordProjectionRunner

OPERATOR_ID = "000000000000000001"
GUILD_ID = "000000000000000002"
CHAT_CHANNEL_ID = "000000000000000003"
MESSAGE_ID = "111111111111111111"


def action_fixture(session: Session, *, complete: bool = True) -> tuple[Record, Account]:
    term = Record(
        record_type="term",
        canonical_key="term:cal-poly:fall-2026",
        schema_version=1,
        title="Fall 2026",
        status="active",
        data={
            "institution": "Cal Poly",
            "term_name": "Fall 2026",
            "start_date": "2026-08-24",
            "end_date": "2026-12-18",
            "timezone": "America/Los_Angeles",
            "notes": None,
        },
    )
    session.add(term)
    session.flush()
    meeting = {
        "meeting_type": "lecture",
        "days": ["MO", "WE"],
        "start_time": "10:30:00" if complete else None,
        "end_time": "11:50:00" if complete else None,
        "location": "Building 14",
        "start_date": "2026-08-24" if complete else None,
        "end_date": "2026-12-18" if complete else None,
        "timezone": "America/Los_Angeles" if complete else None,
    }
    course_data = {
        "term_record_id": str(term.id),
        "course_code": "CSC 101",
        "course_title": "Fundamentals of Computer Science",
        "section": "01",
        "instructor": None,
        "meetings": {"lecture-mo-we-1": meeting},
        "notes": None,
    }
    course = Record(
        record_type="course",
        canonical_key=canonical_record_key(
            "course",
            {"term_record_id": term.id, "course_code": "CSC 101", "section": "01"},
        ),
        schema_version=1,
        title="CSC 101-01",
        status="active",
        data=course_data,
    )
    account = Account(
        provider="google",
        external_account_id="primary",
        capabilities=["gmail", "google_calendar"],
        enabled=True,
    )
    session.add_all([course, account])
    session.flush()
    return course, account


def proposal_request(course: Record, account: Account) -> ProposeActionInput:
    request_key = f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:2"
    return ProposeActionInput(
        action_type="calendar_create_meeting",
        record_id=course.id,
        expected_record_version=course.version,
        account_id=account.id,
        parameters={
            "meeting_id": "lecture-mo-we-1",
            "calendar_id": get_settings().google_calendar_id,
        },
        request_key=request_key,
        source={
            "source_type": "discord_message",
            "source_object_id": MESSAGE_ID,
            "metadata": {
                "guild_id": GUILD_ID,
                "channel_id": CHAT_CHANNEL_ID,
                "message_id": MESSAGE_ID,
                "user_id": OPERATOR_ID,
                "intent_index": 2,
            },
        },
        actor_id=OPERATOR_ID,
    )


def approval_response(short_code: str, *, interaction_id: str) -> ApprovalResponse:
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
        message_id="222222222222222222",
        responded_at=datetime.now(UTC),
    )


def test_proposal_derives_immutable_external_write_preview(session: Session) -> None:
    course, account = action_fixture(session)
    service = ActionService(session)

    result = service.propose(proposal_request(course, account))
    session.flush()

    revision = session.get(ActionRevision, result.action_revision_id)
    approval = session.get(Approval, result.approval_id)
    outbox = session.scalar(select(OutboxEvent))
    assert revision is not None and approval is not None and outbox is not None
    assert revision.risk_class == "external_private_write"
    assert revision.parameters["record_version"] == 1
    assert revision.parameters["schedule"]["first_occurrence_date"] == "2026-08-24"
    assert revision.parameters_sha256 == sha256_json(revision.parameters)
    assert revision.preview_sha256 == sha256_json(revision.preview)
    assert approval.status == "pending"
    assert outbox.payload["short_code"] == result.short_code
    assert session.scalar(select(Operation)) is None

    session.commit()
    replay = service.propose(proposal_request(course, account))
    assert replay.action_id == result.action_id
    assert replay.disposition == "replayed_request"


def test_incomplete_meeting_cannot_be_proposed(session: Session) -> None:
    course, account = action_fixture(session, complete=False)

    with pytest.raises(DocketError) as raised:
        ActionService(session).propose(proposal_request(course, account))

    assert raised.value.code == "action_unavailable"
    assert raised.value.details is not None
    assert set(raised.value.details["missing_fields"]) == {
        "start_time",
        "end_time",
        "start_date",
        "end_date",
        "timezone",
    }


def test_caller_cannot_substitute_calendar_or_risk(session: Session) -> None:
    course, account = action_fixture(session)
    payload = proposal_request(course, account).model_dump(mode="json")
    payload["parameters"]["calendar_id"] = "attacker-calendar"

    with pytest.raises(DocketError) as raised:
        ActionService(session).propose(ProposeActionInput.model_validate(payload))
    assert raised.value.code == "calendar_not_allowed"

    payload["risk_class"] = "read_only"
    with pytest.raises(ValidationError, match="risk_class"):
        ProposeActionInput.model_validate(payload)


def test_authenticated_approval_is_consumed_once_and_queues_operation(
    session: Session,
) -> None:
    course, account = action_fixture(session)
    proposal = ActionService(session).propose(proposal_request(course, account))

    result = ApprovalService(session).respond(
        approval_response(proposal.short_code.lower(), interaction_id="interaction-1")
    )
    session.flush()

    approval = session.get(Approval, proposal.approval_id)
    action = session.get(Action, proposal.action_id)
    queue_item = session.get(QueueItem, proposal.queue_item_id)
    operation = session.get(Operation, uuid.UUID(result["operation_id"]))
    assert approval is not None and action is not None and queue_item is not None
    assert operation is not None
    assert approval.status == "consumed"
    assert approval.consumed_operation_id == operation.id
    assert action.status == "ready"
    assert queue_item.status == "executing"
    assert operation.status == "pending"
    assert operation.idempotency_key.endswith(":lecture-mo-we-1:1")

    with pytest.raises(DocketError) as replayed:
        ApprovalService(session).respond(
            approval_response(proposal.short_code, interaction_id="interaction-1")
        )
    assert replayed.value.code == "interaction_replay"


def test_forged_context_and_changed_target_cannot_consume_approval(session: Session) -> None:
    course, account = action_fixture(session)
    proposal = ActionService(session).propose(proposal_request(course, account))
    forged = approval_response(proposal.short_code, interaction_id="interaction-forged")
    forged.discord_user_id = "999999999999999999"

    with pytest.raises(DocketError) as rejected:
        ApprovalService(session).respond(forged)
    assert rejected.value.code == "invalid_approval_context"
    assert session.get(Approval, proposal.approval_id).status == "pending"

    course.version += 1
    with pytest.raises(DocketError) as stale:
        ApprovalService(session).respond(
            approval_response(proposal.short_code, interaction_id="interaction-stale")
        )
    assert stale.value.code == "target_version_changed"


def test_projection_retry_restart_and_exact_button_context(session_factory) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        course, account = action_fixture(session)
        proposal = ActionService(session).propose(proposal_request(course, account))

    backend = FakeDiscordBackend()
    first_adapter = FakeDiscordProjectionAdapter(backend)
    first_adapter.discard_next_projection_ack = True
    first_runner = DiscordProjectionRunner(session_factory, first_adapter, settings)
    assert first_runner.run_due_once()
    assert len(backend.threads) == 1
    assert len(backend.messages) == 1

    with session_factory.begin() as session:
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "discord.projection.requested")
        )
        assert outbox is not None
        assert outbox.status == "pending"
        outbox.next_attempt_at = None

    # A new adapter instance models a Hermes plugin restart. The fake Discord
    # backend persists, so marker recovery must return the same thread/card.
    restarted = FakeDiscordProjectionAdapter(backend)
    second_runner = DiscordProjectionRunner(session_factory, restarted, settings)
    assert second_runner.run_due_once()
    assert len(backend.threads) == 1
    assert len(backend.messages) == 1

    with session_factory() as session:
        daily_thread = session.scalar(select(DiscordDailyThread))
        projection = session.scalar(select(DiscordProjection))
        approval = session.get(Approval, proposal.approval_id)
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "discord.projection.requested")
        )
        assert daily_thread is not None and projection is not None and approval is not None
        assert outbox is not None and outbox.status == "delivered"
        assert projection.status == "delivered"
        assert approval.control_projection_id == projection.id
        assert projection.message_id is not None and daily_thread.thread_id is not None
        control = backend.messages[str(projection.id)]["controls"][0]

    forged = ApprovalResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="button-forged",
        approval_id=proposal.approval_id,
        approval_token=control["token"],
        short_code=None,
        decision="approve",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=daily_thread.thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection.id,
        message_id="99999999999999999",
        responded_at=datetime.now(UTC),
    )
    with session_factory.begin() as session, pytest.raises(DocketError) as rejected:
        ApprovalService(session).respond(forged)
    assert rejected.value.code == "invalid_approval_projection"

    exact = forged.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "discord_interaction_id": "button-exact",
            "message_id": projection.message_id,
        }
    )
    with session_factory.begin() as session:
        result = ApprovalService(session).respond(exact)
    assert result["ok"] is True
    assert result["operation_id"] is not None


def test_fake_thread_ensure_and_archive_are_idempotent() -> None:
    settings = get_settings()
    backend = FakeDiscordBackend()
    adapter = FakeDiscordProjectionAdapter(backend)
    daily_thread_id = uuid.uuid4()
    payload = {
        "request_id": str(uuid.uuid4()),
        "daily_thread_id": str(daily_thread_id),
        "known_thread_id": None,
        "guild_id": settings.discord_guild_id,
        "channel_id": settings.queue_channel_id,
        "local_date": "2026-07-21",
        "name": "2026-07-21 — Tuesday",
        "thread_type": "public_thread",
        "auto_archive_minutes": 10080,
    }
    first = adapter.ensure_thread(payload)
    second = adapter.ensure_thread({**payload, "request_id": str(uuid.uuid4())})
    assert first["thread_id"] == second["thread_id"]
    assert first["created"] is True
    assert second["created"] is False

    lifecycle = {
        "request_id": str(uuid.uuid4()),
        "guild_id": settings.discord_guild_id,
        "parent_channel_id": settings.queue_channel_id,
        "thread_id": first["thread_id"],
        "desired_state": "archived",
    }
    archived = adapter.set_thread_lifecycle(daily_thread_id, lifecycle)
    replayed = adapter.set_thread_lifecycle(
        daily_thread_id, {**lifecycle, "request_id": str(uuid.uuid4())}
    )
    assert archived["archived"] is True
    assert replayed["archived"] is True
