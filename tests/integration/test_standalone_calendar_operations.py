import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
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
    CalendarEventCache,
    CalendarLink,
    CalendarReminderPlan,
    CalendarSyncState,
    CommandRequest,
    DiscordDailyThread,
    DiscordProjection,
    Operation,
    OutboxEvent,
    QueueItem,
    ReminderRule,
    ScheduledNotification,
)
from docket.providers.discord import FakeDiscordBackend, FakeDiscordProjectionAdapter
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.actions import ProposeCalendarEventInput
from docket.services.approvals import ApprovalService
from docket.services.calendar_actions import CalendarActionService
from docket.services.discord_projection import DiscordProjectionRunner
from docket.services.operations import OperationRunner
from docket.services.proposal_controls import ProposalControlService
from docket.services.rollover import RolloverService


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


def test_all_day_projection_preserves_calendar_date_semantics() -> None:
    rendered = DiscordProjectionRunner._standalone_timing(
        {
            "timing": {
                "kind": "all_day",
                "start_date": "2026-07-30",
                "end_date": "2026-07-31",
                "timezone": "America/Los_Angeles",
            }
        }
    )

    assert rendered == (
        "All day · Jul 30, 2026 through Jul 31, 2026 (end exclusive)\n"
        "Calendar timezone: America/Los_Angeles"
    )
    assert "<t:" not in rendered


@pytest.mark.integration
def test_materially_identical_pending_standalone_proposal_is_suppressed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        account = _seed_target(session)
        first = CalendarActionService(session).propose(
            _create_request(account, "322222222222222222")
        )
        second = CalendarActionService(session).propose(
            _create_request(account, "323333333333333333")
        )

        assert first.disposition == "proposed"
        assert second.disposition == "matched_existing"
        assert second.request_id != first.request_id
        assert second.queue_item_id == first.queue_item_id
        assert second.action_id == first.action_id
        assert second.action_revision_id == first.action_revision_id
        assert second.approval_id == first.approval_id
        assert second.short_code == first.short_code
        assert session.scalar(select(func.count()).select_from(QueueItem)) == 1
        assert session.scalar(select(func.count()).select_from(Action)) == 1
        assert session.scalar(select(func.count()).select_from(Approval)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(func.count()).select_from(CommandRequest)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "action.duplicate_suppressed")
            )
            == 1
        )


@pytest.mark.integration
def test_standalone_create_executes_with_unified_reminder_plan(
    session_factory: sessionmaker[Session],
) -> None:
    message_id = "333333333333333333"
    with session_factory.begin() as session:
        account = _seed_target(session)
        account_id = account.id
        proposal = CalendarActionService(session).propose(_create_request(account, message_id))
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
        rules = list(session.scalars(select(ReminderRule).order_by(ReminderRule.lead_seconds)))
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
def test_update_card_prioritizes_operator_changes_and_terminal_state(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    backend = FakeDiscordBackend()
    projection_runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    provider = FakeCalendarProvider()
    operation_runner = OperationRunner(session_factory, provider)

    with session_factory.begin() as session:
        account = _seed_target(session)
        account_id = account.id
        created = CalendarActionService(session).propose(
            _create_request(account, "343333333333333333")
        )
    while projection_runner.run_due_once():
        pass
    with session_factory.begin() as session:
        _approve(
            session,
            short_code=created.short_code,
            interaction_id="operator-card-create-approval",
        )
    assert operation_runner.run_due_once() is True
    while projection_runner.run_due_once():
        pass

    with session_factory.begin() as session:
        account = session.get(Account, account_id)
        event = session.scalar(select(CalendarEventCache))
        assert account is not None and event is not None
        updated = CalendarActionService(session).propose(
            _event_request(
                account,
                "353333333333333333",
                {
                    "kind": "update",
                    "provider_event_id": event.provider_event_id,
                    "replacement": {
                        "title": "Check my email",
                        "timing": {
                            "kind": "timed",
                            "start_local": "2026-07-30T12:30:00",
                            "end_local": "2026-07-30T12:45:00",
                            "timezone": "America/Los_Angeles",
                        },
                        "location": "Desk",
                        "notes": "Operator-authored private note",
                        "operator_tags": ["email"],
                        "priority": "normal",
                    },
                    "reminder_disposition": "replace",
                    "reminder_plan": {"lead_seconds": [300]},
                },
            )
        )
    while projection_runner.run_due_once():
        pass

    with session_factory() as session:
        projection = session.scalar(
            select(DiscordProjection).where(
                DiscordProjection.view_action_revision_id == updated.action_revision_id
            )
        )
        assert projection is not None and projection.message_id is not None
        projection_id = projection.id
        message_id = projection.message_id
        projected = backend.messages[str(projection.id)]
        fields = {field["name"]: field["value"] for field in projected["embed"]["fields"]}
        assert projected["embed"]["title"] == "Review event update"
        assert projected["embed"]["description"] == (
            "Check my email\nReview the details below. Nothing changes until you approve."
        )
        assert fields["When"] == (
            "Starts <t:1785439800:F>\n"
            "Ends <t:1785440700:F>"
        )
        assert fields["Delta · Time"] == (
            "Before: <t:1785438000:F> to <t:1785438900:t>\n"
            "After: <t:1785439800:F> to <t:1785440700:t>"
        )
        assert fields["Delta · Reminders"] == (
            "Before: 5 minutes, 10 minutes\n"
            "After: 5 minutes"
        )
        assert fields["Details"] == "One-time · Normal priority\nTags: email"
        assert fields["Notifications"] == ("5 minutes\nGoogle Calendar popup + Docket daily thread")
        assert fields["Conflicts"] == "None found"

    with session_factory.begin() as session:
        _approve(
            session,
            short_code=updated.short_code,
            interaction_id="operator-card-update-approval",
        )
    assert operation_runner.run_due_once() is True
    while projection_runner.run_due_once():
        pass

    projected = backend.messages[str(projection_id)]
    fields = {field["name"]: field["value"] for field in projected["embed"]["fields"]}
    assert projected["message_id"] == message_id
    assert projected["embed"]["title"] == "Event updated"
    assert projected["embed"]["description"] == (
        "Check my email\nCompleted on your configured Docket calendar."
    )
    assert projected["embed"]["color"] == 0x3BA55D
    assert fields["Delta · Reminders"].endswith("After: 5 minutes")
    assert {
        "Status",
        "Calendar",
        "Execution",
        "Effect",
        "Before",
        "Changes",
        "Delta",
        "Conflicts",
    }.isdisjoint(fields)


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
            select(Operation).where(Operation.operation_type == "calendar_cancel_event")
        )
        assert event is not None and event.status == "cancelled"
        assert operation is not None and operation.status == "succeeded"
        assert provider_event_id not in provider.events


@pytest.mark.integration
def test_proposal_selects_and_custom_modal_replace_the_revision_in_place(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        account = _seed_target(session)
        proposed = CalendarActionService(session).propose(
            _create_request(account, "777777777777777777")
        )

    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert runner.run_due_once() is True
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and thread is not None
        assert projection.message_id is not None and thread.thread_id is not None
        projection_id = projection.id
        message_id = projection.message_id
        thread_id = thread.thread_id
        projected = backend.messages[str(projection.id)]
        fields = {field["name"]: field["value"] for field in projected["embed"]["fields"]}
        assert projected["embed"]["title"] == "Review new event"
        assert projected["embed"]["description"] == (
            "Check my email\nReview the details below. Nothing changes until you approve."
        )
        assert fields["When"] == (
            "Starts <t:1785438000:F>\n"
            "Ends <t:1785438900:F>"
        )
        assert fields["Where"] == "Desk"
        assert fields["Details"] == "One-time · Normal priority\nTags: email"
        assert fields["Notifications"] == (
            "5 minutes, 10 minutes\nGoogle Calendar popup + Docket daily thread"
        )
        assert fields["Conflicts"] == "None found"
        assert {
            "Status",
            "Calendar",
            "Execution",
            "Effect",
            "Before",
        }.isdisjoint(fields)
        serialized_embed = str(projected["embed"])
        for internal_value in (
            settings.google_calendar_id,
            "calendar_create_event",
            "provider_etag",
            "parameters_sha256",
            "preview_sha256",
            "last_success_at",
            str(proposed.action_revision_id),
        ):
            assert internal_value not in serialized_embed
        controls = projected["controls"]
        assert [control["kind"] for control in controls] == [
            "approval",
            "approval",
            "string_select",
            "string_select",
            "proposal_action",
            "proposal_action",
            "proposal_action",
        ]
        priority_control = next(
            control for control in controls if control.get("field") == "priority"
        )
        assert len(f"dkt:p:{priority_control['token']}") <= 100
        assert [option["value"] for option in priority_control["options"]] == [
            "low",
            "normal",
            "high",
            "urgent",
        ]
        assert (
            next(option["value"] for option in priority_control["options"] if option["default"])
            == "normal"
        )

    priority_response = LocalActionResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="proposal-priority-high",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection_id,
        message_id=message_id,
        responded_at=datetime.now(UTC),
        action_revision_id=proposed.action_revision_id,
        action_token=priority_control["token"],
        transition="proposal_field_change",
        field="priority",
        value="high",
    )
    with session_factory.begin() as session:
        changed = ProposalControlService(session).respond(priority_response)
        assert changed["revision"] == 2
    while runner.run_due_once():
        pass

    with session_factory() as session:
        action = session.get(Action, proposed.action_id)
        old_approval = session.get(Approval, proposed.approval_id)
        revisions = list(
            session.scalars(
                select(ActionRevision)
                .where(ActionRevision.action_id == proposed.action_id)
                .order_by(ActionRevision.revision)
            )
        )
        plans = list(
            session.scalars(
                select(CalendarReminderPlan).order_by(
                    CalendarReminderPlan.action_revision_id,
                    CalendarReminderPlan.lead_seconds,
                )
            )
        )
        assert action is not None and action.current_revision == 2
        assert old_approval is not None and old_approval.status == "superseded"
        assert revisions[1].parameters["priority"] == "high"
        assert revisions[1].parameters["priority_basis"] == "explicit_operator"
        assert revisions[1].preview["classification"]["priority"] == "high"
        assert sorted(
            plan.status for plan in plans if plan.action_revision_id == revisions[0].id
        ) == ["cancelled", "cancelled"]
        assert sorted(
            plan.status for plan in plans if plan.action_revision_id == revisions[1].id
        ) == ["planned", "planned"]
        assert session.scalar(select(Operation)) is None
        projected = backend.messages[str(projection_id)]
        assert projected["message_id"] == message_id
        controls = projected["controls"]
        refreshed_priority = next(
            control for control in controls if control.get("field") == "priority"
        )
        assert (
            next(option["value"] for option in refreshed_priority["options"] if option["default"])
            == "high"
        )
        reminder_control = next(
            control for control in controls if control.get("field") == "reminder_preset"
        )
        current_revision_id = revisions[1].id

    with session_factory.begin() as session, pytest.raises(DocketError) as rejected:
        ProposalControlService(session).respond(
            priority_response.model_copy(
                update={
                    "request_id": uuid.uuid4(),
                    "discord_interaction_id": "proposal-stale-priority",
                }
            )
        )
    assert rejected.value.code == "stale_proposal_control"

    custom_response = LocalActionResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="proposal-custom-reminders",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection_id,
        message_id=message_id,
        responded_at=datetime.now(UTC),
        action_revision_id=current_revision_id,
        action_token=reminder_control["token"],
        transition="proposal_edit",
        field="reminder_preset",
        modal_values={"reminder_leads_minutes": "5, 15"},
    )
    with session_factory.begin() as session:
        edited = ProposalControlService(session).respond(custom_response)
        assert edited["revision"] == 3
    while runner.run_due_once():
        pass

    with session_factory() as session:
        action = session.get(Action, proposed.action_id)
        assert action is not None and action.current_revision == 3
        current = session.scalar(
            select(ActionRevision).where(
                ActionRevision.action_id == proposed.action_id,
                ActionRevision.revision == 3,
            )
        )
        assert current is not None
        assert current.parameters["reminder_plan"]["lead_seconds"] == [300, 900]
        current_plans = list(
            session.scalars(
                select(CalendarReminderPlan)
                .where(CalendarReminderPlan.action_revision_id == current.id)
                .order_by(CalendarReminderPlan.lead_seconds)
            )
        )
        assert [plan.lead_seconds for plan in current_plans] == [300, 900]
        assert all(plan.status == "planned" for plan in current_plans)
        assert backend.messages[str(projection_id)]["message_id"] == message_id


@pytest.mark.integration
def test_refresh_rebinds_conflicts_and_edit_modal_replaces_typed_fields(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        account = _seed_target(session)
        account_id = account.id
        proposed = CalendarActionService(session).propose(
            _create_request(account, "888888888888888888")
        )
    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert runner.run_due_once() is True
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and thread is not None
        assert projection.message_id is not None and thread.thread_id is not None
        projection_id = projection.id
        message_id = projection.message_id
        thread_id = thread.thread_id
        controls = backend.messages[str(projection.id)]["controls"]
        refresh_control = next(
            control for control in controls if control.get("transition") == "proposal_refresh"
        )

    refresh_request = LocalActionResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="proposal-refresh",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection_id,
        message_id=message_id,
        responded_at=datetime.now(UTC),
        action_revision_id=proposed.action_revision_id,
        action_token=refresh_control["token"],
        transition="proposal_refresh",
    )
    with session_factory() as session:
        assert ProposalControlService(session).prepare_refresh(refresh_request) == (
            account_id,
            settings.google_calendar_id,
        )
    refresh_started = datetime.now(UTC)
    with session_factory.begin() as session:
        state = session.scalar(
            select(CalendarSyncState).where(CalendarSyncState.account_id == account_id)
        )
        assert state is not None
        generation = uuid.uuid4()
        state.snapshot_generation = generation
        state.last_attempt_at = refresh_started + timedelta(seconds=1)
        state.last_success_at = refresh_started + timedelta(seconds=1)
        state.status = "current"
        session.add(
            CalendarEventCache(
                account_id=account_id,
                calendar_id=settings.google_calendar_id,
                provider_event_id="fresh-conflict",
                snapshot_generation=generation,
                status="confirmed",
                summary="Existing appointment",
                location="Elsewhere",
                is_all_day=False,
                start_at=datetime(2026, 7, 30, 19, 5, tzinfo=UTC),
                end_at=datetime(2026, 7, 30, 19, 20, tzinfo=UTC),
                timezone="America/Los_Angeles",
                recurrence_kind="one_time",
                system_tags=["one_time", "timed", "external"],
                operator_tags=[],
                priority="normal",
                priority_basis="default",
                provider_reminders={},
                provider_etag='"fresh"',
                synced_at=refresh_started + timedelta(seconds=1),
            )
        )
    with session_factory.begin() as session:
        refreshed = ProposalControlService(session).respond(
            refresh_request,
            refresh_started_at=refresh_started,
        )
        assert refreshed["revision"] == 2
    while runner.run_due_once():
        pass
    with session_factory() as session:
        revision = session.scalar(
            select(ActionRevision).where(
                ActionRevision.action_id == proposed.action_id,
                ActionRevision.revision == 2,
            )
        )
        assert revision is not None
        assert revision.preview["conflicts"][0]["provider_event_id"] == "fresh-conflict"
        controls = backend.messages[str(projection_id)]["controls"]
        edit_control = next(
            control for control in controls if control.get("transition") == "proposal_edit"
        )

    edit_request = LocalActionResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="proposal-generic-edit",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection_id,
        message_id=message_id,
        responded_at=datetime.now(UTC),
        action_revision_id=uuid.UUID(str(refreshed["action_revision_id"])),
        action_token=edit_control["token"],
        transition="proposal_edit",
        modal_values={
            "title": "Check priority inbox",
            "location": "[clear]",
            "operator_tags": "email, focused",
        },
    )
    with session_factory.begin() as session:
        edited = ProposalControlService(session).respond(edit_request)
        assert edited["revision"] == 3
    with session_factory() as session:
        current = session.scalar(
            select(ActionRevision).where(
                ActionRevision.action_id == proposed.action_id,
                ActionRevision.revision == 3,
            )
        )
        assert current is not None
        assert current.parameters["event"]["title"] == "Check priority inbox"
        assert current.parameters["event"]["location"] is None
        assert current.parameters["event"]["operator_tags"] == ["email", "focused"]
        assert current.preview["classification"]["operator_tags"] == [
            "email",
            "focused",
        ]
        assert session.scalar(select(Operation)) is None


@pytest.mark.integration
def test_proposal_snooze_defers_the_fresh_approval_without_provider_work(
    session_factory: sessionmaker[Session],
) -> None:
    settings = get_settings()
    with session_factory.begin() as session:
        account = _seed_target(session)
        proposed = CalendarActionService(session).propose(
            _create_request(account, "999999999999999999")
        )
    backend = FakeDiscordBackend()
    runner = DiscordProjectionRunner(
        session_factory,
        FakeDiscordProjectionAdapter(backend),
        settings,
    )
    assert runner.run_due_once() is True
    with session_factory() as session:
        projection = session.scalar(select(DiscordProjection))
        thread = session.scalar(select(DiscordDailyThread))
        assert projection is not None and thread is not None
        assert projection.message_id is not None and thread.thread_id is not None
        old_projection_id = projection.id
        old_message_id = projection.message_id
        snooze = next(
            control
            for control in backend.messages[str(projection.id)]["controls"]
            if control.get("transition") == "proposal_snooze"
        )
    response = LocalActionResponse(
        request_id=uuid.uuid4(),
        discord_interaction_id="proposal-snooze",
        discord_user_id=settings.operator_discord_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=thread.thread_id,
        parent_channel_id=settings.queue_channel_id,
        projection_id=projection.id,
        message_id=projection.message_id,
        responded_at=datetime.now(UTC),
        action_revision_id=proposed.action_revision_id,
        action_token=snooze["token"],
        transition="proposal_snooze",
    )
    with session_factory.begin() as session:
        snoozed = ProposalControlService(session).respond(response)
        assert snoozed["queue_status"] == "snoozed"
        target_date = datetime.fromisoformat(str(snoozed["snooze_local_date"])).date()
    while runner.run_due_once():
        pass
    with session_factory.begin() as session:
        action = session.get(Action, proposed.action_id)
        queue = session.get(QueueItem, proposed.queue_item_id)
        assert action is not None and queue is not None
        assert action.current_revision == 2
        assert queue.status == "snoozed"
        assert backend.messages[str(old_projection_id)]["message_id"] == old_message_id
        assert backend.messages[str(old_projection_id)]["controls"] == []
        approval = session.scalar(
            select(Approval)
            .join(ActionRevision, ActionRevision.id == Approval.action_revision_id)
            .where(
                ActionRevision.action_id == proposed.action_id,
                Approval.status == "pending",
            )
        )
        assert approval is not None
        approval.expires_at = datetime.combine(
            target_date + timedelta(days=1),
            time(hour=7),
            tzinfo=ZoneInfo(settings.timezone),
        ).astimezone(UTC)
        assert session.scalar(select(Operation)) is None

    rollover_at = datetime.combine(
        target_date,
        time(hour=settings.daily_rollover_hour, minute=5),
        tzinfo=ZoneInfo(settings.timezone),
    ).astimezone(UTC)
    assert RolloverService(session_factory, settings).run_due_once(rollover_at)
    while runner.run_due_once():
        pass
    with session_factory() as session:
        queue = session.get(QueueItem, proposed.queue_item_id)
        projections = list(
            session.scalars(
                select(DiscordProjection).where(
                    DiscordProjection.queue_item_id == proposed.queue_item_id
                )
            )
        )
        assert queue is not None and queue.status == "awaiting_approval"
        assert len(projections) == 2
        newest = next(item for item in projections if item.id != old_projection_id)
        assert any(
            control["kind"] == "approval"
            for control in backend.messages[str(newest.id)]["controls"]
        )
