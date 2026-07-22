from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from docket.providers.google.oauth import CALENDAR_EVENTS_SCOPE


@dataclass(frozen=True, slots=True)
class CalendarEventRequest:
    calendar_id: str
    provider_correlation: str
    summary: str
    schedule: dict[str, Any]
    external_event_id: str | None = None
    provider_etag: str | None = None

    def event_body(self) -> dict[str, Any]:
        timezone = str(self.schedule["timezone"])
        start_date = str(self.schedule["first_occurrence_date"])
        end_date = str(self.schedule["end_date"])
        start_time = str(self.schedule["start_time"])
        end_time = str(self.schedule["end_time"])
        local_zone = ZoneInfo(timezone)
        until_local = datetime.combine(
            datetime.fromisoformat(end_date).date(), time.max, tzinfo=local_zone
        )
        until_utc = until_local.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        body: dict[str, Any] = {
            "summary": self.summary,
            "start": {
                "dateTime": f"{start_date}T{start_time}",
                "timeZone": timezone,
            },
            "end": {
                "dateTime": f"{start_date}T{end_time}",
                "timeZone": timezone,
            },
            "recurrence": [
                f"RRULE:FREQ=WEEKLY;BYDAY={','.join(self.schedule['days'])};UNTIL={until_utc}"
            ],
            "extendedProperties": {
                "private": {"docket_correlation": self.provider_correlation}
            },
        }
        if self.schedule.get("location"):
            body["location"] = self.schedule["location"]
        return body

    def snapshot(self) -> dict[str, Any]:
        return normalize_event_body(self.event_body())


@dataclass(frozen=True, slots=True)
class CalendarEventResult:
    external_event_id: str
    provider_etag: str | None
    provider_request_id: str | None
    snapshot: dict[str, Any]


class CalendarProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.transient = transient


class CalendarUnknownOutcome(CalendarProviderError):
    def __init__(self, message: str = "Calendar request outcome is unknown.") -> None:
        super().__init__("calendar_unknown_outcome", message, transient=False)


class CalendarProvider(Protocol):
    def create_event(self, request: CalendarEventRequest) -> CalendarEventResult: ...

    def update_event(self, request: CalendarEventRequest) -> CalendarEventResult: ...

    def find_by_correlation(self, request: CalendarEventRequest) -> list[CalendarEventResult]: ...


def normalize_event_body(body: dict[str, Any]) -> dict[str, Any]:
    def endpoint(value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
            return value
        timezone = value.get("timeZone")
        parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        if parsed.tzinfo is not None and isinstance(timezone, str):
            parsed = parsed.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
        return {"dateTime": parsed.isoformat(timespec="seconds"), "timeZone": timezone}

    private = body.get("extendedProperties", {}).get("private", {})
    return {
        "summary": body.get("summary"),
        "location": body.get("location"),
        "start": endpoint(body.get("start")),
        "end": endpoint(body.get("end")),
        "recurrence": body.get("recurrence", []),
        "docket_correlation": private.get("docket_correlation"),
    }


def event_matches_request(event: CalendarEventResult, request: CalendarEventRequest) -> bool:
    return event.snapshot == request.snapshot()


class GoogleCalendarProvider:
    def __init__(self, token_file: str, *, timeout_seconds: float = 20.0) -> None:
        self.token_file = token_file
        self.timeout_seconds = timeout_seconds

    def _authorization_header(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            self.token_file, scopes=[CALENDAR_EVENTS_SCOPE]
        )
        if not credentials.valid:
            credentials.refresh(Request())
        if not credentials.token:
            raise CalendarProviderError(
                "google_auth_invalid", "Google did not provide an access token.", transient=False
            )
        return f"Bearer {credentials.token}"

    @staticmethod
    def _event_url(calendar_id: str, event_id: str | None = None) -> str:
        base = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}"
        if event_id is None:
            return f"{base}/events"
        return f"{base}/events/{quote(event_id, safe='')}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        etag: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": self._authorization_header()}
        if etag:
            headers["If-Match"] = etag
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                json=body,
                params=params,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise CalendarUnknownOutcome() from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise CalendarProviderError(
                "google_calendar_transient",
                f"Google Calendar returned HTTP {response.status_code}.",
                transient=True,
            )
        if response.status_code >= 400:
            code = (
                "google_auth_invalid"
                if response.status_code in {401, 403}
                else "google_calendar_rejected"
            )
            raise CalendarProviderError(
                code,
                f"Google Calendar returned HTTP {response.status_code}.",
                transient=False,
            )
        return response

    @staticmethod
    def _result(response: httpx.Response) -> CalendarEventResult:
        document = response.json()
        event_id = document.get("id") if isinstance(document, dict) else None
        if not isinstance(event_id, str) or not event_id:
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned an event without an ID.",
                transient=False,
            )
        etag = document.get("etag")
        return CalendarEventResult(
            external_event_id=event_id,
            provider_etag=etag if isinstance(etag, str) else None,
            provider_request_id=response.headers.get("x-request-id"),
            snapshot=normalize_event_body(document),
        )

    def create_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        response = self._request(
            "POST",
            self._event_url(request.calendar_id),
            body=request.event_body(),
            params={"sendUpdates": "none"},
        )
        return self._result(response)

    def update_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        if request.external_event_id is None:
            raise CalendarProviderError(
                "calendar_event_id_missing",
                "Calendar update requires an event ID.",
                transient=False,
            )
        response = self._request(
            "PATCH",
            self._event_url(request.calendar_id, request.external_event_id),
            body=request.event_body(),
            params={"sendUpdates": "none"},
            etag=request.provider_etag,
        )
        return self._result(response)

    def find_by_correlation(self, request: CalendarEventRequest) -> list[CalendarEventResult]:
        response = self._request(
            "GET",
            self._event_url(request.calendar_id),
            params={
                "privateExtendedProperty": (
                    f"docket_correlation={request.provider_correlation}"
                ),
                "showDeleted": "false",
                "singleEvents": "false",
                "maxResults": "10",
            },
        )
        document = response.json()
        items = document.get("items", []) if isinstance(document, dict) else []
        results: list[CalendarEventResult] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            results.append(
                CalendarEventResult(
                    external_event_id=item["id"],
                    provider_etag=item.get("etag") if isinstance(item.get("etag"), str) else None,
                    provider_request_id=response.headers.get("x-request-id"),
                    snapshot=normalize_event_body(item),
                )
            )
        return results
