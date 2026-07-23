import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.config import get_settings
from docket.internal_api.schemas import ApprovalResponse
from docket.models import (
    Account,
    CalendarEventCache,
    CalendarLink,
    CalendarReminderPlan,
    CalendarSyncState,
    Operation,
    ReminderRule,
    ScheduledNotification,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.actions import ProposeCalendarEventInput
from docket.services.approvals import ApprovalService
from docket.services.calendar_actions import CalendarActionService
from docket.services.operations import OperationRunner


def _source(message_id: str, intent_index: int = 0) -> dict:
    settings = get_settings()
    return {
        "source_type": "discord_message",
        "source_object_id": message_id,
        "metadata": {
            "guild_id": settings.discord_guild_id,
            "channel_id": settings.chat_channel_id,
            "message_id": message_id,
            "user_id": settings.operator_discord_user_id,
            "intent_index": intent_index,
        },
    }


def _request_key(message_id: str, intent_index: int = 0) -> str:
    settings = get_settings()
    return (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:{intent_index}"
    )


def _seed_target(session: Session) -> Account:
    settings = get_settings()
    account = Account(
        provider="google",
        external_account_id=f"standalone-{uuid.uuid4()}",
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
    session.flush()
    return account


def _approve(
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
            message_id="222222222222222222",
            responded_at=datetime.now(UTC),
        )
    )
    return uuid.UUID(result["operation_id"])


def _create_request(account: Account, message_id: str) -> ProposeCalendarEventInput:
    settings = get_settings()
    return ProposeCalendarEventInput.model_validate(
        {
            "account_id": str(account.id),
            "calendar_id": settings.google_calendar_id,
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
                    "location": "Desk",
                    "notes": "Operator-authored private note",
                    "operator_tags": ["email"],
                    "reminder_plan": {
                        "lead_seconds": [300, 600],
                    },
                },
            },
            "request_key": _request_key(message_id),
            "source": _source(message_id),
            "actor_id": settings.operator_discord_user_id,
        }
    )


def _event_request(
    account: Account,
    message_id: str,
    proposal: dict,
) -> ProposeCalendarEventInput:
    settings = get_settings()
    return ProposeCalendarEventInput.model_validate(
        {
            "account_id": str(account.id),
            "calendar_id": settings.google_calendar_id,
            "proposal": proposal,
            "request_key": _request_key(message_id),
            "source": _source(message_id),
            "actor_id": settings.operator_discord_user_id,
        }
    )


@pytest.mark.integration
def test_standalone_create_executes_with_unified_reminder_plan(
    session_factory: sessionmaker[Session],
) -> None:
    message_id = "333333333333333333"
    with session_factory.begin() as session:
        account = _seed_target(session)
        account_id = account.id
        proposal = CalendarActionService(session).propose(
            _create_request(account, message_id)
        )
        operation_id = _approve(
            session,
            short_code=proposal.short_code,
            interaction_id="standalone-create-approval",
        )

    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once() is True

    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        link = session.scalar(select(CalendarLink))
        event = session.scalar(select(CalendarEventCache))
        plans = list(session.scalars(select(CalendarReminderPlan)))
        rules = list(
            session.scalars(
                select(ReminderRule).order_by(ReminderRule.lead_seconds)
            )
        )
        notifications = list(session.scalars(select(ScheduledNotification)))

        assert operation is not None and operation.status == "succeeded"
        assert link is not None and link.origin_kind == "standalone"
        assert link.account_id == account_id
        assert link.recurrence_kind == "one_time"
        assert link.system_tags == ["one_time", "timed", "standalone"]
        assert link.operator_tags == ["email"]
        assert event is not None and event.provider_event_id == link.external_event_id
        assert event.provider_reminders == {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 5},
                {"method": "popup", "minutes": 10},
            ],
        }
        assert [plan.status for plan in plans] == ["activated", "activated"]
        assert [rule.lead_seconds for rule in rules] == [300, 600]
        assert all(rule.source_kind == "canonical_plan" for rule in rules)
        assert len(notifications) == 2
        provider_event = provider.events[link.external_event_id]
        assert provider_event.snapshot["reminders"] == event.provider_reminders
        assert "description" not in provider_event.snapshot


@pytest.mark.integration
def test_reminder_disable_and_cancellation_converge_both_projections(
    session_factory: sessionmaker[Session],
) -> None:
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    with session_factory.begin() as session:
        account = _seed_target(session)
        account_id = account.id
        created = CalendarActionService(session).propose(
            _create_request(account, "444444444444444444")
        )
        _approve(
            session,
            short_code=created.short_code,
            interaction_id="lifecycle-create-approval",
        )
    assert runner.run_due_once() is True

    with session_factory.begin() as session:
        account = session.get(Account, account_id)
        event = session.scalar(select(CalendarEventCache))
        assert account is not None and event is not None
        provider_event_id = event.provider_event_id
        disabled = CalendarActionService(session).propose(
            _event_request(
                account,
                "555555555555555555",
                {
                    "kind": "reminders",
                    "provider_event_id": provider_event_id,
                    "reminder_plan": {"lead_seconds": []},
                },
            )
        )
        _approve(
            session,
            short_code=disabled.short_code,
            interaction_id="lifecycle-disable-approval",
        )
    provider.next_update_outcome = "unknown_after_write"
    assert runner.run_due_once() is True
    assert runner.reconcile_once() is True

    with session_factory() as session:
        event = session.scalar(select(CalendarEventCache))
        rules = list(session.scalars(select(ReminderRule)))
        notifications = list(session.scalars(select(ScheduledNotification)))
        assert event is not None
        assert event.provider_reminders == {
            "useDefault": False,
            "overrides": [],
        }
        assert all(not rule.enabled for rule in rules)
        assert all(item.status == "cancelled" for item in notifications)
        assert provider.events[provider_event_id].snapshot["reminders"] == (
            event.provider_reminders
        )

    with session_factory.begin() as session:
        account = session.get(Account, account_id)
        assert account is not None
        cancelled = CalendarActionService(session).propose(
            _event_request(
                account,
                "666666666666666666",
                {
                    "kind": "cancel",
                    "provider_event_id": provider_event_id,
                    "reason": "The operator no longer needs this event.",
                },
            )
        )
        _approve(
            session,
            short_code=cancelled.short_code,
            interaction_id="lifecycle-cancel-approval",
        )
    provider.next_cancel_outcome = "unknown_after_write"
    assert runner.run_due_once() is True
    assert runner.reconcile_once() is True

    with session_factory() as session:
        event = session.scalar(select(CalendarEventCache))
        operation = session.scalar(
            select(Operation)
            .where(Operation.operation_type == "calendar_cancel_event")
        )
        assert event is not None and event.status == "cancelled"
        assert operation is not None and operation.status == "succeeded"
        assert provider_event_id not in provider.events
