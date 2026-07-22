from docket.providers.google.calendar import CalendarReadProvider
from docket.providers.google.fake_calendar import FakeCalendarProvider

_calendar_read_provider: CalendarReadProvider = FakeCalendarProvider()


def configure_calendar_read_provider(provider: CalendarReadProvider) -> None:
    global _calendar_read_provider
    _calendar_read_provider = provider


def get_calendar_read_provider() -> CalendarReadProvider:
    return _calendar_read_provider
