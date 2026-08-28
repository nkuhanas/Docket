import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import get_settings
from docket.domain.canonical import sha256_json
from docket.domain.errors import DocketError
from docket.models import (
    Account,
    Action,
    ActionRevision,
    CalendarEventCache,
    CalendarLane,
    CalendarProfile,
    CalendarReminderPlan,
    CalendarSyncState,
    Operation,
    QueueItem,
    ReminderRule,
)
from docket.schemas.actions import ProposeCalendarEventInput
from docket.schemas.calendar import SetCalendarProfileInput
from docket.services.action_read import ActionReadService
from docket.services.calendar_actions import CalendarActionService
from docket.services.calendar_profile import CalendarProfileService

OPERATOR_ID = "000000000000000001"
GUILD_ID = "000000000000000002"
CHAT_CHANNEL_ID = "000000000000000003"
MESSAGE_ID = "111111111111111111"


def trusted_source(intent_index: int) -> dict:
    return {
        "source_type": "discord_message",
        "source_object_id": MESSAGE_ID,
        "metadata": {
            "guild_id": GUILD_ID,
            "channel_id": CHAT_CHANNEL_ID,
            "message_id": MESSAGE_ID,
            "user_id": OPERATOR_ID,
            "intent_index": intent_index,
        },
    }


def calendar_fixture(session: Session) -> tuple[Account, CalendarSyncState]:
    account = Account(
        provider="google",
        external_account_id="primary",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    now = datetime.now(UTC)
    state = CalendarSyncState(
        account_id=account.id,
        calendar_id=get_settings().google_calendar_id,
        window_start=now - timedelta(days=30),
        window_end=now + timedelta(days=400),
        snapshot_generation=None,
        status="current",
        last_attempt_at=now,
        last_success_at=now,
    )
    session.add(state)
    session.flush()
    return account, state


def create_request(account: Account, *, intent_index: int = 0) -> ProposeCalendarEventInput:
    return ProposeCalendarEventInput.model_validate(
        {
            "account_id": str(account.id),
            "calendar_id": get_settings().google_calendar_id,
            "proposal": {
                "kind": "create",
                "event": {
                    "title": "Check my email",
                    "timing": {
                        "kind": "timed",
                        "start_local": "2026-07-30T12:00:00",
                        "end_local": "2026-07-30T12:15:00",
                        "timezone": "America/Los_Angeles",
                    },
                    "operator_tags": ["Email"],
                },
            },
            "request_key": (f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:{intent_index}"),
            "source": trusted_source(intent_index),
            "actor_id": OPERATOR_ID,
        }
    )


def cached_event(
    session: Session,
    account: Account,
    *,
    provider_event_id: str = "provider-event-1",
    has_attendees: bool = False,
) -> CalendarEventCache:
    event = CalendarEventCache(
        account_id=account.id,
        calendar_id=get_settings().google_calendar_id,
        provider_event_id=provider_event_id,
        snapshot_generation=uuid.uuid4(),
        status="confirmed",
        summary="Existing event",
        location="Room 1",
        is_all_day=False,
        start_at=datetime(2026, 7, 30, 18, 30, tzinfo=UTC),
        end_at=datetime(2026, 7, 30, 19, 30, tzinfo=UTC),
        timezone="America/Los_Angeles",
        has_attendees=has_attendees,
        organizer_is_self=True,
        provider_reminders={"useDefault": True, "overrides": []},
        provider_etag='"etag-1"',
        synced_at=datetime.now(UTC),
    )
    session.add(event)
    session.flush()
    return event


def test_calendar_profile_initializes_and_updates_with_versioning(
    session: Session,
) -> None:
    service = CalendarProfileService(session)
    initial = service.get()
    assert initial.version == 1
    assert initial.default_reminder_lead_seconds == [600]
    account, _state = calendar_fixture(session)
    rule = ReminderRule(
        account_id=account.id,
        calendar_id=get_settings().google_calendar_id,
        scope="calendar",
        provider_event_id=None,
        lead_seconds=600,
        queue_channel_id=get_settings().queue_channel_id,
        enabled=True,
        created_by_actor_id=OPERATOR_ID,
    )
    session.add(rule)
    session.flush()

    request = SetCalendarProfileInput(
        expected_version=1,
        proposal_mode="explicit_only",
        default_reminder_lead_seconds=[300, 600],
        default_reminder_delivery_channels=["google_popup"],
        conflict_policy="block",
        request_key=f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:4",
        source=trusted_source(4),
        actor_id=OPERATOR_ID,
    )
    updated = service.set(request)

    assert updated.version == 2
    assert updated.proposal_mode == "explicit_only"
    assert updated.default_reminder_lead_seconds == [300, 600]
    assert updated.default_reminder_delivery_channels == ["google_popup"]
    assert rule.enabled is False and rule.version == 2
    assert session.scalar(select(CalendarProfile)) is not None


def test_standalone_create_proposal_uses_profile_reminder_and_conflict_scan(
    session: Session,
) -> None:
    account, _state = calendar_fixture(session)
    cached_event(session, account)
    request = create_request(account)

    result = CalendarActionService(session).apply_explicit(request)
    session.flush()

    revision = session.get(ActionRevision, result.action_revision_id)
    queue_item = session.get(QueueItem, result.queue_item_id)
    plans = list(
        session.scalars(
            select(CalendarReminderPlan).where(
                CalendarReminderPlan.action_revision_id == result.action_revision_id
            )
        )
    )
    assert revision is not None and queue_item is not None
    assert revision.action_type == "calendar_create_event"
    assert revision.parameters["logical_key"].startswith("canonical_event:")
    assert (
        revision.parameters["logical_key"].removeprefix("canonical_event:")
        == (revision.parameters["canonical_event_id"])
    )
    assert revision.parameters["reminder_plan"]["lead_seconds"] == [600]
    assert revision.parameters["reminder_plan_sha256"] == sha256_json(
        revision.parameters["reminder_plan"]
    )
    assert revision.preview["conflicts"][0]["provider_event_id"] == "provider-event-1"
    assert queue_item.priority == "normal"
    assert len(plans) == 1 and plans[0].lead_seconds == 600

    session.commit()
    replay = CalendarActionService(session).apply_explicit(request)
    assert replay.disposition == "replayed_request"
    assert replay.action_id == result.action_id


def test_conflict_scan_spans_every_active_calendar_lane(session: Session) -> None:
    account, _state = calendar_fixture(session)
    personal_calendar_id = "personal@group.calendar.google.com"
    session.add(
        CalendarLane(
            account_id=account.id,
            lane="personal",
            display_name="Docket · Personal",
            color_hex="#8E24AA",
            calendar_id=personal_calendar_id,
            status="active",
        )
    )
    event = cached_event(session, account, provider_event_id="personal-conflict")
    event.calendar_id = personal_calendar_id
    session.flush()

    result = CalendarActionService(session).apply_explicit(create_request(account))
    revision = session.get(ActionRevision, result.action_revision_id)

    assert revision is not None
    assert [value["provider_event_id"] for value in revision.preview["conflicts"]] == [
        "personal-conflict"
    ]


def test_event_lane_must_match_destination_calendar(session: Session) -> None:
    account, _state = calendar_fixture(session)
    request = create_request(account).model_copy(deep=True)
    assert request.proposal.kind == "create"
    request.proposal.event.calendar_lane = "personal"

    with pytest.raises(DocketError) as rejected:
        CalendarActionService(session).apply_explicit(request)

    assert rejected.value.code == "calendar_lane_mismatch"


def test_profile_block_policy_cannot_turn_overlap_into_validation_failure(
    session: Session,
) -> None:
    account, _state = calendar_fixture(session)
    cached_event(session, account)
    profile = CalendarProfileService(session).get()
    stored = session.scalar(select(CalendarProfile))
    assert stored is not None and profile.version == 1
    stored.conflict_policy = "block"

    result = CalendarActionService(session).apply_explicit(create_request(account))
    revision = session.get(ActionRevision, result.action_revision_id)

    assert revision is not None
    assert revision.preview["conflicts"][0]["provider_event_id"] == "provider-event-1"


def test_explicit_calendar_intent_queues_direct_operation_without_card(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, _state = calendar_fixture(session)
    monkeypatch.setattr(
        type(get_settings()),
        "calendar_write_mode",
        lambda _settings: "fake",
    )

    result = CalendarActionService(session).apply_explicit(create_request(account))
    session.flush()

    assert result.disposition == "execution_queued"
    queue_item = session.get(QueueItem, result.queue_item_id)
    action = session.get(Action, result.action_id)
    revision = session.get(ActionRevision, result.action_revision_id)
    operation = session.get(Operation, result.operation_id)
    assert queue_item is not None and queue_item.presentation == "suppressed"
    assert queue_item.status == "executing"
    assert action is not None and action.status == "ready"
    assert revision is not None and revision.authority == "explicit_user"
    assert operation is not None and operation.approval_id is None
    assert operation.status == "pending"

    compact = ActionReadService(session).get(result.action_id)
    assert "revision" not in compact
    assert compact["operation"]["status"] == "pending"

    full = ActionReadService(session).get(result.action_id, include_revision=True)
    assert full["revision"]["preview"] == revision.preview


def test_attendee_event_cannot_be_targeted(session: Session) -> None:
    account, _state = calendar_fixture(session)
    cached_event(session, account, has_attendees=True)
    request = create_request(account).model_dump(mode="json")
    request["proposal"] = {
        "kind": "reminders",
        "provider_event_id": "provider-event-1",
        "reminder_plan": {"lead_seconds": [300]},
    }

    with pytest.raises(DocketError) as raised:
        CalendarActionService(session).apply_explicit(
            ProposeCalendarEventInput.model_validate(request)
        )

    assert raised.value.code == "calendar_event_not_private"


def test_update_reminder_contract_does_not_merge_ambiguous_plans() -> None:
    account_id = "11111111-1111-4111-8111-111111111111"
    base = {
        "account_id": account_id,
        "calendar_id": "docket-smoke-calendar",
        "proposal": {
            "kind": "update",
            "provider_event_id": "provider-event-1",
            "replacement": {
                "title": "Replacement",
                "timing": {
                    "kind": "all_day",
                    "start_date": "2026-07-30",
                    "end_date": "2026-07-31",
                },
            },
            "reminder_disposition": "replace",
        },
        "request_key": f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:8",
        "source": trusted_source(8),
        "actor_id": OPERATOR_ID,
    }

    with pytest.raises(ValidationError, match="replace requires"):
        ProposeCalendarEventInput.model_validate(base)
