import uuid

from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarEventResult,
    CalendarLaneDeleteResult,
    CalendarLaneProviderResult,
    CalendarLaneRequest,
    CalendarProviderError,
    CalendarSnapshotEvent,
    CalendarSnapshotPage,
    CalendarUnknownOutcome,
)


class FakeCalendarProvider:
    """Stateful fake with injectable crash-window outcomes for automated tests."""

    def __init__(self) -> None:
        self.events: dict[str, CalendarEventResult] = {}
        self.next_create_outcome = "success"
        self.next_update_outcome = "success"
        self.next_cancel_outcome = "success"
        self.next_move_outcome = "success"
        self.snapshot_events: dict[str, CalendarSnapshotEvent] = {}
        self.snapshot_page_size = 2500
        self.fail_snapshot_page: int | None = None
        self.snapshot_calls = 0
        self.lanes: dict[str, CalendarLaneProviderResult] = {}

    @staticmethod
    def _result(
        request: CalendarEventRequest,
        event_id: str,
        previous: CalendarEventResult | None = None,
    ) -> CalendarEventResult:
        snapshot = request.snapshot()
        if previous is not None and request.operation_type == "calendar_update_reminders":
            snapshot = {
                **previous.snapshot,
                "reminders": snapshot["reminders"],
                "docket_correlation": snapshot["docket_correlation"],
                "docket_reminder_plan_sha256": snapshot["docket_reminder_plan_sha256"],
            }
        elif (
            previous is not None
            and request.operation_type == "calendar_update_event"
            and request.reminder_plan is None
        ):
            snapshot["reminders"] = previous.snapshot["reminders"]
        return CalendarEventResult(
            external_event_id=event_id,
            provider_etag=f'"fake-{uuid.uuid4()}"',
            provider_request_id=str(uuid.uuid4()),
            snapshot=snapshot,
        )

    def create_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        outcome, self.next_create_outcome = self.next_create_outcome, "success"
        if outcome == "transient":
            raise CalendarProviderError(
                "fake_transient", "Injected transient Calendar failure.", transient=True
            )
        if outcome == "permanent":
            raise CalendarProviderError(
                "fake_permanent", "Injected permanent Calendar failure.", transient=False
            )
        result = self._result(request, f"fake-event-{uuid.uuid4()}")
        self.events[result.external_event_id] = result
        if outcome == "unknown_after_write":
            raise CalendarUnknownOutcome("Injected unknown outcome after Calendar creation.")
        return result

    def update_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        outcome, self.next_update_outcome = self.next_update_outcome, "success"
        if outcome == "transient":
            raise CalendarProviderError(
                "fake_transient", "Injected transient Calendar failure.", transient=True
            )
        if outcome == "permanent":
            raise CalendarProviderError(
                "fake_permanent", "Injected permanent Calendar failure.", transient=False
            )
        if request.external_event_id is None or request.external_event_id not in self.events:
            raise CalendarProviderError(
                "fake_not_found", "Fake Calendar event was not found.", transient=False
            )
        result = self._result(
            request,
            request.external_event_id,
            previous=self.events[request.external_event_id],
        )
        self.events[result.external_event_id] = result
        if outcome == "unknown_after_write":
            raise CalendarUnknownOutcome("Injected unknown outcome after Calendar update.")
        return result

    def cancel_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        outcome, self.next_cancel_outcome = self.next_cancel_outcome, "success"
        if outcome == "transient":
            raise CalendarProviderError(
                "fake_transient", "Injected transient Calendar failure.", transient=True
            )
        if outcome == "permanent":
            raise CalendarProviderError(
                "fake_permanent", "Injected permanent Calendar failure.", transient=False
            )
        if request.external_event_id is None:
            raise CalendarProviderError(
                "fake_not_found", "Fake Calendar event was not found.", transient=False
            )
        previous = self.events.pop(request.external_event_id, None)
        result = CalendarEventResult(
            external_event_id=request.external_event_id,
            provider_etag=None,
            provider_request_id=str(uuid.uuid4()),
            snapshot={
                **(previous.snapshot if previous is not None else request.snapshot()),
                "status": "cancelled",
            },
        )
        if outcome == "unknown_after_write":
            raise CalendarUnknownOutcome("Injected unknown outcome after Calendar cancellation.")
        return result

    def move_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        outcome, self.next_move_outcome = self.next_move_outcome, "success"
        if outcome == "transient":
            raise CalendarProviderError(
                "fake_transient", "Injected transient Calendar failure.", transient=True
            )
        if outcome == "permanent":
            raise CalendarProviderError(
                "fake_permanent", "Injected permanent Calendar failure.", transient=False
            )
        if (
            request.external_event_id is None
            or request.destination_calendar_id is None
            or request.external_event_id not in self.events
        ):
            raise CalendarProviderError(
                "fake_not_found", "Fake Calendar event was not found.", transient=False
            )
        previous = self.events[request.external_event_id]
        result = CalendarEventResult(
            external_event_id=request.external_event_id,
            provider_etag=f'"fake-{uuid.uuid4()}"',
            provider_request_id=str(uuid.uuid4()),
            snapshot=dict(previous.snapshot),
        )
        self.events[request.external_event_id] = result
        if outcome == "unknown_after_write":
            raise CalendarUnknownOutcome("Injected unknown outcome after Calendar move.")
        return result

    def get_event(self, request: CalendarEventRequest) -> CalendarEventResult | None:
        if request.external_event_id is None:
            return None
        return self.events.get(request.external_event_id)

    def find_by_correlation(self, request: CalendarEventRequest) -> list[CalendarEventResult]:
        correlation = request.provider_correlation
        return [
            event
            for event in self.events.values()
            if event.snapshot.get("docket_correlation") == correlation
        ]

    def add_correlation_duplicate(self, request: CalendarEventRequest) -> None:
        result = self._result(request, f"fake-event-{uuid.uuid4()}")
        self.events[result.external_event_id] = result

    def ensure_calendar_lane(self, request: CalendarLaneRequest) -> CalendarLaneProviderResult:
        existing = self.lanes.get(request.lane)
        if request.calendar_id is None and existing is None and not request.create_if_missing:
            raise CalendarProviderError(
                "google_calendar_lane_not_found",
                "The fake Calendar lane is not present.",
                transient=False,
            )
        result = CalendarLaneProviderResult(
            calendar_id=(
                request.calendar_id
                or (existing.calendar_id if existing is not None else f"fake-{request.lane}")
            ),
            provider_request_id=str(uuid.uuid4()),
        )
        self.lanes[request.lane] = result
        return result

    def delete_calendar_lane(self, request: CalendarLaneRequest) -> CalendarLaneDeleteResult:
        existing = self.lanes.get(request.lane)
        if existing is None or request.calendar_id != existing.calendar_id:
            raise CalendarProviderError(
                "google_calendar_lane_not_found",
                "The fake Calendar lane is not present.",
                transient=False,
            )
        del self.lanes[request.lane]
        return CalendarLaneDeleteResult(
            calendar_id=existing.calendar_id,
            provider_request_id=str(uuid.uuid4()),
        )

    def put_snapshot_event(self, event: CalendarSnapshotEvent) -> None:
        self.snapshot_events[event.provider_event_id] = event

    def remove_snapshot_event(self, provider_event_id: str) -> None:
        self.snapshot_events.pop(provider_event_id, None)

    def list_events_page(
        self,
        *,
        calendar_id: str,
        time_min: object,
        time_max: object,
        page_token: str | None,
    ) -> CalendarSnapshotPage:
        del calendar_id, time_min, time_max
        self.snapshot_calls += 1
        page_number = int(page_token or "0")
        if self.fail_snapshot_page == page_number:
            raise CalendarProviderError(
                "fake_snapshot_failure",
                "Injected Calendar snapshot page failure.",
                transient=True,
            )
        ordered = sorted(self.snapshot_events.values(), key=lambda item: item.provider_event_id)
        start = page_number * self.snapshot_page_size
        end = start + self.snapshot_page_size
        next_token = str(page_number + 1) if end < len(ordered) else None
        return CalendarSnapshotPage(
            events=tuple(ordered[start:end]),
            next_page_token=next_token,
            provider_request_id=f"fake-snapshot-{self.snapshot_calls}",
        )
