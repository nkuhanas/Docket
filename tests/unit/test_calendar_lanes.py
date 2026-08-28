import uuid
from datetime import UTC, datetime

import httpx

from docket.config import get_settings
from docket.internal_api.schemas import ApprovalResponse
from docket.models import (
    Account,
    AuditEvent,
    CalendarLane,
    CalendarLink,
    CommandRequest,
    Operation,
    OperationItem,
)
from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarLaneProviderResult,
    CalendarLaneRequest,
    CalendarUnknownOutcome,
    GoogleCalendarProvider,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.calendar import (
    CalendarLaneEventSelection,
    ConfigureCalendarLaneInput,
    DeleteCalendarLaneInput,
    MigrateCalendarLaneEventsInput,
)
from docket.schemas.records import DiscordSourceMetadata, RecordSourceInput
from docket.services.approvals import ApprovalService
from docket.services.calendar_lanes import CalendarLaneService
from docket.services.operations import OperationRunner


def _source(message_id: str) -> RecordSourceInput:
    settings = get_settings()
    return RecordSourceInput(
        source_type="discord_message",
        source_object_id=message_id,
        metadata=DiscordSourceMetadata(
            guild_id=settings.discord_guild_id,
            channel_id=settings.chat_channel_id,
            message_id=message_id,
            user_id=settings.operator_discord_user_id,
            intent_index=0,
        ),
    )


def _request_key(message_id: str, intent_index: int = 0) -> str:
    settings = get_settings()
    return (
        f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:"
        f"{message_id}:{intent_index}"
    )


def _approve(session, proposal: dict[str, object], interaction_id: str) -> uuid.UUID:
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
            message_id="000000000000000099",
            responded_at=datetime.now(UTC),
        )
    )
    return uuid.UUID(str(result["operation_id"]))


class _UnknownAfterLaneCreate(FakeCalendarProvider):
    def __init__(self) -> None:
        super().__init__()
        self.raise_unknown = True

    def ensure_calendar_lane(self, request: CalendarLaneRequest) -> CalendarLaneProviderResult:
        result = super().ensure_calendar_lane(request)
        if self.raise_unknown:
            self.raise_unknown = False
            raise CalendarUnknownOutcome("Injected unknown lane-creation outcome.")
        return result


def test_lane_registry_seeds_five_stable_lanes_and_configures_idempotently(
    session, session_factory
) -> None:
    settings = get_settings()
    account = Account(
        provider="google",
        external_account_id="calendar-lane-test",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    service = CalendarLaneService(session, settings)

    lanes = service.list_lanes(account.id)

    assert [lane.lane for lane in lanes] == [
        "academic",
        "work",
        "organizations",
        "personal",
        "unsorted",
    ]
    assert lanes[-1].calendar_id == settings.google_calendar_id
    assert lanes[-1].status == "active"
    assert all(lane.status == "unprovisioned" for lane in lanes[:-1])

    message_id = "000000000000000006"
    request = ConfigureCalendarLaneInput(
        lane="academic",
        expected_version=1,
        display_name="Docket · Academic",
        color_hex="#3f51b5",
        account_id=account.id,
        request_key=(
            f"discord:{settings.discord_guild_id}:{settings.chat_channel_id}:{message_id}:0"
        ),
        source=_source(message_id),
        actor_id=settings.operator_discord_user_id,
    )
    provider = _UnknownAfterLaneCreate()
    configured = service.configure(request)

    assert configured.disposition == "execution_queued"
    assert configured.lane.calendar_id is None
    assert configured.lane.color_hex == "#3F51B5"
    assert configured.lane.status == "provisioning"
    replay = service.configure(request)
    assert replay.disposition == "replayed_request"
    assert session.query(CalendarLane).count() == 5
    assert session.query(CommandRequest).count() == 1
    assert session.query(Operation).count() == 1
    session.commit()

    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once()
    with session_factory() as check:
        operation = check.get(Operation, configured.operation_id)
        assert operation is not None and operation.status == "reconciliation_required"
    assert runner.reconcile_once()
    session.expire_all()
    academic = session.query(CalendarLane).filter_by(lane="academic").one()
    assert academic.calendar_id == "fake-academic"
    assert academic.status == "active"
    assert session.query(AuditEvent).filter_by(event_type="calendar_lane.configured").count() == 1


def test_google_lane_ensure_creates_marks_and_colors_secondary_calendar(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    def google_request(method, url, *, headers, json=None, params=None, timeout):
        del headers, timeout
        calls.append((method, url, json, params))
        if method == "GET":
            body = {"items": []}
        elif method == "POST":
            body = {"id": "academic@group.calendar.google.com"}
        else:
            body = {}
        return httpx.Response(
            200,
            json=body,
            headers={"x-request-id": str(uuid.uuid4())},
            request=httpx.Request(method, url),
        )

    provider = GoogleCalendarProvider("unused-token-file")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer test")
    monkeypatch.setattr(httpx, "request", google_request)

    result = provider.ensure_calendar_lane(
        CalendarLaneRequest(
            lane="academic",
            display_name="Docket · Academic",
            color_hex="#3F51B5",
            timezone="America/Los_Angeles",
        )
    )

    assert result.calendar_id == "academic@group.calendar.google.com"
    assert [call[0] for call in calls] == ["GET", "POST", "PATCH", "PATCH"]
    assert calls[1][2] == {
        "summary": "Docket · Academic",
        "description": "Docket managed calendar lane: academic",
        "timeZone": "America/Los_Angeles",
    }
    assert calls[3][2] == {
        "summaryOverride": "Docket · Academic",
        "backgroundColor": "#3F51B5",
        "foregroundColor": "#FFFFFF",
        "selected": True,
    }
    assert calls[3][3] == {"colorRgbFormat": "true"}


def test_google_event_move_and_empty_lane_delete_use_narrow_provider_calls(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def google_request(method, url, *, headers, json=None, params=None, timeout):
        del headers, json, timeout
        calls.append((method, url, params))
        if url.endswith("/move"):
            return httpx.Response(
                200,
                json={
                    "id": "event-1",
                    "status": "confirmed",
                    "eventType": "default",
                    "summary": "Move me",
                    "start": {
                        "dateTime": "2026-08-28T15:00:00-07:00",
                        "timeZone": "America/Los_Angeles",
                    },
                    "end": {
                        "dateTime": "2026-08-28T15:15:00-07:00",
                        "timeZone": "America/Los_Angeles",
                    },
                },
                request=httpx.Request(method, url),
            )
        if method == "GET":
            return httpx.Response(
                200,
                json={"items": []},
                request=httpx.Request(method, url),
            )
        return httpx.Response(204, request=httpx.Request(method, url))

    provider = GoogleCalendarProvider("unused-token-file")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer test")
    monkeypatch.setattr(httpx, "request", google_request)
    moved = provider.move_event(
        CalendarEventRequest(
            calendar_id="source@group.calendar.google.com",
            destination_calendar_id="destination@group.calendar.google.com",
            external_event_id="event-1",
            provider_correlation="move-1",
            summary="Move me",
        )
    )
    assert moved.external_event_id == "event-1"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/events/event-1/move")
    assert calls[0][2] == {
        "destination": "destination@group.calendar.google.com",
        "sendUpdates": "none",
    }

    deleted = provider.delete_calendar_lane(
        CalendarLaneRequest(
            lane="archive",
            display_name="Docket · Archive",
            color_hex="#616161",
            timezone="America/Los_Angeles",
            calendar_id="archive@group.calendar.google.com",
            create_if_missing=False,
        )
    )
    assert deleted.calendar_id == "archive@group.calendar.google.com"
    assert [call[0] for call in calls[1:]] == ["GET", "DELETE"]
    assert calls[1][2] == {
        "showDeleted": "false",
        "singleEvents": "false",
        "maxResults": "1",
    }


def test_custom_lane_event_migration_and_empty_lane_deletion_are_durable(
    session, session_factory
) -> None:
    settings = get_settings()
    account = Account(
        provider="google",
        external_account_id="calendar-lane-lifecycle-test",
        capabilities=["google_calendar"],
        enabled=True,
    )
    session.add(account)
    session.flush()
    service = CalendarLaneService(session, settings)
    service.list_lanes(account.id)
    configure_message = "000000000000000020"
    service.configure(
        ConfigureCalendarLaneInput(
            lane="health",
            expected_version=None,
            display_name="Docket · Health",
            color_hex="#00897B",
            account_id=account.id,
            request_key=_request_key(configure_message),
            source=_source(configure_message),
            actor_id=settings.operator_discord_user_id,
        )
    )
    session.commit()
    provider = FakeCalendarProvider()
    runner = OperationRunner(session_factory, provider)
    assert runner.run_due_once()

    session.expire_all()
    health = session.query(CalendarLane).filter_by(account_id=account.id, lane="health").one()
    unsorted = session.query(CalendarLane).filter_by(account_id=account.id, lane="unsorted").one()
    assert health.status == "active"
    assert health.calendar_id == "fake-health"
    assert [lane.lane for lane in service.list_lanes(account.id)][-1] == "health"

    created = provider.create_event(
        CalendarEventRequest(
            calendar_id=str(unsorted.calendar_id),
            provider_correlation="lane-move-seed",
            summary="Move me",
            event_spec={
                "title": "Move me",
                "calendar_lane": "unsorted",
                "timing": {
                    "kind": "timed",
                    "start_local": "2026-08-28T15:00:00",
                    "end_local": "2026-08-28T15:15:00",
                    "timezone": "America/Los_Angeles",
                },
                "location": None,
                "notes": None,
                "operator_tags": [],
                "priority": "normal",
                "recurrence": None,
                "reminder_plan": None,
            },
        )
    )
    link = CalendarLink(
        record_id=None,
        meeting_id=None,
        origin_kind="standalone",
        logical_key="standalone:lane-move-test",
        account_id=account.id,
        calendar_id=str(unsorted.calendar_id),
        external_event_id=created.external_event_id,
        provider_etag=created.provider_etag,
        provider_correlation="lane-move-seed",
        last_synced_version=1,
        recurrence_kind="one_time",
        system_tags=["one_time", "timed", "standalone"],
        operator_tags=[],
        priority="normal",
        priority_basis="default",
        synced_snapshot=created.snapshot,
    )
    session.add(link)
    session.flush()
    move_message = "000000000000000021"
    proposal = service.migrate_events(
        MigrateCalendarLaneEventsInput(
            account_id=account.id,
            source_lane="unsorted",
            destination_lane="health",
            expected_source_version=unsorted.version,
            expected_destination_version=health.version,
            events=[
                CalendarLaneEventSelection(
                    provider_event_id=created.external_event_id,
                    scope="event",
                )
            ],
            reason="Move this appointment to Health.",
            request_key=_request_key(move_message),
            source=_source(move_message),
            actor_id=settings.operator_discord_user_id,
        )
    )
    operation_id = _approve(session, proposal, "lane-move-approve")
    session.commit()
    assert runner.run_due_once()
    session.expire_all()
    assert session.get(Operation, operation_id).status == "succeeded"
    assert session.query(OperationItem).filter_by(item_type="calendar_move_event").count() == 1
    assert session.get(CalendarLink, link.id).calendar_id == health.calendar_id

    empty_message = "000000000000000022"
    service.configure(
        ConfigureCalendarLaneInput(
            lane="archive",
            expected_version=None,
            display_name="Docket · Archive",
            color_hex="#616161",
            account_id=account.id,
            request_key=_request_key(empty_message),
            source=_source(empty_message),
            actor_id=settings.operator_discord_user_id,
        )
    )
    session.commit()
    assert runner.run_due_once()
    session.expire_all()
    archive = session.query(CalendarLane).filter_by(account_id=account.id, lane="archive").one()
    delete_message = "000000000000000023"
    deletion = service.delete_lane(
        DeleteCalendarLaneInput(
            account_id=account.id,
            lane="archive",
            expected_version=archive.version,
            reason="Remove the unused Archive lane.",
            request_key=_request_key(delete_message),
            source=_source(delete_message),
            actor_id=settings.operator_discord_user_id,
        )
    )
    delete_operation_id = _approve(session, deletion, "lane-delete-approve")
    session.commit()
    assert runner.run_due_once()
    session.expire_all()
    assert session.get(Operation, delete_operation_id).status == "succeeded"
    archive = session.get(CalendarLane, archive.id)
    assert archive is not None and archive.status == "deleted"
    assert archive.calendar_id is None
