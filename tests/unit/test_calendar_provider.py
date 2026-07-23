import json

import httpx
import pytest

from docket.config import get_settings
from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarProviderError,
    CalendarUnknownOutcome,
    GoogleCalendarProvider,
    event_matches_request,
    normalize_event_body,
)
from docket.providers.google.factory import (
    build_calendar_read_provider,
    build_calendar_write_provider,
)
from docket.providers.google.fake_calendar import FakeCalendarProvider


def event_request() -> CalendarEventRequest:
    return CalendarEventRequest(
        calendar_id="calendar@group.calendar.google.com",
        provider_correlation="11111111-1111-4111-8111-111111111111",
        summary="CSC 101 - Fundamentals",
        schedule={
            "meeting_type": "lecture",
            "days": ["MO", "WE"],
            "start_time": "10:30:00",
            "end_time": "11:50:00",
            "location": "Building 14",
            "start_date": "2026-08-24",
            "end_date": "2026-12-18",
            "timezone": "America/Los_Angeles",
            "first_occurrence_date": "2026-08-24",
        },
    )


def response(status: int, body: dict, *, url: str = "https://example.test") -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-request-id": "google-request-1"},
        request=httpx.Request("POST", url),
    )


def test_calendar_body_uses_local_recurrence_and_private_correlation() -> None:
    request = event_request()
    body = request.event_body()

    assert body["start"] == {
        "dateTime": "2026-08-24T10:30:00",
        "timeZone": "America/Los_Angeles",
    }
    assert body["recurrence"] == [
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261219T075959Z"
    ]
    assert body["extendedProperties"]["private"]["docket_correlation"] == (
        request.provider_correlation
    )


def test_google_offset_response_normalizes_to_immutable_request(monkeypatch) -> None:
    request = event_request()
    document = request.event_body() | {"id": "event-1", "etag": '"etag-1"'}
    document["start"] = {
        "dateTime": "2026-08-24T10:30:00-07:00",
        "timeZone": "America/Los_Angeles",
    }
    document["end"] = {
        "dateTime": "2026-08-24T11:50:00-07:00",
        "timeZone": "America/Los_Angeles",
    }
    provider = GoogleCalendarProvider("unused-token-file")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer test")
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: response(200, document))

    result = provider.create_event(request)

    assert result.external_event_id == "event-1"
    assert event_matches_request(result, request)


def test_http_timeout_is_unknown_but_rate_limit_is_definite_transient(monkeypatch) -> None:
    provider = GoogleCalendarProvider("unused-token-file")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer test")

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("injected timeout")

    monkeypatch.setattr(httpx, "request", timeout)
    with pytest.raises(CalendarUnknownOutcome):
        provider.create_event(event_request())

    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: response(429, {"error": {"code": 429}}),
    )
    with pytest.raises(CalendarProviderError) as raised:
        provider.create_event(event_request())
    assert raised.value.transient is True
    assert raised.value.code == "google_calendar_transient"


def test_normalization_drops_unneeded_google_response_fields() -> None:
    request = event_request()
    body = request.event_body() | {
        "id": "event-1",
        "htmlLink": "https://calendar.google.com/private-link",
        "creator": {"email": "private@example.com"},
    }

    snapshot = normalize_event_body(body)

    assert "htmlLink" not in snapshot
    assert "creator" not in snapshot
    assert snapshot == request.snapshot()


def test_real_calendar_reads_do_not_enable_provider_writes() -> None:
    settings = get_settings().model_copy(
        update={"calendar_reads_enabled": True, "external_writes_enabled": False}
    )

    assert isinstance(build_calendar_read_provider(settings), GoogleCalendarProvider)
    assert isinstance(build_calendar_write_provider(settings), FakeCalendarProvider)


def test_standalone_event_compiles_recurrence_exceptions_and_popup_plan() -> None:
    request = CalendarEventRequest(
        calendar_id="calendar@group.calendar.google.com",
        provider_correlation="22222222-2222-4222-8222-222222222222",
        summary="Check my email",
        event_spec={
            "title": "Check my email",
            "timing": {
                "kind": "timed",
                "start_local": "2026-07-30T12:00:00",
                "end_local": "2026-07-30T12:15:00",
                "timezone": "America/Los_Angeles",
                "fold": None,
            },
            "location": "Desk",
            "notes": "Private operator note",
            "operator_tags": ["email"],
            "priority": "normal",
            "recurrence": {
                "frequency": "weekly",
                "interval": 1,
                "weekdays": ["TH"],
                "month_days": [],
                "count": 4,
                "until_date": None,
                "excluded_dates": ["2026-08-06"],
                "additional_dates": ["2026-08-07"],
            },
            "reminder_plan": None,
        },
        reminder_plan={
            "delivery_channels": ["google_popup", "docket_queue"],
            "lead_seconds": [300, 600],
        },
        logical_key="standalone:request-1",
        reminder_plan_sha256="a" * 64,
        operation_type="calendar_create_event",
    )

    body = request.event_body()

    assert body["start"] == {
        "dateTime": "2026-07-30T12:00:00",
        "timeZone": "America/Los_Angeles",
    }
    assert body["recurrence"] == [
        "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=TH;COUNT=4",
        "EXDATE;TZID=America/Los_Angeles:20260806T120000",
        "RDATE;TZID=America/Los_Angeles:20260807T120000",
    ]
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": 5},
            {"method": "popup", "minutes": 10},
        ],
    }
    assert body["extendedProperties"]["private"]["docket_logical_key"] == (
        "standalone:request-1"
    )
    assert "description" not in request.snapshot()


def test_all_day_event_uses_exclusive_google_dates() -> None:
    request = CalendarEventRequest(
        calendar_id="calendar@group.calendar.google.com",
        provider_correlation="33333333-3333-4333-8333-333333333333",
        summary="Conference",
        event_spec={
            "title": "Conference",
            "timing": {
                "kind": "all_day",
                "start_date": "2026-08-10",
                "end_date": "2026-08-12",
                "timezone": "America/Los_Angeles",
            },
            "location": None,
            "notes": None,
            "operator_tags": [],
            "priority": "normal",
            "recurrence": None,
            "reminder_plan": None,
        },
        reminder_plan={"lead_seconds": []},
        operation_type="calendar_create_event",
    )

    body = request.event_body()

    assert body["start"] == {"date": "2026-08-10"}
    assert body["end"] == {"date": "2026-08-12"}
    assert body["reminders"] == {"useDefault": False, "overrides": []}
