import uuid

from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarEventResult,
    CalendarProviderError,
    CalendarUnknownOutcome,
)


class FakeCalendarProvider:
    """Stateful fake with injectable crash-window outcomes for automated tests."""

    def __init__(self) -> None:
        self.events: dict[str, CalendarEventResult] = {}
        self.next_create_outcome = "success"
        self.next_update_outcome = "success"

    @staticmethod
    def _result(request: CalendarEventRequest, event_id: str) -> CalendarEventResult:
        return CalendarEventResult(
            external_event_id=event_id,
            provider_etag=f'"fake-{uuid.uuid4()}"',
            provider_request_id=str(uuid.uuid4()),
            snapshot=request.snapshot(),
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
        result = self._result(request, request.external_event_id)
        self.events[result.external_event_id] = result
        if outcome == "unknown_after_write":
            raise CalendarUnknownOutcome("Injected unknown outcome after Calendar update.")
        return result

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
