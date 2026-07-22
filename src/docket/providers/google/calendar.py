from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Protocol, cast
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
            "extendedProperties": {"private": {"docket_correlation": self.provider_correlation}},
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


@dataclass(frozen=True, slots=True)
class CalendarSnapshotEvent:
    provider_event_id: str
    status: str
    summary: str | None
    location: str | None
    is_all_day: bool
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: str | None = None
    recurring_event_id: str | None = None
    original_start_at: datetime | None = None
    provider_etag: str | None = None
    provider_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalendarSnapshotPage:
    events: tuple[CalendarSnapshotEvent, ...]
    next_page_token: str | None
    provider_request_id: str | None = None


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


class CalendarReadProvider(Protocol):
    def list_events_page(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        page_token: str | None,
    ) -> CalendarSnapshotPage: ...


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

        try:
            credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                self.token_file, scopes=[CALENDAR_EVENTS_SCOPE]
            )
            if not credentials.valid:
                credentials.refresh(Request())
        except Exception as exc:
            raise CalendarProviderError(
                "google_auth_invalid",
                "Google Calendar authorization is unavailable.",
                transient=False,
            ) from exc
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
            if method == "GET":
                raise CalendarProviderError(
                    "google_calendar_transient",
                    "Google Calendar could not be reached.",
                    transient=True,
                ) from exc
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
                "privateExtendedProperty": (f"docket_correlation={request.provider_correlation}"),
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

    @staticmethod
    def _snapshot_event(
        document: dict[str, Any], default_timezone: str | None
    ) -> CalendarSnapshotEvent:
        event_id = document.get("id")
        status = document.get("status", "confirmed")
        if not isinstance(event_id, str) or not event_id:
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned an event without an ID.",
                transient=False,
            )
        if status not in {"confirmed", "tentative", "cancelled"}:
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned an event with an unsupported status.",
                transient=False,
            )

        start = document.get("start")
        end = document.get("end")
        is_all_day = isinstance(start, dict) and isinstance(start.get("date"), str)
        start_at: datetime | None = None
        end_at: datetime | None = None
        start_date: date | None = None
        end_date: date | None = None
        timezone = default_timezone
        if isinstance(start, dict) and isinstance(start.get("timeZone"), str):
            timezone = start["timeZone"]
        if isinstance(timezone, str) and timezone:
            try:
                ZoneInfo(timezone)
            except Exception as exc:
                raise CalendarProviderError(
                    "google_calendar_invalid_response",
                    "Google Calendar returned an invalid event timezone.",
                    transient=False,
                ) from exc
        if status != "cancelled" or (isinstance(start, dict) and isinstance(end, dict)):
            try:
                if is_all_day:
                    if (
                        not isinstance(start, dict)
                        or not isinstance(start.get("date"), str)
                        or not isinstance(end, dict)
                        or not isinstance(end.get("date"), str)
                    ):
                        raise ValueError
                    start_date = date.fromisoformat(start["date"])
                    end_date = date.fromisoformat(end["date"])
                    if end_date <= start_date:
                        raise ValueError
                else:
                    if (
                        not isinstance(start, dict)
                        or not isinstance(start.get("dateTime"), str)
                        or not isinstance(end, dict)
                        or not isinstance(end.get("dateTime"), str)
                    ):
                        raise ValueError
                    start_at = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                    end_at = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
                    if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
                        raise ValueError
                    start_at = start_at.astimezone(UTC)
                    end_at = end_at.astimezone(UTC)
            except (TypeError, ValueError) as exc:
                raise CalendarProviderError(
                    "google_calendar_invalid_response",
                    "Google Calendar returned an event with invalid time bounds.",
                    transient=False,
                ) from exc

        original_start_at: datetime | None = None
        original = document.get("originalStartTime")
        if isinstance(original, dict) and isinstance(original.get("dateTime"), str):
            try:
                original_start_at = datetime.fromisoformat(
                    original["dateTime"].replace("Z", "+00:00")
                )
                if original_start_at.tzinfo is None:
                    raise ValueError
                original_start_at = original_start_at.astimezone(UTC)
            except ValueError as exc:
                raise CalendarProviderError(
                    "google_calendar_invalid_response",
                    "Google Calendar returned an invalid recurring-instance time.",
                    transient=False,
                ) from exc

        provider_updated_at: datetime | None = None
        updated = document.get("updated")
        if isinstance(updated, str):
            try:
                provider_updated_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if provider_updated_at.tzinfo is None:
                    raise ValueError
                provider_updated_at = provider_updated_at.astimezone(UTC)
            except ValueError as exc:
                raise CalendarProviderError(
                    "google_calendar_invalid_response",
                    "Google Calendar returned an invalid update timestamp.",
                    transient=False,
                ) from exc

        summary = document.get("summary")
        location = document.get("location")
        recurring = document.get("recurringEventId")
        etag = document.get("etag")
        return CalendarSnapshotEvent(
            provider_event_id=event_id,
            status=status,
            summary=(summary[:512] if isinstance(summary, str) and summary else None),
            location=(location[:1000] if isinstance(location, str) and location else None),
            is_all_day=is_all_day,
            start_at=start_at,
            end_at=end_at,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone[:128] if isinstance(timezone, str) and timezone else None,
            recurring_event_id=(
                recurring[:1024] if isinstance(recurring, str) and recurring else None
            ),
            original_start_at=original_start_at,
            provider_etag=etag[:1024] if isinstance(etag, str) and etag else None,
            provider_updated_at=provider_updated_at,
        )

    def list_events_page(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        page_token: str | None,
    ) -> CalendarSnapshotPage:
        if time_min.tzinfo is None or time_max.tzinfo is None or time_max <= time_min:
            raise ValueError("Calendar snapshot bounds must be ordered timezone-aware instants")
        params = {
            "timeMin": time_min.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "timeMax": time_max.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": "2500",
        }
        if page_token is not None:
            params["pageToken"] = page_token
        response = self._request("GET", self._event_url(calendar_id), params=params)
        try:
            document = response.json()
        except ValueError as exc:
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned an invalid event page.",
                transient=False,
            ) from exc
        if not isinstance(document, dict) or not isinstance(document.get("items", []), list):
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned an invalid event page.",
                transient=False,
            )
        default_timezone = document.get("timeZone")
        next_page_token = document.get("nextPageToken")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned an invalid page token.",
                transient=False,
            )
        if any(not isinstance(item, dict) for item in document["items"]):
            raise CalendarProviderError(
                "google_calendar_invalid_response",
                "Google Calendar returned a malformed event entry.",
                transient=False,
            )
        event_documents = cast(list[dict[str, Any]], document["items"])
        events = tuple(
            self._snapshot_event(
                item, default_timezone if isinstance(default_timezone, str) else None
            )
            for item in event_documents
        )
        return CalendarSnapshotPage(
            events=events,
            next_page_token=next_page_token,
            provider_request_id=response.headers.get("x-request-id"),
        )
