from typing import Never

from docket.providers.google.calendar import (
    CalendarEventRequest,
    CalendarEventResult,
    CalendarLaneProviderResult,
    CalendarLaneRequest,
    CalendarProviderError,
)


class DisabledCalendarProvider:
    """Fail-closed provider used when production Calendar writes are disabled."""

    @staticmethod
    def _unavailable() -> Never:
        raise CalendarProviderError(
            "external_writes_disabled",
            "External Calendar writes are disabled.",
            transient=False,
        )

    def create_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        del request
        return self._unavailable()

    def update_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        del request
        return self._unavailable()

    def cancel_event(self, request: CalendarEventRequest) -> CalendarEventResult:
        del request
        return self._unavailable()

    def get_event(self, request: CalendarEventRequest) -> CalendarEventResult | None:
        del request
        return self._unavailable()

    def find_by_correlation(self, request: CalendarEventRequest) -> list[CalendarEventResult]:
        del request
        return self._unavailable()

    def ensure_calendar_lane(self, request: CalendarLaneRequest) -> CalendarLaneProviderResult:
        del request
        return self._unavailable()
