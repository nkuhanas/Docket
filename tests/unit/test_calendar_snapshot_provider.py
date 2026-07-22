from datetime import UTC, datetime

import httpx
import pytest

from docket.providers.google.calendar import CalendarProviderError, GoogleCalendarProvider


def test_google_snapshot_request_is_bounded_paginated_and_redacted(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(method, url, *, headers, json, params, timeout):
        captured.update(
            method=method,
            url=url,
            headers=headers,
            json=json,
            params=params,
            timeout=timeout,
        )
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            headers={"x-request-id": "request-1"},
            json={
                "timeZone": "America/Los_Angeles",
                "nextPageToken": "next-page",
                "items": [
                    {
                        "id": "provider-event",
                        "status": "confirmed",
                        "summary": "Exam",
                        "description": "must not be cached",
                        "attendees": [{"email": "private@example.test"}],
                        "conferenceData": {"entryPoints": [{"uri": "secret"}]},
                        "location": "Room 1",
                        "start": {
                            "dateTime": "2026-07-23T09:00:00-07:00",
                            "timeZone": "America/Los_Angeles",
                        },
                        "end": {
                            "dateTime": "2026-07-23T10:00:00-07:00",
                            "timeZone": "America/Los_Angeles",
                        },
                        "updated": "2026-07-22T10:00:00Z",
                        "etag": '"etag"',
                    }
                ],
            },
        )

    provider = GoogleCalendarProvider("unused-token.json")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer redacted")
    monkeypatch.setattr(httpx, "request", fake_request)
    page = provider.list_events_page(
        calendar_id="calendar@group.calendar.google.com",
        time_min=datetime(2026, 7, 22, tzinfo=UTC),
        time_max=datetime(2026, 8, 22, tzinfo=UTC),
        page_token="page-1",
    )

    assert captured["method"] == "GET"
    assert captured["json"] is None
    params = captured["params"]
    assert isinstance(params, dict)
    assert params == {
        "timeMin": "2026-07-22T00:00:00Z",
        "timeMax": "2026-08-22T00:00:00Z",
        "singleEvents": "true",
        "showDeleted": "true",
        "maxResults": "2500",
        "fields": (
            "nextPageToken,timeZone,"
            "items(id,status,summary,location,start,end,recurringEventId,"
            "originalStartTime,etag,updated)"
        ),
        "pageToken": "page-1",
    }
    assert page.next_page_token == "next-page"
    event = page.events[0]
    assert event.provider_event_id == "provider-event"
    assert event.start_at == datetime(2026, 7, 23, 16, tzinfo=UTC)
    assert not hasattr(event, "description")
    assert not hasattr(event, "attendees")
    assert not hasattr(event, "conference_data")


@pytest.mark.parametrize(
    "document",
    [
        {"items": "not-a-list"},
        {"items": ["not-an-event"]},
        {"items": [], "nextPageToken": 42},
        {
            "items": [
                {
                    "id": "bad-time",
                    "start": {"dateTime": "not-a-time"},
                    "end": {"dateTime": "also-not-a-time"},
                }
            ]
        },
    ],
)
def test_google_snapshot_rejects_malformed_pages(monkeypatch, document) -> None:
    def fake_request(method, url, *, headers, json, params, timeout):
        return httpx.Response(200, request=httpx.Request(method, url), json=document)

    provider = GoogleCalendarProvider("unused-token.json")
    monkeypatch.setattr(provider, "_authorization_header", lambda: "Bearer redacted")
    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(CalendarProviderError) as raised:
        provider.list_events_page(
            calendar_id="calendar@group.calendar.google.com",
            time_min=datetime(2026, 7, 22, tzinfo=UTC),
            time_max=datetime(2026, 8, 22, tzinfo=UTC),
            page_token=None,
        )

    assert raised.value.code == "google_calendar_invalid_response"
