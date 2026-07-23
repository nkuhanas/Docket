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
    ActionRevision,
    CalendarEventCache,
    CalendarProfile,
    CalendarReminderPlan,
    CalendarSyncState,
    QueueItem,
)
from docket.schemas.actions import ProposeCalendarEventInput
from docket.schemas.calendar import SetCalendarProfileInput
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
            "request_key": (
                f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:{intent_index}"
            ),
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

    request = SetCalendarProfileInput(
        expected_version=1,
        proposal_mode="explicit_only",
        default_reminder_lead_seconds=[300, 600],
        conflict_policy="block",
        request_key=f"discord:{GUILD_ID}:{CHAT_CHANNEL_ID}:{MESSAGE_ID}:4",
        source=trusted_source(4),
        actor_id=OPERATOR_ID,
    )
    updated = service.set(request)

    assert updated.version == 2
    assert updated.proposal_mode == "explicit_only"
    assert updated.default_reminder_lead_seconds == [300, 600]
    assert session.scalar(select(CalendarProfile)) is not None


def test_standalone_create_proposal_uses_profile_reminder_and_conflict_scan(
    session: Session,
) -> None:
    account, _state = calendar_fixture(session)
    cached_event(session, account)
    request = create_request(account)

    result = CalendarActionService(session).propose(request)
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
    assert revision.parameters["logical_key"] == f"standalone:{result.request_id}"
    assert revision.parameters["reminder_plan"]["lead_seconds"] == [600]
    assert revision.parameters["reminder_plan_sha256"] == sha256_json(
        revision.parameters["reminder_plan"]
    )
    assert revision.preview["conflicts"][0]["provider_event_id"] == "provider-event-1"
    assert queue_item.priority == "normal"
    assert len(plans) == 1 and plans[0].lead_seconds == 600

    session.commit()
    replay = CalendarActionService(session).propose(request)
    assert replay.disposition == "replayed_request"
    assert replay.action_id == result.action_id


def test_profile_block_policy_rejects_overlap(session: Session) -> None:
    account, _state = calendar_fixture(session)
    cached_event(session, account)
    profile = CalendarProfileService(session).get()
    stored = session.scalar(select(CalendarProfile))
    assert stored is not None and profile.version == 1
    stored.conflict_policy = "block"

    with pytest.raises(DocketError) as raised:
        CalendarActionService(session).propose(create_request(account))

    assert raised.value.code == "calendar_conflict_blocked"


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
        CalendarActionService(session).propose(
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
