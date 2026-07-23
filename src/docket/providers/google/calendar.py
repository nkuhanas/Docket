from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Protocol, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from docket.providers.google.oauth import CALENDAR_EVENTS_SCOPE

_SNAPSHOT_FIELDS = (
    "nextPageToken,timeZone,"
    "items(id,status,summary,location,start,end,recurringEventId,"
    "originalStartTime,recurrence,attendees(self),organizer(self),reminders,"
    "extendedProperties(private),etag,updated)"
)


@dataclass(frozen=True, slots=True)
class CalendarEventRequest:
    calendar_id: str
    provider_correlation: str
    summary: str
    schedule: dict[str, Any] | None = None
    external_event_id: str | None = None
    provider_etag: str | None = None
    event_spec: dict[str, Any] | None = None
    reminder_plan: dict[str, Any] | None = None
    logical_key: str | None = None
    priority: str = "normal"
    priority_basis: str = "default"
    reminder_plan_sha256: str | None = None
    operation_type: str = "calendar_create_meeting"

    def event_body(self) -> dict[str, Any]:
        if self.event_spec is not None:
            body = _standalone_event_body(self.event_spec)
        elif self.schedule is not None:
            body = _course_meeting_body(self.summary, self.schedule)
        else:
            body = {}
        private = {
            "docket_correlation": self.provider_correlation,
            "docket_origin_kind": (
                "course_meeting" if self.schedule is not None else "standalone"
            ),
            "docket_priority": self.priority,
            "docket_priority_basis": self.priority_basis,
        }
        if self.logical_key is not None:
            private["docket_logical_key"] = self.logical_key
        if self.reminder_plan_sha256 is not None:
            private["docket_reminder_plan_sha256"] = self.reminder_plan_sha256
        body["extendedProperties"] = {"private": private}
        reminders = _google_reminders(self.reminder_plan)
        if reminders is not None:
            body["reminders"] = reminders
        return body

    def snapshot(self) -> dict[str, Any]:
        return normalize_event_body(self.event_body())


def _course_meeting_body(summary: str, schedule: dict[str, Any]) -> dict[str, Any]:
    timezone = str(schedule["timezone"])
    start_date = str(schedule["first_occurrence_date"])
    end_date = str(schedule["end_date"])
    start_time = str(schedule["start_time"])
    end_time = str(schedule["end_time"])
    local_zone = ZoneInfo(timezone)
    until_local = datetime.combine(
        datetime.fromisoformat(end_date).date(), time.max, tzinfo=local_zone
    )
    until_utc = until_local.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    body: dict[str, Any] = {
        "summary": summary,
        "start": {
            "dateTime": f"{start_date}T{start_time}",
            "timeZone": timezone,
        },
        "end": {
            "dateTime": f"{start_date}T{end_time}",
            "timeZone": timezone,
        },
        "recurrence": [
            f"RRULE:FREQ=WEEKLY;BYDAY={','.join(schedule['days'])};UNTIL={until_utc}"
        ],
    }
    if schedule.get("location"):
        body["location"] = schedule["location"]
    return body


def _google_reminders(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    leads = [int(value) for value in plan.get("lead_seconds", [])]
    return {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": lead // 60}
            for lead in leads
        ],
    }


def _recurrence_lines(
    recurrence: dict[str, Any],
    *,
    is_all_day: bool,
    timezone: str,
    start_local: datetime | None,
) -> list[str]:
    parts = [
        f"FREQ={str(recurrence['frequency']).upper()}",
        f"INTERVAL={int(recurrence.get('interval', 1))}",
    ]
    weekdays = recurrence.get("weekdays", [])
    if weekdays:
        parts.append(f"BYDAY={','.join(str(day) for day in weekdays)}")
    month_days = recurrence.get("month_days", [])
    if month_days:
        parts.append(f"BYMONTHDAY={','.join(str(day) for day in month_days)}")
    count = recurrence.get("count")
    until_date = recurrence.get("until_date")
    if count is not None:
        parts.append(f"COUNT={int(count)}")
    elif until_date is not None:
        until = date.fromisoformat(str(until_date))
        if is_all_day:
            parts.append(f"UNTIL={until.strftime('%Y%m%d')}")
        else:
            until_local = datetime.combine(until, time.max, tzinfo=ZoneInfo(timezone))
            parts.append(f"UNTIL={until_local.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    lines = [f"RRULE:{';'.join(parts)}"]
    excluded_dates = [date.fromisoformat(str(value)) for value in recurrence.get(
        "excluded_dates", []
    )]
    additional_dates = [date.fromisoformat(str(value)) for value in recurrence.get(
        "additional_dates", []
    )]
    if is_all_day:
        if excluded_dates:
            encoded = ",".join(
                value.strftime("%Y%m%d") for value in excluded_dates
            )
            lines.append(
                f"EXDATE;VALUE=DATE:{encoded}"
            )
        if additional_dates:
            encoded = ",".join(
                value.strftime("%Y%m%d") for value in additional_dates
            )
            lines.append(
                f"RDATE;VALUE=DATE:{encoded}"
            )
    else:
        assert start_local is not None
        suffixes = [
            (
                "EXDATE",
                excluded_dates,
            ),
            (
                "RDATE",
                additional_dates,
            ),
        ]
        for kind, values in suffixes:
            if values:
                encoded = ",".join(
                    datetime.combine(value, start_local.time()).strftime("%Y%m%dT%H%M%S")
                    for value in values
                )
                lines.append(f"{kind};TZID={timezone}:{encoded}")
    return lines


def _standalone_event_body(event: dict[str, Any]) -> dict[str, Any]:
    timing = dict(event["timing"])
    timezone = str(timing["timezone"])
    is_all_day = timing["kind"] == "all_day"
    start_local: datetime | None = None
    if is_all_day:
        start = {"date": str(timing["start_date"])}
        end = {"date": str(timing["end_date"])}
    else:
        start_local = datetime.fromisoformat(str(timing["start_local"]))
        end_local = datetime.fromisoformat(str(timing["end_local"]))
        start = {
            "dateTime": start_local.isoformat(timespec="seconds"),
            "timeZone": timezone,
        }
        end = {
            "dateTime": end_local.isoformat(timespec="seconds"),
            "timeZone": timezone,
        }
    body: dict[str, Any] = {
        "summary": str(event["title"]),
        "start": start,
        "end": end,
    }
    location = event.get("location")
    notes = event.get("notes")
    body["location"] = str(location) if location is not None else None
    body["description"] = str(notes) if notes is not None else None
    recurrence = event.get("recurrence")
    if isinstance(recurrence, dict):
        body["recurrence"] = _recurrence_lines(
            recurrence,
            is_all_day=is_all_day,
            timezone=timezone,
            start_local=start_local,
        )
    return body


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
    has_attendees: bool = False
    organizer_is_self: bool | None = None
    recurrence_kind: str = "one_time"
    system_tags: tuple[str, ...] = ()
    provider_reminders: dict[str, Any] | None = None
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

    def cancel_event(self, request: CalendarEventRequest) -> CalendarEventResult: ...

    def get_event(self, request: CalendarEventRequest) -> CalendarEventResult | None: ...

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


def normalize_reminders(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"useDefault": True, "overrides": []}
    use_default = value.get("useDefault")
    overrides: list[dict[str, Any]] = []
    raw_overrides = value.get("overrides", [])
    if isinstance(raw_overrides, list):
        for override in raw_overrides:
            if (
                isinstance(override, dict)
                and override.get("method") in {"popup", "email"}
                and isinstance(override.get("minutes"), int)
            ):
                overrides.append(
                    {
                        "method": override["method"],
                        "minutes": override["minutes"],
                    }
                )
    overrides.sort(key=lambda item: (item["minutes"], item["method"]))
    return {
        "useDefault": use_default if isinstance(use_default, bool) else True,
        "overrides": overrides,
    }


def normalize_event_body(body: dict[str, Any]) -> dict[str, Any]:
    def endpoint(value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
            return value
        timezone = value.get("timeZone")
        parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        if parsed.tzinfo is not None and isinstance(timezone, str):
            parsed = parsed.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
        return {"dateTime": parsed.isoformat(timespec="seconds"), "timeZone": timezone}

    properties = body.get("extendedProperties", {})
    private = properties.get("private", {}) if isinstance(properties, dict) else {}
    if not isinstance(private, dict):
        private = {}
    return {
        "status": body.get("status"),
        "summary": body.get("summary"),
        "location": body.get("location"),
        "start": endpoint(body.get("start")),
        "end": endpoint(body.get("end")),
        "recurrence": body.get("recurrence", []),
        "reminders": normalize_reminders(body.get("reminders")),
        "docket_correlation": private.get("docket_correlation"),
        "docket_origin_kind": private.get("docket_origin_kind"),
        "docket_logical_key": private.get("docket_logical_key"),
        "docket_priority": private.get("docket_priority"),
        "docket_priority_basis": private.get("docket_priority_basis"),
        "docket_reminder_plan_sha256": private.get(
            "docket_reminder_plan_sha256"
        ),
    }


def event_matches_request(event: CalendarEventResult, request: CalendarEventRequest) -> bool:
    expected = request.snapshot()
    if request.operation_type == "calendar_cancel_event":
        return event.snapshot.get("status") == "cancelled"
    keys: tuple[str, ...]
    if request.operation_type == "calendar_update_reminders":
        keys = ("reminders", "docket_correlation", "docket_reminder_plan_sha256")
    else:
        keys = (
            "summary",
            "location",
            "start",
            "end",
            "recurrence",
            "docket_correlation",
            "docket_origin_kind",
            "docket_logical_key",
            "docket_priority",
            "docket_priority_basis",
            "docket_reminder_plan_sha256",
        )
        if request.reminder_plan is not None:
            keys += ("reminders",)
    return all(event.snapshot.get(key) == expected.get(key) for key in keys)


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
        allow_not_found: bool = False,
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
        if response.status_code == 404 and allow_not_found:
            return response
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

    def cancel_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        if request.external_event_id is None:
            raise CalendarProviderError(
                "calendar_event_id_missing",
                "Calendar cancellation requires an event ID.",
                transient=False,
            )
        response = self._request(
            "DELETE",
            self._event_url(request.calendar_id, request.external_event_id),
            params={"sendUpdates": "none"},
            etag=request.provider_etag,
            allow_not_found=True,
        )
        return CalendarEventResult(
            external_event_id=request.external_event_id,
            provider_etag=None,
            provider_request_id=response.headers.get("x-request-id"),
            snapshot={
                **request.snapshot(),
                "status": "cancelled",
            },
        )

    def get_event(self, request: CalendarEventRequest) -> CalendarEventResult | None:
        if request.external_event_id is None:
            raise CalendarProviderError(
                "calendar_event_id_missing",
                "Calendar lookup requires an event ID.",
                transient=False,
            )
        response = self._request(
            "GET",
            self._event_url(request.calendar_id, request.external_event_id),
            allow_not_found=True,
        )
        if response.status_code == 404:
            return None
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
        recurrence = document.get("recurrence")
        attendees = document.get("attendees")
        organizer = document.get("organizer")
        has_attendees = isinstance(attendees, list) and bool(attendees)
        organizer_is_self = (
            organizer.get("self")
            if isinstance(organizer, dict) and isinstance(organizer.get("self"), bool)
            else None
        )
        recurrence_kind = (
            "recurring"
            if (isinstance(recurring, str) and recurring)
            or (isinstance(recurrence, list) and bool(recurrence))
            else "one_time"
        )
        timing_kind = "all_day" if is_all_day else "timed"
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
            has_attendees=has_attendees,
            organizer_is_self=organizer_is_self,
            recurrence_kind=recurrence_kind,
            system_tags=(recurrence_kind, timing_kind),
            provider_reminders=normalize_reminders(document.get("reminders")),
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
            "fields": _SNAPSHOT_FIELDS,
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
