import json

import httpx
import pytest

from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarProviderError,
    CalendarUnknownOutcome,
    GoogleCalendarProvider,
    event_matches_request,
    normalize_event_body,
)


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
