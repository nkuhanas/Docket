import uuid

import httpx

from docket.config import get_settings
from docket.models import Account, AuditEvent, CalendarLane, CommandRequest, Operation
from docket.providers.google.calendar import (
    CalendarLaneProviderResult,
    CalendarLaneRequest,
    CalendarUnknownOutcome,
    GoogleCalendarProvider,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider
from docket.schemas.calendar import ConfigureCalendarLaneInput
from docket.schemas.records import DiscordSourceMetadata, RecordSourceInput
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
